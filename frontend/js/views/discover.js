// Discover: search, trending, personal picks, and genre / rating filters.
import { api } from "../api.js";
import { errorState, segmented, skeletonGrid, stateBlock, titleCard } from "../components.js";
import { clear, debounce, h } from "../dom.js";
import { icon } from "../icons.js";
import { store } from "../store.js";

const SORTS = {
  movie: [["popularity.desc", "Most popular"], ["vote_average.desc", "Top rated"], ["primary_release_date.desc", "Newest"]],
  tv: [["popularity.desc", "Most popular"], ["vote_average.desc", "Top rated"], ["first_air_date.desc", "Newest"]],
};
const RATINGS = [["", "Any rating"], ["6", "6+ rating"], ["7", "7+ rating"], ["8", "8+ rating"]];
const MEDIA_NAMES = { all: "titles", movie: "movies", tv: "TV shows" };

function section(title, note, content) {
  return h("section", {}, h("div", { class: "section-head" }, h("h3", {}, title), note ? h("span", {}, note) : null), content);
}

const grid = (items) => h("div", { class: "grid" }, items.map((item) => titleCard(item)));

export async function mountDiscover(container, { setTitle }) {
  setTitle("Discover");
  const state = { q: "", media: "all", genre: "", sort: "popularity.desc", rating: "" };
  let requestId = 0;
  let controller = null;
  let alive = true;

  const results = h("div", { "aria-live": "polite" });
  const input = h("input", { class: "input", type: "search", placeholder: "Search movies and TV shows", "aria-label": "Search titles", autocomplete: "off" });
  const genre = h("select", { class: "select", "aria-label": "Genre", onChange: () => { state.genre = genre.value; refresh(); } });
  const sort = h("select", { class: "select", "aria-label": "Sort by", onChange: () => { state.sort = sort.value; refresh(); } });
  const rating = h("select", { class: "select", "aria-label": "Minimum rating", onChange: () => { state.rating = rating.value; refresh(); } },
    RATINGS.map(([value, label]) => h("option", { value }, label)));
  const filters = h("div", { class: "filters", hidden: true }, genre, sort, rating);

  const mediaControl = segmented([["all", "All"], ["movie", "Movies"], ["tv", "TV shows"]], state.media, (value) => {
    state.media = value;
    state.genre = "";
    state.sort = "popularity.desc";
    loadFilters();
    refresh();
  }, "Type");

  container.append(h("div", { class: "page" },
    h("header", { class: "page-head" }, h("h2", {}, "Discover"), h("p", {}, "Search TMDB, browse by genre, or see what is trending.")),
    h("div", { class: "toolbar" },
      h("div", { class: "search-box" }, icon("search"), input),
      mediaControl,
    ),
    filters,
    results,
  ));

  async function loadFilters() {
    const browsing = state.media !== "all" && !state.q;
    filters.hidden = !browsing;
    if (state.media === "all") return;
    sort.replaceChildren(...SORTS[state.media].map(([value, label]) => h("option", { value }, label)));
    genre.replaceChildren(h("option", { value: "" }, "All genres"));
    try {
      const { genres } = await api.genres(state.media);
      if (!alive) return;
      genre.append(...genres.map((name) => h("option", { value: name }, name)));
    } catch { /* filtering by genre is optional */ }
  }

  async function refresh() {
    if (controller) controller.abort();
    controller = new AbortController();
    const mine = ++requestId;
    const { signal } = controller;
    filters.hidden = !(state.media !== "all" && !state.q);
    clear(results).append(skeletonGrid(12));

    try {
      let view;
      if (state.q) {
        const { results: found } = await api.search(state.q, state.media, signal);
        view = found.length
          ? section(`Results for "${state.q}"`, `${found.length} ${MEDIA_NAMES[state.media]}`, grid(found))
          : stateBlock({ iconName: "search", title: "No matches", text: `Nothing found for "${state.q}". Try another spelling or fewer words.` });
      } else if (state.media === "all") {
        const wantsPicks = store.favorites.size + store.watching.size + store.reviews.size > 0;
        const [picks, trending] = await Promise.all([
          wantsPicks ? api.forYou().catch(() => null) : null,
          api.trending("all"),
        ]);
        view = h("div", { class: "stack" },
          picks && picks.results.length
            ? section("Picked for you", picks.seeds.length ? `Based on ${picks.seeds.slice(0, 2).join(" and ")}` : "", h("div", { class: "rail" }, picks.results.map((item) => titleCard(item))))
            : null,
          section("Trending this week", "", grid(trending.results)),
        );
      } else {
        const min = state.rating || (state.sort === "vote_average.desc" ? "7" : "");
        const { results: found } = await api.discover({
          media_type: state.media, genre: state.genre, sort_by: state.sort, min_rating: min,
        }, signal);
        const heading = `${state.genre ? `${state.genre} ` : ""}${MEDIA_NAMES[state.media]}`;
        view = found.length
          ? section(heading.charAt(0).toUpperCase() + heading.slice(1), `${found.length} results`, grid(found))
          : stateBlock({ iconName: "search", title: "Nothing matches these filters", text: "Try a lower rating or a different genre." });
      }
      if (mine !== requestId || !alive) return;
      clear(results).append(view);
    } catch (error) {
      if (error.name === "AbortError" || mine !== requestId || !alive) return;
      clear(results).append(errorState(error.message, refresh));
    }
  }

  const onInput = debounce(() => {
    state.q = input.value.trim();
    loadFilters();
    refresh();
  }, 350);
  input.addEventListener("input", onInput);
  input.addEventListener("keydown", (event) => { if (event.key === "Enter") { onInput.cancel(); state.q = input.value.trim(); refresh(); } });

  refresh();
  return () => { alive = false; onInput.cancel(); if (controller) controller.abort(); };
}
