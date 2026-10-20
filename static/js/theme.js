/* Dark and light theme. Sets data-theme on <html> before first paint, remembers the choice per browser. */
(function () {
  if (window.__triageThemeLoaded) return;   // the script was included twice, so skip the second copy
  window.__triageThemeLoaded = true;
  var KEY = "triage1_theme";
  var SUN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>';
  var MOON = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>';

  function saved() {
    try { var s = localStorage.getItem(KEY); if (s === "light" || s === "dark") return s; } catch (e) { /* storage blocked */ }
    return null;
  }
  function osTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  function current() {
    return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
  }
  function apply(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    var btns = document.querySelectorAll(".theme-toggle");
    for (var i = 0; i < btns.length; i++) paint(btns[i]);
    document.dispatchEvent(new CustomEvent("triage-theme"));
  }
  function paint(btn) {
    var light = current() === "light";
    var next = light ? "dark" : "light";
    btn.innerHTML = light ? MOON : SUN;
    btn.title = "Switch to " + next + " theme";
    btn.setAttribute("aria-label", btn.title);
  }

  apply(saved() || osTheme());

  window.TriageTheme = {
    mode: function () { return saved() || "system"; },
    set: function (m) {
      try {
        if (m === "light" || m === "dark") localStorage.setItem(KEY, m);
        else localStorage.removeItem(KEY);
      } catch (e) { /* ignore */ }
      apply(saved() || osTheme());
    }
  };

  function mount() {
    if (document.querySelector(".theme-toggle")) return;   // already added
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "theme-toggle";
    btn.onclick = function () {
      var next = current() === "light" ? "dark" : "light";
      try { localStorage.setItem(KEY, next); } catch (e) { /* ignore */ }
      apply(next);
    };
    var slot = document.getElementById("theme-slot");
    if (slot) { btn.classList.add("in-slot"); slot.appendChild(btn); }
    else document.body.appendChild(btn);
    paint(btn);
  }

  if (window.matchMedia) {
    var mq = window.matchMedia("(prefers-color-scheme: light)");
    var onOs = function () { if (!saved()) apply(osTheme()); };
    if (mq.addEventListener) mq.addEventListener("change", onOs);
  }
  window.addEventListener("storage", function (e) { if (e.key === KEY) apply(saved() || osTheme()); });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
})();