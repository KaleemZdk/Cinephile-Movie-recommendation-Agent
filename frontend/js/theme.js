// Theme: "system" follows the OS; "dark" and "light" are explicit. theme-init.js applies it before first paint.
const KEY = "cinephile.theme";
const query = window.matchMedia("(prefers-color-scheme: dark)");

export function themePreference() {
  try {
    return localStorage.getItem(KEY) || "system";
  } catch {
    return "system";
  }
}

export function resolvedTheme() {
  return document.documentElement.getAttribute("data-theme") || "dark";
}

export function setTheme(preference) {
  try { localStorage.setItem(KEY, preference); } catch { /* storage unavailable */ }
  document.documentElement.setAttribute("data-theme", preference === "system" ? (query.matches ? "dark" : "light") : preference);
  document.dispatchEvent(new CustomEvent("themechange", { detail: preference }));
}

export function toggleTheme() {
  setTheme(resolvedTheme() === "dark" ? "light" : "dark");
}

export function initTheme() {
  query.addEventListener("change", () => {
    if (themePreference() === "system") setTheme("system");
  });
}
