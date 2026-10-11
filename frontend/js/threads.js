// Sidebar chat list: grouped by date, searchable, with pin / rename / delete.
import { api } from "./api.js";
import { clear, debounce, h } from "./dom.js";
import { icon } from "./icons.js";
import { dropThread, loadThreads, on, store, upsertThread } from "./store.js";
import { confirmDialog, promptDialog, toast } from "./ui.js";

let list;
let navigate;

function groupLabel(thread) {
  if (thread.pinned) return "Pinned";
  const days = Math.floor((new Date().setHours(0, 0, 0, 0) - new Date(thread.updated_at).setHours(0, 0, 0, 0)) / 86400000);
  if (days <= 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days < 7) return "Previous 7 days";
  if (days < 30) return "Previous 30 days";
  return "Older";
}

function threadRow(thread) {
  const active = thread.id === store.activeThreadId;
  const busy = store.streaming.has(thread.id);
  return h("div", { class: "thread", role: "listitem", "aria-current": active ? "true" : null },
    h("button", { class: "thread-open", type: "button", onClick: () => navigate(`#/chat/${thread.id}`), title: thread.title },
      thread.pinned ? icon("pin", { filled: true }) : null,
      h("span", { class: "title" }, thread.title),
      busy ? h("span", { class: "spinner", role: "status", "aria-label": "Answering" }) : null,
    ),
    h("div", { class: "thread-tools" },
      h("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": thread.pinned ? `Unpin ${thread.title}` : `Pin ${thread.title}`, title: thread.pinned ? "Unpin" : "Pin", onClick: () => pin(thread) }, icon("pin")),
      h("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Rename ${thread.title}`, title: "Rename", onClick: () => rename(thread) }, icon("pencil")),
      h("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Delete ${thread.title}`, title: "Delete", onClick: () => remove(thread) }, icon("trash")),
    ),
  );
}

function render() {
  if (!list) return;
  clear(list);
  if (!store.threads.length) {
    list.append(h("p", { class: "threads-empty" }, store.threadQuery ? "No chats match your search." : "Your conversations will appear here."));
    return;
  }
  let lastGroup = null;
  for (const thread of store.threads) {
    const group = groupLabel(thread);
    if (group !== lastGroup) {
      list.append(h("div", { class: "thread-group", role: "presentation" }, group));
      lastGroup = group;
    }
    list.append(threadRow(thread));
  }
}

async function pin(thread) {
  try { upsertThread(await api.threads.pin(thread.id, !thread.pinned)); }
  catch (error) { toast(error.message, { error: true }); }
}

async function rename(thread) {
  const title = await promptDialog({ title: "Rename chat", label: "Chat name", value: thread.title, maxLength: 60 });
  if (!title || title === thread.title) return;
  try { upsertThread(await api.threads.rename(thread.id, title)); }
  catch (error) { toast(error.message, { error: true }); }
}

async function remove(thread) {
  const ok = await confirmDialog({
    title: "Delete this chat?",
    message: `"${thread.title}" and its messages will be permanently removed.`,
    confirmLabel: "Delete",
    danger: true,
  });
  if (!ok) return;
  try {
    await api.threads.remove(thread.id);
    dropThread(thread.id);
    if (store.activeThreadId === thread.id) navigate("#/chat");
    toast("Chat deleted");
  } catch (error) {
    toast(error.message, { error: true });
  }
}

export function initThreads(go) {
  navigate = go;
  list = document.getElementById("threadList");
  const search = document.getElementById("threadSearch");
  const runSearch = debounce(() => { store.threadQuery = search.value.trim(); loadThreads(); }, 250);
  search.addEventListener("input", runSearch);
  on("threads", render);
  on("route", render);
  on("streaming", render);
  loadThreads();
}
