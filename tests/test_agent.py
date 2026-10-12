"""Agent internals that need no network: history preparation and the model router."""

import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai.chat_models import GoogleRateLimitError

from cinephile.agent import messages as msgs
from cinephile.agent.models import LLMUnavailableError, ModelRouter, classify_error, retry_after_seconds
from cinephile.agent.prompts import profile_summary, system_message
from cinephile.storage import library
from cinephile.storage.library import TitleRef


def call(name="search_shows", id_="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": id_}])


def kinds(messages):
    return [type(m).__name__ for m in messages]


# ----------------------------------------------------------------- messages


def test_extract_text_handles_blocks():
    assert msgs.extract_text("plain") == "plain"
    assert msgs.extract_text([{"type": "text", "text": "a"}, {"type": "thinking", "x": 1}, "b"]) == "ab"


def test_repair_adds_missing_tool_result():
    fixed = msgs.repair_tool_calls([HumanMessage("hi"), call(), HumanMessage("again")])
    assert kinds(fixed) == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage"]
    assert fixed[2].tool_call_id == "c1" and fixed[2].content == msgs.INTERRUPTED_RESULT


def test_repair_keeps_complete_pairs_and_drops_orphans():
    history = [
        ToolMessage(content="orphan", tool_call_id="zzz", name="x"),
        HumanMessage("hi"),
        call(id_="c1"),
        ToolMessage(content="ok", tool_call_id="c1", name="search_shows"),
        AIMessage(content="done"),
    ]
    fixed = msgs.repair_tool_calls(history)
    assert kinds(fixed) == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert fixed[2].content == "ok"


def test_repair_fills_only_the_missing_one_of_two_calls():
    two = AIMessage(
        content="", tool_calls=[{"name": "a", "args": {}, "id": "c1"}, {"name": "b", "args": {}, "id": "c2"}]
    )
    fixed = msgs.repair_tool_calls([HumanMessage("x"), two, ToolMessage(content="r1", tool_call_id="c1", name="a")])
    assert [m.tool_call_id for m in fixed if isinstance(m, ToolMessage)] == ["c1", "c2"]


def test_old_tool_results_are_shortened_but_current_turn_is_not():
    big = "x" * 2000
    history = [
        HumanMessage("first"),
        call(id_="c1"),
        ToolMessage(content=big, tool_call_id="c1", name="s"),
        AIMessage(content="answer"),
        HumanMessage("second"),
        call(id_="c2"),
        ToolMessage(content=big, tool_call_id="c2", name="s"),
    ]
    out = msgs.shrink_old_tool_results(history)
    assert len(out[2].content) < 600 and "shortened" in out[2].content
    assert out[6].content == big
    assert history[2].content == big  # the saved history is untouched


def test_prepare_history_trims_to_a_human_start():
    history = []
    for i in range(30):
        history += [HumanMessage(f"q{i}"), AIMessage(content=f"a{i}")]
    prepared = msgs.prepare_history(history)
    assert prepared[0].type == "human"
    assert len(prepared) <= msgs.MAX_HISTORY_MESSAGES
    assert prepared[-1].content == "a29"


# ------------------------------------------------------------------ prompts


def test_system_message_includes_saved_titles():
    assert profile_summary("u1") == ""
    library.add_favorite("u1", TitleRef(1, "movie", "Whiplash", "2014"))
    library.save_review("u1", TitleRef(2, "tv", "Dark"), 9)
    text = system_message("u1").content
    assert "Whiplash (2014)" in text and "Dark 9/10" in text and "Today is" in text


# ------------------------------------------------------------------- models


def test_retry_after_parsing():
    assert retry_after_seconds("Please retry in 6h2m26.178s.") == pytest.approx(21746.178, rel=1e-3)
    assert retry_after_seconds("{'retryDelay': '21746s'}") == 21746
    assert retry_after_seconds("Please retry in 45s") == 45
    assert retry_after_seconds("nothing useful") is None


def test_classify_error():
    assert classify_error(GoogleRateLimitError("429")) == "quota"
    assert classify_error(TimeoutError()) == "unavailable"
    assert classify_error(ValueError("x")) == "other"


class FakeLLM:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        if isinstance(self.behaviour, Exception):
            raise self.behaviour
        return AIMessage(content=self.behaviour)


@pytest.fixture
def router():
    r = ModelRouter("m1", ["m2"], api_key="test", timeout=5)
    r.fakes = {}
    r._llm_for = lambda model: r.fakes[model]
    return r


async def test_falls_back_when_primary_is_out_of_quota_and_remembers_it(router):
    router.fakes = {"m1": FakeLLM(GoogleRateLimitError("429 retryDelay': '120s'")), "m2": FakeLLM("from m2")}

    assert (await router.invoke([HumanMessage("hi")])).content == "from m2"
    assert (await router.invoke([HumanMessage("again")])).content == "from m2"
    assert router.fakes["m1"].calls == 1, "a model in cooldown must not be called again"
    status = {s["name"]: s for s in router.status()}
    assert not status["m1"]["available"] and status["m1"]["retry_in"] > 60
    assert router._cooldown["m1"] > time.monotonic() + 60


async def test_preferred_model_goes_first(router):
    router.fakes = {"m1": FakeLLM("from m1"), "m2": FakeLLM("from m2")}
    assert (await router.invoke([HumanMessage("hi")], preferred="m2")).content == "from m2"
    assert router.fakes["m1"].calls == 0


async def test_raises_clear_error_when_every_model_fails(router):
    router.fakes = {"m1": FakeLLM(GoogleRateLimitError("429")), "m2": FakeLLM(GoogleRateLimitError("429"))}
    with pytest.raises(LLMUnavailableError) as info:
        await router.invoke([HumanMessage("hi")])
    assert info.value.kind == "quota" and "quota" in str(info.value) and info.value.retry_in


async def test_unknown_errors_are_not_swallowed(router):
    router.fakes = {"m1": FakeLLM(ValueError("bug")), "m2": FakeLLM("from m2")}
    with pytest.raises(ValueError):
        await router.invoke([HumanMessage("hi")])
    assert router.fakes["m2"].calls == 0


async def test_missing_api_key_is_reported_as_auth():
    router = ModelRouter("m1", [], api_key=None, timeout=5)
    with pytest.raises(LLMUnavailableError) as info:
        await router.invoke([HumanMessage("hi")])
    assert info.value.kind == "auth"


def test_chain_deduplicates_and_honours_preference():
    router = ModelRouter("a", ["b", "a"], api_key="k", timeout=5)
    assert router.chain() == ["a", "b"]
    assert router.chain(" b ") == ["b", "a"]
