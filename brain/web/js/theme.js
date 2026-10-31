// Runs before the page paints, so a chosen theme never flashes the wrong colours.
(function () {
  try {
    var saved = localStorage.getItem("brain.theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (_) {
    // Storage can be blocked. The system theme is a fine fallback.
  }
})();
