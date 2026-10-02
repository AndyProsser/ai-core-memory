/* Runs blocking in <head>, before first paint, so there is no flash of the wrong theme.
   Server preference (signed-in users) wins; otherwise the browser-local choice; otherwise follow the OS. */
(function () {
  var root = document.documentElement;
  var pref = root.getAttribute("data-pref") || "system";
  try {
    if (pref !== "system") localStorage.setItem("acm-theme", pref);
    else pref = localStorage.getItem("acm-theme") || "system";
  } catch (e) {}
  if (pref === "light" || pref === "dark") root.setAttribute("data-theme", pref);
  else root.removeAttribute("data-theme");
})();
