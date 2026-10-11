// Details drawer: opens from any card, loads full details, similar titles, and save controls.
import { api } from "./api.js";
import { errorState, posterNode, rateNode, typeLabel } from "./components.js";
import { clear, h } from "./dom.js";
import { icon } from "./icons.js";
import { openReviewModal } from "./review-modal.js";
import { STATUS_LABELS, emit, isFavorite, on, reviewRating, setWatchStatus, toggleFavorite, watchStatus } from "./store.js";

let drawer;
let body;
let scrim;
let closeButton;
let opener = null;
let token = 0;
let current = null;   // item currently shown
let controls = null;  // live controls kept in sync with the library

export function initDrawer() {
  drawer = document.getElementById("drawer");
  body = document.getElementById("drawerBody");
  scrim = document.getElementById("drawerScrim");
  closeButton = document.getElementById("drawerClose");

  on("open-title", openDetails);
  on("library", syncControls);
  closeButton.addEventListener("click", closeDetails);
  scrim.addEventListener("click", closeDetails);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && drawer.classList.contains("is-open") && !document.querySelector(".modal-backdrop")) closeDetails();
  });
}

export function closeDetails() {
  token++;
  drawer.classList.remove("is-open");
  scrim.classList.remove("is-visible");
  drawer.setAttribute("aria-hidden", "true");
  current = null;
  controls = null;
  if (opener && opener.isConnected) opener.focus();
  opener = null;
}

async function openDetails(item) {
  if (!drawer.classList.contains("is-open")) opener = document.activeElement;
  const mine = ++token;
  current = item;
  controls = null;
  drawer.classList.add("is-open");
  scrim.classList.add("is-visible");
  drawer.setAttribute("aria-hidden", "false");
  clear(body).append(h("div", { class: "detail detail-loading" }, h("div", { class: "skeleton sk-hero" }), h("div", { class: "skeleton sk-title" }), h("div", { class: "skeleton sk-text" })));
  closeButton.focus();

  try {
    const data = await api.title(item.media_type || "movie", item.tmdb_id ?? item.id);
    if (mine !== token) return;
    render(data);
    loadSimilar(data, mine);
  } catch (error) {
    if (mine !== token) return;
    clear(body).append(errorState(error.message, () => openDetails(item)));
  }
}

function chips(values) {
  return values && values.length ? h("div", { class: "chips" }, values.map((v) => h("span", { class: "pill" }, v))) : null;
}

function block(title, ...children) {
  const content = children.filter(Boolean);
  return content.length ? h("section", { class: "detail-block" }, h("h3", {}, title), content) : null;
}

function providers(info) {
  if (!info) return null;
  const groups = [["Stream", info.stream], ["Rent", info.rent], ["Buy", info.buy]].filter(([, list]) => list && list.length);
  if (!groups.length) {
    return block("Where to watch", h("p", {}, `No streaming information for ${info.region || "your region"} yet.`));
  }
  return block("Where to watch",
    groups.map(([label, list]) => h("div", { class: "provider-group" }, h("span", {}, label), list.slice(0, 6).map((name) => h("span", { class: "pill" }, name)))),
    info.link ? h("a", { href: info.link, target: "_blank", rel: "noopener noreferrer", class: "provider-group" }, `More on TMDB (${info.region})`, icon("externalLink")) : null,
  );
}

function render(data) {
  const item = { ...current, ...data, tmdb_id: data.tmdb_id, media_type: data.media_type };
  current = item;

  const meta = h("div", { class: "detail-meta" },
    h("span", { class: "pill pill-accent" }, typeLabel(item)),
    item.year ? h("span", {}, item.year) : null,
    item.media_type === "tv" && item.seasons ? h("span", {}, `${item.seasons} season${item.seasons === 1 ? "" : "s"}`) : null,
    item.media_type === "movie" && item.runtime ? h("span", {}, `${item.runtime} min`) : null,
    rateNode(item.rating),
  );

  const favorite = h("button", { class: "btn btn-secondary", type: "button", "aria-pressed": "false", onClick: () => toggleFavorite(item) }, icon("heart"), h("span", {}, "Favorite"));
  const status = h("select", { class: "select", "aria-label": "Watch list status", onChange: (event) => setWatchStatus(item, event.target.value || null) },
    h("option", { value: "" }, "Not on watch list"),
    Object.entries(STATUS_LABELS).map(([value, label]) => h("option", { value }, label)),
  );
  const review = h("button", { class: "btn btn-secondary", type: "button", onClick: () => openReviewModal(item) }, icon("star"), h("span", {}, "Write a review"));
  const trailer = item.trailer_url
    ? h("a", { class: "btn btn-secondary", href: item.trailer_url, target: "_blank", rel: "noopener noreferrer" }, icon("play"), "Trailer")
    : null;
  controls = { item, favorite, status, review };

  const people = [];
  if (item.director) people.push(h("p", {}, h("strong", {}, "Director: "), item.director));
  if (item.created_by && item.created_by.length) people.push(h("p", {}, h("strong", {}, "Created by: "), item.created_by.join(", ")));
  if (item.cast && item.cast.length) people.push(h("p", {}, h("strong", {}, "Cast: "), item.cast.slice(0, 8).join(", ")));

  const hero = h("div", { class: "hero" });
  const heroImage = item.backdrop_url || item.poster_url;
  if (heroImage) {
    const img = h("img", { src: heroImage, alt: "", decoding: "async" });
    img.addEventListener("error", () => img.remove());
    hero.append(img);
  }

  clear(body).append(
    hero,
    h("div", { class: "detail has-hero" },
      h("h2", {}, item.title),
      item.tagline ? h("p", { class: "tagline" }, item.tagline) : null,
      meta,
      chips(item.genres),
      h("div", { class: "detail-actions" }, favorite, review, h("div", { class: "span-2" }, status), trailer ? h("div", { class: "span-2" }, trailer) : null),
      item.overview ? h("p", { class: "overview" }, item.overview) : null,
      block("People", people),
      providers(item.where_to_watch),
      chips(item.keywords) ? block("Themes", chips(item.keywords.slice(0, 8))) : null,
      h("section", { class: "detail-block", id: "similarBlock", hidden: true }, h("h3", {}, "More like this"), h("div", { class: "rail compact", id: "similarRail" })),
    ),
  );
  syncControls();
}

function syncControls() {
  if (!controls) return;
  const { item, favorite, status, review } = controls;
  const saved = isFavorite(item);
  favorite.setAttribute("aria-pressed", String(saved));
  favorite.querySelector("span:last-child").textContent = saved ? "Saved" : "Favorite";
  status.value = watchStatus(item) || "";
  const rating = reviewRating(item);
  review.querySelector("span:last-child").textContent = rating != null ? `Your review: ${rating}/10` : "Write a review";
}

async function loadSimilar(data, mine) {
  try {
    const { results } = await api.similar(data.media_type, data.tmdb_id);
    if (mine !== token || !results.length) return;
    const rail = document.getElementById("similarRail");
    const section = document.getElementById("similarBlock");
    if (!rail) return;
    for (const similar of results.slice(0, 10)) {
      const poster = posterNode(similar);
      rail.append(h("button", { class: "mini", type: "button", "aria-label": `Details for ${similar.title}`, onClick: () => emit("open-title", similar) }, poster, h("strong", {}, similar.title)));
    }
    section.hidden = false;
  } catch { /* similar titles are optional */ }
}
