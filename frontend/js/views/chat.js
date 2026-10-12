// Chat view: loads a saved thread, streams new replies, and shows recommendation cards.
import { api, chatStream } from "../api.js";
import { titleCard } from "../components.js";
import { h } from "../dom.js";
import { icon } from "../icons.js";
import { renderMarkdown } from "../markdown.js";
import { emit, on, store, upsertThread } from "../store.js";
import { toast } from "../ui.js";

const SUGGESTIONS = [
  { title: "Something like Dark", text: "Recommend something like Dark", hint: "Slow-burn mysteries with big ideas" },
  { title: "Tonight, under two hours", text: "Suggest a great movie for tonight that is under two hours", hint: "A satisfying, easy pick" },
  { title: "Hidden gems", text: "Recommend some underrated movies or shows I probably have not seen", hint: "Overlooked and worth your time" },
  { title: "Based on my taste", text: "Recommend something based on my favorites and reviews", hint: "Uses what you have saved" },
];

export function modelPreference() {
  try {
    const value = localStorage.getItem("cinephile.model");
    return value && value !== "auto" ? value : null;
  } catch {
    return null;
  }
}

function userMessage(text) {
  return h("div", { class: "msg msg-user" }, h("div", { class: "bubble-user" }, text));
}

/** An assistant message whose parts can be updated while the reply streams in. */
function assistantMessage() {
  const typing = h("div", { class: "typing", role: "status", "aria-label": "Cinephile is thinking" }, h("i"), h("i"), h("i"));
  const statusText = h("span", {});
  const status = h("div", { class: "status", role: "status", hidden: true }, h("span", { class: "spinner" }), statusText);
  const prose = h("div", { class: "prose" });
  const cards = h("div", { class: "msg-cards", hidden: true });
  const tools = h("div", { class: "msg-tools" });
  const notice = h("div", {});
  const root = h("div", { class: "msg msg-assistant" },
    h("div", { class: "avatar" }, icon("sparkles")),
    h("div", { class: "msg-body" }, typing, status, prose, notice, cards, tools),
  );

  let text = "";
  let frame = 0;
  const paint = () => { frame = 0; prose.innerHTML = renderMarkdown(text); };
  const idle = () => { typing.hidden = true; status.hidden = true; };

  const message = {
    root,
    text: () => text,
    status(label) {
      typing.hidden = true;
      status.hidden = false;
      statusText.textContent = label;
      text = "";
      prose.innerHTML = "";
    },
    token(chunk) {
      idle();
      text += chunk;
      prose.classList.add("is-streaming");
      if (!frame) frame = requestAnimationFrame(paint);
    },
    cards(items) {
      if (!items || !items.length) return;
      cards.hidden = false;
      cards.replaceChildren(...items.map((item) => titleCard(item)));
    },
    done(reply) {
      idle();
      cancelAnimationFrame(frame);
      text = reply;
      prose.classList.remove("is-streaming");
      paint();
      tools.replaceChildren(h("button", {
        class: "icon-btn icon-btn-sm", type: "button", "aria-label": "Copy reply", title: "Copy",
        onClick: async () => {
          try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); }
          catch { toast("Copy is not available in this browser.", { error: true }); }
        },
      }, icon("copy")));
    },
    stopped() {
      idle();
      cancelAnimationFrame(frame);
      prose.classList.remove("is-streaming");
      paint();
      notice.replaceChildren(h("p", { class: "stopped" }, "Stopped."));
    },
    error(message, retry) {
      idle();
      cancelAnimationFrame(frame);
      prose.classList.remove("is-streaming");
      paint();
      notice.replaceChildren(h("div", { class: "msg-error", role: "alert" },
        icon("alert"),
        h("div", {}, h("p", {}, message),
          retry ? h("button", { class: "btn btn-secondary btn-sm", type: "button", onClick: retry }, "Try again") : null),
      ));
    },
  };
  return message;
}

function savedAssistant(message) {
  const reply = assistantMessage();
  reply.done(message.content);
  reply.cards(message.recommendations);
  return reply.root;
}

export async function mountChat(container, { threadId, setTitle, navigate, reload }) {
  container.classList.add("is-chat");
  const view = { alive: true, threadId: threadId || null, busy: false, abort: null, stick: true };
  const cleanups = [];
  store.activeThreadId = view.threadId;
  emit("route");

  // ---- layout
  const inner = h("div", { class: "chat-inner", role: "log", "aria-label": "Conversation" });
  const welcome = h("div", { class: "welcome" },
    h("div", { class: "welcome-mark" }, icon("film")),
    h("h2", {}, "What are you in the mood for?"),
    h("p", {}, "Tell me what you like, or describe a feeling. I will find movies and shows that fit, and remember what you save."),
    h("div", { class: "suggestions" }, SUGGESTIONS.map((s) =>
      h("button", { class: "suggestion", type: "button", onClick: () => send(s.text) }, h("strong", {}, s.title), h("span", {}, s.hint)))),
  );
  const scroller = h("div", { class: "chat-scroll" }, inner);
  const jump = h("button", { class: "btn btn-secondary btn-sm jump", type: "button", hidden: true, onClick: () => scrollEnd(true) }, icon("arrowDown"), "Latest");

  const textarea = h("textarea", { rows: "1", maxlength: "4000", placeholder: "Ask for a recommendation, a comparison, or what to watch next...", "aria-label": "Message Cinephile" });
  const sendButton = h("button", { class: "send-btn", type: "button" });
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = Recognition ? h("button", { class: "icon-btn", type: "button", "aria-label": "Dictate a message", title: "Dictate", onClick: dictate }, icon("mic")) : null;
  const form = h("form", { class: "composer", onSubmit: (event) => event.preventDefault() },
    h("div", { class: "composer-row" }, textarea, mic, sendButton));
  const composerWrap = h("div", { class: "composer-wrap" }, form,
    h("p", { class: "composer-foot" }, "Suggestions use TMDB data and can be imperfect. Check details before you watch."));
  container.append(h("div", { class: "chat" }, scroller, jump, composerWrap));

  // ---- helpers
  function scrollEnd(force = false) {
    if (force || view.stick) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "auto" });
  }
  scroller.addEventListener("scroll", () => {
    view.stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
    jump.hidden = view.stick;
  });

  function autosize() {
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 180)}px`;
  }

  function syncComposer() {
    sendButton.replaceChildren(icon(view.busy ? "stop" : "arrowUp", { filled: view.busy }));
    sendButton.classList.toggle("is-stop", view.busy);
    sendButton.setAttribute("aria-label", view.busy ? "Stop generating" : "Send message");
    sendButton.disabled = !view.busy && !textarea.value.trim();
  }

  function showMessages() {
    if (welcome.isConnected) welcome.remove();
    if (!inner.isConnected) scroller.replaceChildren(inner);
  }

  function dictate() {
    const recognition = new Recognition();
    recognition.lang = navigator.language || "en-US";
    recognition.interimResults = false;
    recognition.onresult = (event) => { textarea.value = event.results[0][0].transcript; autosize(); syncComposer(); textarea.focus(); };
    recognition.onerror = () => toast("Voice input could not be captured.", { error: true });
    recognition.start();
    toast("Listening...");
  }

  // ---- sending
  async function send(raw, { retry = false } = {}) {
    const text = String(raw || "").trim();
    if (!text || view.busy) return;
    showMessages();
    if (!retry) inner.append(userMessage(text));
    textarea.value = "";
    autosize();

    const reply = assistantMessage();
    inner.append(reply.root);
    view.stick = true;
    scrollEnd(true);
    view.busy = true;
    syncComposer();
    const controller = new AbortController();
    view.abort = controller;

    const adopt = (thread) => {
      if (!thread) return;
      // The server starts a new thread when the one in the URL is gone (or belongs to someone else).
      if (view.threadId !== thread.id) {
        view.threadId = thread.id;
        if (view.alive) {
          store.activeThreadId = thread.id;
          history.replaceState(null, "", `#/chat/${thread.id}`);
          try { localStorage.setItem("cinephile.lastThread", thread.id); } catch { /* storage unavailable */ }
        }
      }
      store.streaming.add(thread.id);
      emit("streaming");
      upsertThread(thread);
      if (view.alive) setTitle(thread.title);
    };

    const onEvent = (event) => {
      if (event.type === "thread") adopt(event.thread);
      else if (event.type === "status") reply.status(event.label);
      else if (event.type === "token") reply.token(event.text);
      else if (event.type === "cards") reply.cards(event.recommendations);
      else if (event.type === "done") { reply.done(event.reply); upsertThread(event.thread); }
      else if (event.type === "error") reply.error(event.message, event.kind === "busy" ? null : () => { reply.root.remove(); send(text, { retry: true }); });
      if (view.alive) scrollEnd();
    };

    try {
      await chatStream({ message: text, thread_id: view.threadId, model: modelPreference() }, onEvent, controller.signal);
    } catch (error) {
      if (error.name === "AbortError") reply.stopped();
      else reply.error(error.message, () => { reply.root.remove(); send(text, { retry: true }); });
    } finally {
      const id = view.threadId;
      view.busy = false;
      view.abort = null;
      if (id) { store.streaming.delete(id); emit("streaming"); emit("turn-done", id); }
      if (view.alive) { syncComposer(); textarea.focus(); scrollEnd(); }
    }
  }

  sendButton.addEventListener("click", () => {
    if (view.busy) view.abort?.abort();
    else send(textarea.value);
  });
  textarea.addEventListener("input", () => { autosize(); syncComposer(); });
  textarea.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send(textarea.value);
    }
  });

  // ---- initial content
  if (view.threadId) {
    try {
      const { thread, messages } = await api.threads.get(view.threadId);
      if (!view.alive) return () => {};
      upsertThread(thread);
      setTitle(thread.title);
      showMessages();
      for (const message of messages) {
        inner.append(message.role === "user" ? userMessage(message.content) : savedAssistant(message));
      }
      if (store.streaming.has(view.threadId)) {
        const pending = assistantMessage();
        pending.status("Still answering");
        inner.append(pending.root);
        cleanups.push(on("turn-done", (id) => { if (id === view.threadId && view.alive) reload(); }));
      }
      view.stick = true;
      scrollEnd(true);
    } catch (error) {
      // A thread the server no longer has (for example after a redeploy) must not be reopened on every visit.
      if (error.status === 404) {
        try { localStorage.removeItem("cinephile.lastThread"); } catch { /* storage unavailable */ }
      }
      toast("That chat could not be opened.", { error: true });
      navigate("#/chat");
      return () => { view.alive = false; container.classList.remove("is-chat"); };
    }
  } else {
    setTitle("New chat");
    scroller.replaceChildren(welcome);
  }

  syncComposer();
  textarea.focus();

  return () => {
    view.alive = false;
    cleanups.forEach((fn) => fn());
    container.classList.remove("is-chat");
  };
}
