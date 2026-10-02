/* Theme toggle, quick-capture dialog, keyboard shortcuts. No dependencies. */
(function () {
  var root = document.documentElement;
  var csrf = (document.querySelector('meta[name="csrf"]') || {}).content || "";

  function applyTheme(t) {
    if (t === "light" || t === "dark") root.setAttribute("data-theme", t);
    else root.removeAttribute("data-theme");
    try { localStorage.setItem("acm-theme", t); } catch (e) {}
    document.querySelectorAll("[data-theme-set]").forEach(function (b) {
      b.setAttribute("aria-pressed", String(b.getAttribute("data-theme-set") === t));
    });
  }
  var current = root.getAttribute("data-pref") || "system";
  try { if (current === "system") current = localStorage.getItem("acm-theme") || "system"; } catch (e) {}
  applyTheme(current);
  document.querySelectorAll("[data-theme-set]").forEach(function (b) {
    b.addEventListener("click", function () {
      var t = b.getAttribute("data-theme-set");
      applyTheme(t);
      if (csrf) {
        var body = new URLSearchParams({ theme: t, csrf_token: csrf });
        fetch("/settings/theme", { method: "POST", body: body, headers: { "X-CSRF-Token": csrf }, redirect: "manual" }).catch(function () {});
      }
    });
  });

  var capture = document.getElementById("capture-dialog");
  var help = document.getElementById("help-dialog");
  function open(d) { if (d && !d.open) { d.showModal(); var f = d.querySelector("input,textarea"); if (f) f.focus(); } }
  document.querySelectorAll("[data-open-capture]").forEach(function (b) { b.addEventListener("click", function () { open(capture); }); });
  document.querySelectorAll("[data-close-dialog]").forEach(function (b) { b.addEventListener("click", function () { b.closest("dialog").close(); }); });

  var chord = null;
  document.addEventListener("keydown", function (e) {
    var t = e.target, tag = (t && t.tagName) || "";
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (t && t.isContentEditable)) return;
    if (document.querySelector("dialog[open]")) return;
    if (chord) {
      var dest = { m: "/memory", r: "/review", f: "/focus", d: "/data" }[e.key];
      chord = null;
      if (dest) { window.location.href = dest; e.preventDefault(); }
      return;
    }
    if (e.key === "g") { chord = true; setTimeout(function () { chord = null; }, 1200); }
    else if (e.key === "/") { var s = document.querySelector("[data-search]"); if (s) { s.focus(); e.preventDefault(); } }
    else if (e.key === "c") { open(capture); e.preventDefault(); }
    else if (e.key === "?") { open(help); e.preventDefault(); }
  });

  document.querySelectorAll("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = document.getElementById(b.getAttribute("data-copy"));
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent.trim()).then(function () {
        var old = b.textContent; b.textContent = "Copied"; setTimeout(function () { b.textContent = old; }, 1500);
      });
    });
  });
})();
