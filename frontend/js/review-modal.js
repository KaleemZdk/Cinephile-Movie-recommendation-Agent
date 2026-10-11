// Review editor: 10-star rating, a number field for precision and keyboard use, and a comment.
import { api } from "./api.js";
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { deleteReview, reviewRating, saveReview } from "./store.js";
import { confirmDialog, openModal, toast } from "./ui.js";

export async function openReviewModal(item) {
  let existing = null;
  if (reviewRating(item) != null) {
    try {
      existing = await api.reviews.get(item);
    } catch { /* treat as a new review */ }
  }

  let rating = existing ? Number(existing.rating) : 0;
  const readout = h("span", { class: "rating-readout" });
  const number = h("input", { class: "input", id: "review-number", type: "number", min: "0", max: "10", step: "0.5" });
  const stars = Array.from({ length: 10 }, (_, i) =>
    h("button", { type: "button", "aria-label": `Rate ${i + 1} out of 10`, onClick: () => setRating(i + 1) }, icon("star", { filled: true })));
  const comment = h("textarea", { class: "textarea", id: "review-comment", rows: "4", maxlength: "2000", placeholder: "What did you think? (optional)" });
  comment.value = existing?.comment || "";

  function setRating(value, { fromField = false } = {}) {
    rating = Math.max(0, Math.min(10, Number(value) || 0));
    stars.forEach((star, i) => star.classList.toggle("on", i < Math.round(rating)));
    readout.textContent = rating ? `${rating} / 10` : "Not rated yet";
    if (!fromField) number.value = rating || "";
  }
  number.addEventListener("input", () => setRating(number.value, { fromField: true }));
  setRating(rating);

  const content = h("div", { class: "review-fields" },
    h("p", {}, item.title, item.year ? ` (${item.year})` : ""),
    h("div", { class: "field" },
      h("span", { class: "label" }, "Your rating"),
      h("div", { class: "star-input" }, stars),
      h("div", { class: "inline-field" },
        h("label", { class: "sr-only", for: "review-number" }, "Rating from 0 to 10"), number, readout),
    ),
    h("div", { class: "field" }, h("label", { for: "review-comment" }, "Your thoughts"), comment),
  );

  const actions = [{ label: "Cancel", kind: "secondary", onClick: (close) => close() }];
  if (existing) {
    actions.push({
      label: "Delete",
      kind: "danger",
      onClick: async (close) => {
        if (!(await confirmDialog({ title: "Delete review?", message: `Your review of ${item.title} will be removed.`, confirmLabel: "Delete", danger: true }))) return;
        try { await deleteReview(item); close(); } catch (error) { toast(error.message, { error: true }); }
      },
    });
  }
  actions.push({
    label: "Save review",
    kind: "primary",
    onClick: async (close) => {
      if (!rating) { toast("Choose a rating first.", { error: true }); return; }
      try { await saveReview(item, rating, comment.value.trim()); close(); } catch (error) { toast(error.message, { error: true }); }
    },
  });

  openModal({ title: existing ? "Edit your review" : "Write a review", content, actions });
}
