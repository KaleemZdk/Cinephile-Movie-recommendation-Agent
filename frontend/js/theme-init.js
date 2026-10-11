// Runs before the page paints so the chosen theme never flashes.
(function () {
  var pref = "system";
  try { pref = localStorage.getItem("cinephile.theme") || "system"; } catch (e) { /* storage unavailable */ }
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.setAttribute("data-theme", pref === "system" ? (dark ? "dark" : "light") : pref);
})();
