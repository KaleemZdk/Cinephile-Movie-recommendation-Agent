// Library pages: Favorites, Watch list, Reviews.
import { api } from "../api.js";
import { errorState, openTitle, posterNode, skeletonGrid, starsRow, stateBlock, titleCard } from "../components.js";
import { clear, formatDate, h } from "../dom.js";
import { icon } from "../icons.js";
import { openReviewModal } from "../review-modal.js";
import { STATUS_LABELS, deleteReview, on, setWatchStatus, store } from "../store.js";
import { confirmDialog, toast } from "../ui.js";

function page(title, subtitle) {
  const body = h("div", {});
  const count = h("p", {}, subtitle);
  const root = h("div", { class: "page" }, h("header", { class: "page-head" }, h("h2", {}, title), count), body);
  return { root, body, count };
}

const discoverAction = { label: "Find something to watch", onClick: () => { location.hash = "#/discover"; } };

// ---------------------------------------------------------------- favorites

export async function mountFavorites(container, { setTitle }) {
  setTitle("Favorites");
  const { root, body, count } = page("Favorites", "Titles you have saved.");
  container.append(root);
  body.append(skeletonGrid(8));
  let alive = true;

  async function load() {
    try {
      const items = await api.favorites.list();
      if (!alive) return;
      count.textContent = items.length ? `${items.length} saved ${items.length === 1 ? "title" : "titles"}` : "Titles you have saved.";
      clear(body).append(items.length
        ? h("div", { class: "grid" }, items.map((item) => titleCard(item)))
        : stateBlock({ iconName: "heart", title: "No favorites yet", text: "Tap the heart on any title, or ask Cinephile to save one for you.", action: discoverAction }));
    } catch (error) {
      if (alive) clear(body).append(errorState(error.message, load));
    }
  }

  const off = on("library", () => {
    for (const card of body.querySelectorAll(".card[data-key]")) card.classList.toggle("is-faded", !store.favorites.has(card.dataset.key));
  });
  load();
  return () => { alive = false; off(); };
}

// ----------------------------------------------------------------- watching

const TABS = [["all", "All"], ["watching", "Watching"], ["plan", "Plan to watch"], ["watched", "Watched"]];

export async function mountWatching(container, { setTitle }) {
  setTitle("Watch list");
  const { root, body } = page("Watch list", "What you are watching, and what is next.");
  container.append(root);
  body.append(skeletonGrid(8));
  let alive = true;
  let items = [];
  let tab = "all";

  function render() {
    const counts = Object.fromEntries(TABS.map(([key]) => [key, key === "all" ? items.length : items.filter((i) => i.status === key).length]));
    const shown = tab === "all" ? items : items.filter((i) => i.status === tab);
    const tabs = h("div", { class: "tabs", role: "group", "aria-label": "Filter by status" }, TABS.map(([key, label]) =>
      h("button", {
        class: "btn btn-ghost btn-sm tab", type: "button", "aria-pressed": String(key === tab),
        onClick: () => { tab = key; render(); },
      }, label, h("span", { class: "tab-count" }, counts[key]))));

    let content;
    if (!items.length) {
      content = stateBlock({ iconName: "bookmark", title: "Your watch list is empty", text: "Add titles you plan to watch, or tell Cinephile what you are watching now.", action: discoverAction });
    } else if (!shown.length) {
      content = stateBlock({ iconName: "bookmark", title: "Nothing here yet", text: "Titles with this status will appear in this tab." });
    } else {
      content = h("div", { class: "grid" }, shown.map((item) => {
        const select = h("select", {
          class: "select select-sm", "aria-label": `Status for ${item.title}`,
          onChange: async (event) => {
            const value = event.target.value;
            await setWatchStatus(item, value || null);
            if (!alive) return;
            if (!value) items = items.filter((i) => i !== item); else item.status = value;
            render();
          },
        }, Object.entries(STATUS_LABELS).map(([value, label]) => h("option", { value }, label)), h("option", { value: "" }, "Remove from list"));
        select.value = item.status;
        return titleCard(item, { footer: h("div", { class: "card-select" }, select) });
      }));
    }
    clear(body).append(h("div", { class: "stack" }, tabs, content));
  }

  async function load() {
    try {
      items = await api.watching.list();
      if (alive) render();
    } catch (error) {
      if (alive) clear(body).append(errorState(error.message, load));
    }
  }
  load();
  return () => { alive = false; };
}

// ------------------------------------------------------------------ reviews

export async function mountReviews(container, { setTitle }) {
  setTitle("Reviews");
  const { root, body, count } = page("Reviews", "Your ratings and notes.");
  container.append(root);
  body.append(skeletonGrid(4));
  let alive = true;

  function row(review) {
    const poster = h("button", { class: "card-poster-btn", type: "button", "aria-label": `Details for ${review.title}`, onClick: () => openTitle(review) }, posterNode(review));
    return h("article", { class: "review" },
      poster,
      h("div", { class: "review-main" },
        h("h3", {}, review.title, review.year ? ` (${review.year})` : ""),
        h("div", { class: "review-score" }, starsRow(review.rating), `${Number(review.rating).toFixed(1)}/10`),
        review.comment ? h("p", {}, review.comment) : h("p", {}, "No written review."),
        h("span", { class: "review-date" }, formatDate(review.updated_at || review.created_at)),
      ),
      h("div", { class: "review-tools" },
        h("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Edit review of ${review.title}`, title: "Edit", onClick: () => openReviewModal(review) }, icon("pencil")),
        h("button", {
          class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Delete review of ${review.title}`, title: "Delete",
          onClick: async () => {
            const ok = await confirmDialog({ title: "Delete review?", message: `Your review of ${review.title} will be removed.`, confirmLabel: "Delete", danger: true });
            if (!ok) return;
            try { await deleteReview(review); } catch (error) { toast(error.message, { error: true }); }
          },
        }, icon("trash")),
      ),
    );
  }

  async function load() {
    try {
      const reviews = await api.reviews.list();
      if (!alive) return;
      count.textContent = reviews.length ? `${reviews.length} ${reviews.length === 1 ? "review" : "reviews"}` : "Your ratings and notes.";
      clear(body).append(reviews.length
        ? h("div", { class: "review-list" }, reviews.map(row))
        : stateBlock({ iconName: "star", title: "No reviews yet", text: "Open any title and choose Write a review, or tell Cinephile how you rated something.", action: discoverAction }));
    } catch (error) {
      if (alive) clear(body).append(errorState(error.message, load));
    }
  }
  const off = on("reviews", load);
  load();
  return () => { alive = false; off(); };
}
