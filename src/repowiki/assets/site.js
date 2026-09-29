// repowiki: mobile menu and source line-range highlighting. No dependencies.
(function () {
  "use strict";
  var menu = document.querySelector(".menu");
  if (menu) {
    menu.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  function highlight() {
    var rows = document.querySelectorAll(".source tr.hl");
    for (var i = 0; i < rows.length; i++) rows[i].classList.remove("hl");
    var m = /^#L(\d+)(?:-L(\d+))?$/.exec(location.hash);
    if (!m) return;
    var a = parseInt(m[1], 10), b = parseInt(m[2] || m[1], 10), first = null;
    for (var n = a; n <= b && n - a < 5000; n++) {
      var row = document.getElementById("L" + n);
      if (!row) break;
      row.classList.add("hl");
      if (!first) first = row;
    }
    if (first) first.scrollIntoView({ block: "center" });
  }
  window.addEventListener("hashchange", highlight);
  document.addEventListener("DOMContentLoaded", highlight);
})();
