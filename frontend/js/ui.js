// Toasts and accessible modal dialogs.
import { h } from "./dom.js";
import { icon } from "./icons.js";

export function toast(message, { error = false } = {}) {
  const root = document.getElementById("toasts");
  if (!root) return;
  while (root.children.length >= 3) root.firstChild.remove();
  const el = h("div", { class: error ? "toast is-error" : "toast", role: error ? "alert" : "status" }, icon(error ? "alert" : "check"), h("span", {}, message));
  root.append(el);
  setTimeout(() => el.remove(), error ? 6000 : 3200);
}

const FOCUSABLE = 'a[href], button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex]:not([tabindex="-1"])';
let modalCount = 0;

/**
 * Open a modal. actions: [{ label, kind: "primary" | "secondary" | "danger", onClick(close), autofocus }]
 * Returns { close, modal }.
 */
export function openModal({ title, content, actions = [], onClose }) {
  const previous = document.activeElement;
  const titleId = `modal-title-${++modalCount}`;
  const backdrop = h("div", { class: "modal-backdrop" });

  const buttons = actions.map((action) =>
    h("button", {
      class: `btn btn-${action.kind || "secondary"}`,
      type: "button",
      "data-autofocus": action.autofocus ? "" : null,
      onClick: () => action.onClick(close),
    }, action.label),
  );
  const modal = h("div", { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": titleId },
    h("h2", { id: titleId }, title),
    content,
    buttons.length ? h("div", { class: "modal-actions" }, buttons) : null,
  );
  backdrop.append(modal);
  document.body.append(backdrop);

  function onKey(event) {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    } else if (event.key === "Tab") {
      const items = [...modal.querySelectorAll(FOCUSABLE)];
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  }
  function close() {
    document.removeEventListener("keydown", onKey, true);
    backdrop.remove();
    if (previous && previous.focus) previous.focus();
    if (onClose) onClose();
  }
  document.addEventListener("keydown", onKey, true);
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop) close(); });

  const target = modal.querySelector("[data-autofocus]") || modal.querySelector(FOCUSABLE);
  if (target) target.focus();
  return { close, modal };
}

/** Resolves true if the user confirms. */
export function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false }) {
  return new Promise((resolve) => {
    let answered = false;
    const answer = (value, close) => { answered = true; resolve(value); close(); };
    openModal({
      title,
      content: h("p", {}, message),
      actions: [
        { label: "Cancel", kind: "secondary", onClick: (close) => answer(false, close) },
        { label: confirmLabel, kind: danger ? "danger" : "primary", autofocus: true, onClick: (close) => answer(true, close) },
      ],
      onClose: () => { if (!answered) resolve(false); },
    });
  });
}

/** Resolves the entered text, or null if cancelled. */
export function promptDialog({ title, label, value = "", confirmLabel = "Save", maxLength = 80 }) {
  return new Promise((resolve) => {
    let answered = false;
    const input = h("input", { class: "input", id: "prompt-input", type: "text", value, maxlength: maxLength, "data-autofocus": "" });
    const submit = (close) => {
      const text = input.value.trim();
      if (!text) return;
      answered = true;
      resolve(text);
      close();
    };
    const { close } = openModal({
      title,
      content: h("div", { class: "field" }, h("label", { for: "prompt-input" }, label), input),
      actions: [
        { label: "Cancel", kind: "secondary", onClick: (c) => { answered = true; resolve(null); c(); } },
        { label: confirmLabel, kind: "primary", onClick: submit },
      ],
      onClose: () => { if (!answered) resolve(null); },
    });
    input.addEventListener("keydown", (event) => { if (event.key === "Enter") submit(close); });
    input.select();
  });
}
