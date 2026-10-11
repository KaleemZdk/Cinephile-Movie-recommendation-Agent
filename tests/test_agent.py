import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai.chat_models import GoogleAPIError, GoogleRateLimitError

import agent


def call(name="search_shows", id_="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": id_}])


def test_repair_adds_missing_tool_result():
    fixed = agent._repair_tool_calls([HumanMessage("hi"), call(), HumanMessage("again")])
    assert [type(m).__name__ for m in fixed] == ["HumanMessage", "AIMessage", "ToolMessage", "HumanMessage"]
    assert fixed[2].tool_call_id == "c1"


def test_repair_keeps_complete_pairs_and_drops_orphans():
    msgs = [
        ToolMessage(content="orphan", tool_call_id="zzz", name="x"),
        HumanMessage("hi"),
        call(id_="c1"),
        ToolMessage(content="ok", tool_call_id="c1", name="search_shows"),
        AIMessage(content="done"),
    ]
    fixed = agent._repair_tool_calls(msgs)
    assert [type(m).__name__ for m in fixed] == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert fixed[2].content == "ok"


def test_repair_fills_only_the_missing_one_of_two_calls():
    two = AIMessage(content="", tool_calls=[
        {"name": "a", "args": {}, "id": "c1"}, {"name": "b", "args": {}, "id": "c2"}])
    fixed = agent._repair_tool_calls([HumanMessage("x"), two, ToolMessage(content="r1", tool_call_id="c1", name="a")])
    ids = [m.tool_call_id for m in fixed if isinstance(m, ToolMessage)]
    assert ids == ["c1", "c2"]


def test_old_tool_results_are_shortened_but_current_turn_is_not():
    big = "x" * 2000
    msgs = [
        HumanMessage("first"), call(id_="c1"), ToolMessage(content=big, tool_call_id="c1", name="s"),
        AIMessage(content="answer"),
        HumanMessage("second"), call(id_="c2"), ToolMessage(content=big, tool_call_id="c2", name="s"),
    ]
    out = agent._shrink_old_tool_results(msgs)
    assert len(out[2].content) < 600 and "shortened" in out[2].content
    assert out[6].content == big
    assert msgs[2].content == big  # the saved history is untouched


def test_prepare_messages_trims_to_a_human_start_and_adds_system():
    history = []
    for i in range(30):
        history += [HumanMessage(f"q{i}"), AIMessage(content=f"a{i}")]
    prepared = agent.prepare_messages(history)
    assert prepared[0].type == "system"
    assert prepared[1].type == "human"
    assert len(prepared) <= agent.MAX_HISTORY_MESSAGES + 1
    assert prepared[-1].content == "a29"


def test_retry_after_parsing():
    assert agent._retry_after_seconds("Please retry in 6h2m26.178s.") == pytest.approx(21746.178, rel=1e-3)
    assert agent._retry_after_seconds("{'retryDelay': '21746s'}") == 21746
    assert agent._retry_after_seconds("Please retry in 45s") == 45
    assert agent._retry_after_seconds("nothing useful") is None


def test_classify_error():
    assert agent.classify_error(GoogleRateLimitError("429")) == "quota"
    assert agent.classify_error(TimeoutError()) == "unavailable"
    assert agent.classify_error(ValueError("x")) == "other"


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
def chain(monkeypatch):
    agent._cooldown.clear()
    monkeypatch.setattr(agent, "GEMINI_MODEL", "m1")
    monkeypatch.setattr(agent, "GEMINI_FALLBACK_MODELS", ["m2"])
    llms = {}
    monkeypatch.setattr(agent, "_bound", lambda model: llms[model])
    yield llms
    agent._cooldown.clear()


async def test_falls_back_when_primary_is_out_of_quota_and_remembers_it(chain):
    chain["m1"] = FakeLLM(GoogleRateLimitError("429 retryDelay': '120s'"))
    chain["m2"] = FakeLLM("from m2")

    first = await agent.invoke_with_fallback([HumanMessage("hi")])
    assert first.content == "from m2" and chain["m1"].calls == 1

    second = await agent.invoke_with_fallback([HumanMessage("hi again")])
    assert second.content == "from m2"
    assert chain["m1"].calls == 1, "a model in cooldown must not be called again"
    assert agent._cooldown["m1"] > time.monotonic() + 60


async def test_preferred_model_goes_first(chain):
    chain["m1"] = FakeLLM("from m1")
    chain["m2"] = FakeLLM("from m2")
    out = await agent.invoke_with_fallback([HumanMessage("hi")], preferred="m2")
    assert out.content == "from m2" and chain["m1"].calls == 0


async def test_raises_clear_error_when_every_model_fails(chain):
    chain["m1"] = FakeLLM(GoogleRateLimitError("429"))
    chain["m2"] = FakeLLM(GoogleRateLimitError("429"))
    with pytest.raises(agent.LLMUnavailableError) as info:
        await agent.invoke_with_fallback([HumanMessage("hi")])
    assert info.value.kind == "quota" and "quota" in str(info.value)


async def test_unknown_errors_are_not_swallowed(chain):
    chain["m1"] = FakeLLM(ValueError("bug"))
    chain["m2"] = FakeLLM("from m2")
    with pytest.raises(ValueError):
        await agent.invoke_with_fallback([HumanMessage("hi")])
    assert chain["m2"].calls == 0
