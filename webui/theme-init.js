  // Applies a saved manual theme choice before first paint (a flash of the
  // wrong theme, however brief, would otherwise be visible every launch) --
  // must run synchronously here, not from app.js, which only loads/executes
  // after the page has already rendered once. "system" (default) leaves no
  // attribute, so app.css's @media (prefers-color-scheme) block alone
  // decides -- matches this having no saved preference yet.
  (function () {
    try {
      var saved = localStorage.getItem("ahb-theme");
      if (saved === "light" || saved === "dark") {
        document.documentElement.setAttribute("data-theme", saved);
      }
    } catch (err) {
      // localStorage can throw (private mode, disabled storage) -- fall
      // back to the system theme silently rather than breaking the page.
    }
  })();
