// App shell: routing, sidebar, theme toggle, and startup.
import { api } from "./api.js";
import { initDrawer, closeDetails } from "./drawer.js";
import { $, $$, clear, h } from "./dom.js";
import { icon } from "./icons.js";
import { emit, loadLibrary, on, store } from "./store.js";
import { initTheme, resolvedTheme, toggleTheme } from "./theme.js";
import { initThreads } from "./threads.js";
import { toast } from "./ui.js";
import { mountChat } from "./views/chat.js";
import { mountDiscover } from "./views/discover.js";
import { mountFavorites, mountReviews, mountWatching } from "./views/library.js";
import { mountSettings } from "./views/settings.js";

const LAST_THREAD_KEY = "cinephile.lastThread";

const ROUTES = [
  { name: "chat", pattern: /^#\/chat(?:\/([\w-]+))?$/, nav: "chat", mount: (el, ctx, m) => mountChat(el, { ...ctx, threadId: m[1] || null }) },
  { name: "discover", pattern: /^#\/discover$/, nav: "discover", mount: mountDiscover },
  { name: "favorites", pattern: /^#\/favorites$/, nav: "favorites", mount: mountFavorites },
  { name: "watching", pattern: /^#\/watching$/, nav: "watching", mount: mountWatching },
  { name: "reviews", pattern: /^#\/reviews$/, nav: "reviews", mount: mountReviews },
  { name: "settings", pattern: /^#\/settings$/, nav: "settings", mount: mountSettings },
];

const viewEl = $("#view");
const sidebar = $("#sidebar");
const sidebarScrim = $("#sidebarScrim");
let cleanup = null;
let mountId = 0;

export function navigate(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function rememberThread(id) {
  try { localStorage.setItem(LAST_THREAD_KEY, id); } catch { /* storage unavailable */ }
}
function lastThread() {
  try { return localStorage.getItem(LAST_THREAD_KEY); } catch { return null; }
}

function setTitle(text) {
  $("#pageTitle").textContent = text;
  document.title = `${text} - Cinephile`;
}

function openSidebar(open) {
  sidebar.classList.toggle("is-open", open);
  sidebarScrim.classList.toggle("is-visible", open);
  $("#menuButton").setAttribute("aria-expanded", String(open));
}

async function route() {
  openSidebar(false);
  closeDetails();

  const hash = location.hash;
  if (!hash || hash === "#" || hash === "#/") {
    const last = lastThread();
    location.replace(last ? `#/chat/${last}` : "#/chat");
    return;
  }
  const found = ROUTES.map((r) => ({ r, m: r.pattern.exec(hash) })).find((x) => x.m);
  if (!found) { location.replace("#/chat"); return; }

  const id = ++mountId;
  if (cleanup) { try { cleanup(); } catch { /* view already gone */ } cleanup = null; }

  const root = h("div", { class: "view-root" });
  clear(viewEl).append(root);
  viewEl.scrollTop = 0;

  $$(".nav-link").forEach((link) => {
    if (link.dataset.nav === found.r.nav) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (found.r.name !== "chat") { store.activeThreadId = null; emit("route"); }
  else if (found.m[1]) rememberThread(found.m[1]);

  const result = await found.r.mount(root, { setTitle, navigate, reload: route }, found.m);
  if (id !== mountId) { if (typeof result === "function") result(); root.remove(); return; }
  cleanup = typeof result === "function" ? result : null;
}

function syncCounts() {
  $("#countFavorites").textContent = store.favorites.size || "";
  $("#countWatching").textContent = store.watching.size || "";
}

function syncThemeButton() {
  const dark = resolvedTheme() === "dark";
  const button = $("#themeToggle");
  button.replaceChildren(icon(dark ? "sun" : "moon"));
  button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  button.title = dark ? "Light theme" : "Dark theme";
}

function init() {
  initTheme();
  initDrawer();
  initThreads(navigate);

  $("#newChat").addEventListener("click", () => { openSidebar(false); navigate("#/chat"); });
  $("#menuButton").addEventListener("click", () => openSidebar(!sidebar.classList.contains("is-open")));
  sidebarScrim.addEventListener("click", () => openSidebar(false));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") openSidebar(false); });
  $("#themeToggle").addEventListener("click", toggleTheme);
  document.addEventListener("themechange", syncThemeButton);
  window.addEventListener("hashchange", route);
  on("library", syncCounts);

  syncThemeButton();
  loadLibrary();
  route();

  api.health().then((health) => {
    if (!health.agent_ready) toast("The assistant is not running yet. Chat will start working once the server is ready.", { error: true });
  }).catch(() => toast("Cannot reach the Cinephile server.", { error: true }));
}

init();
