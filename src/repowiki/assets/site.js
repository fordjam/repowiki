// repowiki: mobile menu, source line-range highlighting, page search, theme persistence and the
// portal hooks (logout). No dependencies. Every control here is idempotent: running this script
// again never doubles a listener or a control.
(function () {
  "use strict";
  if (document.documentElement.hasAttribute("data-rw-ready")) return;
  document.documentElement.setAttribute("data-rw-ready", "");

  var THEME_KEY = "repowiki-theme";
  var THEME_EVENT = "repowiki:theme-change";
  var generation = 0;

  // ---- theme: persisted preference, emitted as the portal theme event -----------------------
  function storedTheme() {
    try {
      var t = localStorage.getItem(THEME_KEY);
      return t === "dark" || t === "light" ? t : null;
    } catch (e) {
      return null;
    }
  }

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-rw-theme", theme);
  }

  function emitTheme(theme) {
    applyTheme(theme);
    generation += 1;
    window.dispatchEvent(new CustomEvent(THEME_EVENT, { detail: { theme: theme, generation: generation } }));
  }

  window.repowiki = {
    // The portal (or a reader control) sets the theme through here: it persists and is announced.
    setTheme: function (theme) {
      if (theme !== "dark" && theme !== "light") return;
      try {
        localStorage.setItem(THEME_KEY, theme);
      } catch (e) {}
      emitTheme(theme);
    },
  };

  // The portal may move the theme itself: keep the attribute and storage in step. The announcer of
  // that event is responsible for rendering; this must not re-emit it.
  window.addEventListener(THEME_EVENT, function (e) {
    var t = e && e.detail && e.detail.theme;
    if ((t === "dark" || t === "light") && document.documentElement.getAttribute("data-rw-theme") !== t) {
      try {
        localStorage.setItem(THEME_KEY, t);
      } catch (err) {}
      applyTheme(t);
    }
  });

  // Applied early (no flash), announced on load so scripts registered meanwhile also hear it: the
  // chosen theme survives a reload.
  var early = storedTheme();
  if (early) applyTheme(early);
  window.addEventListener("load", function () {
    var t = storedTheme();
    if (t) emitTheme(t);
  });
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  if (media && media.addEventListener) {
    media.addEventListener("change", function (e) {
      if (!storedTheme()) emitTheme(e.matches ? "dark" : "light");
    });
  }

  // ---- mobile menu -------------------------------------------------------------------------
  var menu = document.querySelector(".menu");
  if (menu) {
    menu.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      menu.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  // ---- source line-range highlighting -------------------------------------------------------
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

  // ---- page search over the bounded, repo-scoped index --------------------------------------
  function initSearch() {
    var input = document.querySelector(".portal-search");
    var dataEl = document.getElementById("repowiki-search");
    var box = document.querySelector(".search-results");
    if (!input || !dataEl || !box) return;
    var pages;
    try {
      // The index is the text of a <template>: it lives in the template's content fragment, and the
      // element's own textContent is empty.
      var holder = dataEl.content || dataEl;
      pages = JSON.parse(holder.textContent).pages || [];
    } catch (e) {
      input.disabled = true;
      input.placeholder = "Search is unavailable";
      return;
    }
    function matches(q) {
      var out = [];
      for (var i = 0; i < pages.length && out.length < 12; i++) {
        var m = pages[i];
        if ((m.t + " " + m.r).toLowerCase().indexOf(q) !== -1) out.push(m);
      }
      return out;
    }
    function show(list) {
      box.textContent = "";
      for (var i = 0; i < list.length; i++) {
        var a = document.createElement("a");
        a.href = list[i].u;
        a.textContent = list[i].r + " / " + list[i].t;
        box.appendChild(a);
      }
      box.hidden = list.length === 0;
    }
    function hide() {
      box.hidden = true;
    }
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      if (!q) return hide();
      show(matches(q));
    });
    input.addEventListener("keydown", function (e) {
      var first = box.hidden ? null : box.querySelector("a");
      if ((e.key === "Enter" || e.key === "ArrowDown") && first) {
        e.preventDefault();
        window.location.href = first.href; // selection navigates, scoped to the entry's repository
      }
      if (e.key === "Escape") hide();
    });
    document.addEventListener("click", function (e) {
      if (e.target !== input && !box.contains(e.target)) hide();
    });
  }
  initSearch();

  // ---- structured POST logout: same-origin action, runtime token, never a baked session token --
  // With a token endpoint, the POST is only sent carrying a token that endpoint just issued; any
  // failure (token or logout) is shown beside the button instead of passing silently.
  function initLogout() {
    var button = document.querySelector(".portal-logout");
    if (!button) return;
    var status = document.querySelector(".portal-logout-status");
    var busy = false;

    function report(message) {
      busy = false;
      if (button.removeAttribute) button.removeAttribute("aria-busy");
      if (status) {
        status.textContent = message;
        status.hidden = !message;
      }
    }

    function fetchToken(tokenUrl) {
      return fetch(tokenUrl, { credentials: "same-origin", headers: { Accept: "application/json" } })
        .then(
          function (r) {
            if (!r || !r.ok) throw new Error("the token request was refused" + (r ? " (" + r.status + ")" : ""));
            return r.json().catch(function () {
              throw new Error("the token response was not JSON");
            });
          },
          function () {
            throw new Error("the token could not be fetched");
          }
        )
        .then(function (data) {
          var token = data && data.token;
          if (typeof token !== "string" || !token.trim()) throw new Error("the token response carried no token");
          return token;
        });
    }

    button.addEventListener("click", function () {
      if (busy) return;
      var action = button.getAttribute("data-action");
      var tokenUrl = button.getAttribute("data-token-endpoint");
      if (!action || action.indexOf("/") !== 0 || action.indexOf("//") === 0) {
        report("Log out failed: this page has no valid logout action.");
        return;
      }
      if (tokenUrl !== null && (tokenUrl.indexOf("/") !== 0 || tokenUrl.indexOf("//") === 0)) {
        report("Log out failed: this page has no valid token endpoint.");
        return;
      }
      busy = true;
      if (button.setAttribute) button.setAttribute("aria-busy", "true");
      if (status) status.hidden = true;
      var ready = tokenUrl ? fetchToken(tokenUrl) : Promise.resolve(null);
      ready
        .then(
          function (token) {
            var headers = token ? { "X-CSRF-Token": token } : {};
            return fetch(action, { method: "POST", credentials: "same-origin", headers: headers }).then(
              function (r) {
                // 401: the session is already gone, which is what logging out asks for.
                if (r && (r.ok || r.status === 401)) {
                  report("");
                  window.location.reload();
                  return;
                }
                report("Log out failed: the server refused it" + (r ? " (" + r.status + ")" : "") + ". You are still signed in.");
              },
              function () {
                report("Log out failed: the server could not be reached. You are still signed in.");
              }
            );
          },
          function (err) {
            report("Log out failed: " + err.message + ". Nothing was sent; you are still signed in.");
          }
        );
    });
  }
  initLogout();
})();
