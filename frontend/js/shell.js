// Fills the static parts of the page (icons, nav labels) and then starts the app.
import { h } from "./dom.js";
import { icon } from "./icons.js";

const NAV = {
  navDiscover: ["compass", "Discover"],
  navFavorites: ["heart", "Favorites", "countFavorites"],
  navWatching: ["bookmark", "Watch list", "countWatching"],
  navReviews: ["star", "Reviews"],
  navSettings: ["sliders", "Settings"],
};

for (const [id, [iconName, label, countId]] of Object.entries(NAV)) {
  const link = document.getElementById(id);
  link.append(icon(iconName), h("span", {}, label));
  if (countId) link.append(h("span", { class: "nav-count", id: countId }));
}

document.getElementById("brandMark").append(icon("film"));
document.getElementById("newChat").prepend(icon("plus"));
document.getElementById("menuButton").append(icon("menu"));
document.getElementById("drawerClose").append(icon("x"));

await import("./app.js");
