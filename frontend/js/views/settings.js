// Settings: theme, model choice and status, and local data.
import { api } from "../api.js";
import { segmented } from "../components.js";
import { clear, h } from "../dom.js";
import { dropThread, store } from "../store.js";
import { setTheme, themePreference } from "../theme.js";
import { confirmDialog, toast } from "../ui.js";

function readModel() {
  try { return localStorage.getItem("cinephile.model") || "auto"; } catch { return "auto"; }
}
function writeModel(value) {
  try { localStorage.setItem("cinephile.model", value); } catch { /* storage unavailable */ }
}

function panel(title, description, ...children) {
  return h("section", { class: "panel" }, h("div", { class: "row-text" }, h("h3", {}, title), description ? h("span", {}, description) : null), ...children);
}

export async function mountSettings(container, { setTitle, navigate }) {
  setTitle("Settings");
  let alive = true;
  const models = h("div", { class: "model-list" }, h("div", { class: "skeleton sk-row" }));
  const picker = h("select", { class: "select", id: "model-pick", "aria-label": "Assistant model", onChange: () => { writeModel(picker.value); toast("Model preference saved"); } },
    h("option", { value: "auto" }, "Automatic (recommended)"));

  const theme = segmented([["system", "System"], ["dark", "Dark"], ["light", "Light"]], themePreference(), setTheme, "Theme");

  const deleteAll = h("button", {
    class: "btn btn-danger", type: "button",
    onClick: async () => {
      if (!store.threads.length) { toast("There are no chats to delete."); return; }
      const ok = await confirmDialog({
        title: "Delete all chats?",
        message: `All ${store.threads.length} chats and their messages will be permanently removed. Your favorites, watch list and reviews are not affected.`,
        confirmLabel: "Delete all",
        danger: true,
      });
      if (!ok) return;
      let failed = 0;
      for (const thread of [...store.threads]) {
        try { await api.threads.remove(thread.id); dropThread(thread.id); } catch { failed++; }
      }
      toast(failed ? `${failed} chats could not be deleted.` : "All chats deleted", { error: failed > 0 });
      navigate("#/chat");
    },
  }, "Delete all chats");

  container.append(h("div", { class: "page" },
    h("header", { class: "page-head" }, h("h2", {}, "Settings"), h("p", {}, "Appearance, assistant and data.")),
    h("div", { class: "settings" },
      panel("Appearance", "Choose how Cinephile looks.", theme),
      panel("Assistant model", "Automatic tries the fastest model first and switches when one is out of quota.",
        h("div", { class: "field" }, h("label", { for: "model-pick" }, "Preferred model"), picker), models),
      panel("Your data", `${store.favorites.size} favorites, ${store.watching.size} on your watch list, ${store.reviews.size} reviews, ${store.threads.length} chats. Everything is stored on this computer.`, deleteAll),
    ),
  ));

  try {
    const health = await api.health();
    if (!alive) return () => {};
    for (const model of health.models) picker.append(h("option", { value: model.name }, model.name));
    picker.value = readModel();
    if (picker.value !== readModel()) picker.value = "auto";
    clear(models).append(...health.models.map((model) =>
      h("div", { class: "model" },
        h("span", {}, h("span", { class: model.available ? "dot" : "dot off", "aria-hidden": "true" }), model.name),
        h("span", { class: "pill" }, model.available ? "Available" : `Paused, retry in ${Math.max(1, Math.round(model.retry_in / 60))} min`),
      )));
    if (!health.agent_ready) models.prepend(h("p", { class: "label" }, "The assistant is not running. Check the server log."));
  } catch (error) {
    if (alive) clear(models).append(h("p", { class: "label" }, `Could not read model status: ${error.message}`));
  }
  return () => { alive = false; };
}
