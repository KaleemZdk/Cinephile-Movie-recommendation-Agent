// Reusable pieces: title cards, posters, rating display, loading and empty states.
import { h } from "./dom.js";
import { icon } from "./icons.js";
import { emit, on, keyOf, store, toggleFavorite, setWatchStatus } from "./store.js";

export const typeLabel = (item) => (item.media_type === "tv" ? "TV" : "Movie");

export function posterNode(item, extraClass = "") {
  const wrap = h("div", { class: `card-poster ${extraClass}`.trim() });
  const empty = () => h("div", { class: "poster-empty" }, icon("film"));
  if (item.poster_url) {
    const img = h("img", { src: item.poster_url, alt: "", loading: "lazy", decoding: "async" });
    img.addEventListener("error", () => { img.replaceWith(empty()); });
    wrap.append(img);
  } else {
    wrap.append(empty());
  }
  return wrap;
}

export function rateNode(rating) {
  const value = Number(rating);
  if (!value) return null;
  return h("span", { class: "rate" }, icon("star", { filled: true }), value.toFixed(1));
}

export function openTitle(item) {
  emit("open-title", item);
}

/** A poster card. opts: { why: string, footer: Node } */
export function titleCard(item, opts = {}) {
  const key = keyOf(item);
  const poster = posterNode(item);
  poster.append(h("span", { class: "badge" }, typeLabel(item)));
  const posterButton = h("button", {
    class: "card-poster-btn",
    type: "button",
    "aria-label": `Details for ${item.title}`,
    onClick: () => openTitle(item),
  }, poster);

  const meta = h("div", { class: "card-meta" }, item.year || null, rateNode(item.rating));
  const why = opts.why || (item.because_you_liked && item.because_you_liked.length ? `Because you liked ${item.because_you_liked[0]}` : null);

  return h("article", { class: "card", dataset: { key } },
    posterButton,
    h("div", { class: "card-body" },
      h("h3", { class: "card-title", title: item.title }, item.title),
      meta,
      item.genres && item.genres.length ? h("p", { class: "card-genres" }, item.genres.slice(0, 2).join(" / ")) : null,
      why ? h("p", { class: "card-why" }, why) : null,
    ),
    opts.footer || h("div", { class: "card-actions" },
      h("button", {
        class: "icon-btn icon-btn-sm", type: "button", dataset: { action: "favorite" },
        "aria-label": `Favorite ${item.title}`, "aria-pressed": String(store.favorites.has(key)), title: "Favorite",
        onClick: () => toggleFavorite(item),
      }, icon("heart")),
      h("button", {
        class: "icon-btn icon-btn-sm", type: "button", dataset: { action: "watch" },
        "aria-label": `Add ${item.title} to watch list`, "aria-pressed": String(store.watching.has(key)), title: "Watch list",
        onClick: () => setWatchStatus(item, store.watching.has(key) ? null : "plan"),
      }, icon("bookmark")),
      h("span", { class: "grow" }),
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onClick: () => openTitle(item) }, "Details"),
    ),
  );
}

/** Keep the heart and bookmark of every visible card in step with the library. */
export function syncCards(root = document) {
  for (const card of root.querySelectorAll(".card[data-key]")) {
    const key = card.dataset.key;
    const fav = card.querySelector('[data-action="favorite"]');
    const watch = card.querySelector('[data-action="watch"]');
    if (fav) fav.setAttribute("aria-pressed", String(store.favorites.has(key)));
    if (watch) watch.setAttribute("aria-pressed", String(store.watching.has(key)));
  }
}
on("library", () => syncCards());

export function skeletonGrid(count = 12) {
  return h("div", { class: "grid", "aria-hidden": "true" },
    Array.from({ length: count }, () =>
      h("div", { class: "card card-skeleton" },
        h("div", { class: "skeleton poster" }),
        h("div", { class: "skeleton line" }),
        h("div", { class: "skeleton line short" }),
      )),
  );
}

/** Centered message. opts: { icon, title, text, action: { label, onClick } } */
export function stateBlock({ iconName = "inbox", title, text, action }) {
  return h("div", { class: "state" },
    icon(iconName),
    h("h3", {}, title),
    text ? h("p", {}, text) : null,
    action ? h("button", { class: "btn btn-secondary", type: "button", onClick: action.onClick }, action.label) : null,
  );
}

export function errorState(message, retry) {
  return stateBlock({
    iconName: "alert",
    title: "Something went wrong",
    text: message,
    action: retry ? { label: "Try again", onClick: retry } : null,
  });
}

/** Pill-style choice group. options: [[value, label], ...]. Calls onChange(value). */
export function segmented(options, value, onChange, label) {
  const group = h("div", { class: "segmented", role: "group", "aria-label": label });
  for (const [optionValue, optionLabel] of options) {
    group.append(h("button", {
      type: "button",
      "aria-pressed": String(optionValue === value),
      onClick: (event) => {
        for (const button of group.children) button.setAttribute("aria-pressed", String(button === event.currentTarget));
        onChange(optionValue);
      },
    }, optionLabel));
  }
  return group;
}

/** Five-star display for a 0-10 rating. */
export function starsRow(rating) {
  const filled = Math.round(Number(rating) / 2);
  return h("span", { class: "stars", role: "img", "aria-label": `${rating} out of 10` },
    Array.from({ length: 5 }, (_, i) => h("span", { class: i < filled ? "" : "off" }, icon("star", { filled: true }))),
  );
}
