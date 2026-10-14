// Render the pinned Mermaid diagrams on a page under a strict CSP (style-src 'self').
// The bundle is served from the site itself (assets/vendor/mermaid.min.js) and initialized
// externally with startOnLoad disabled.
//
// Such a CSP refuses every inline style: a <style> element, and a style attribute whether it comes
// from markup, from setAttribute, or from parsing a string (even in a DOMParser document). Mermaid
// does all three while it renders, so each render runs inside an isolation scope that lasts exactly
// as long as the render:
//   - Mermaid's <style> element is created as an inert element; its CSS goes to a constructed
//     stylesheet (adoptedStyleSheets), which CSP does not govern, so text is still measured right.
//   - style attributes set by script go through CSSOM (element.style.cssText), which CSP allows.
//   - markup parsed by innerHTML or DOMParser has style attributes and <style> tags renamed first,
//     so the parser never meets an inline style; innerHTML content gets its styles back via CSSOM.
//   - Mermaid draws into an off-screen container this script owns (it needs layout to measure
//     text, so the container sits in the document), and that container is removed when the render
//     ends, even when Mermaid throws: an error diagram never stays behind.
// The returned SVG is mounted the same way: inline styles re-applied through CSSOM, CSS adopted.
//
// Rerendering is generation-guarded and driven by the portal's "repowiki:theme-change" event
// carrying {theme, generation}: a completion from an older generation is discarded, so delayed
// overlapping renders retain only the newest theme.
(function () {
  "use strict";
  if (!window.mermaid) return;
  var THEME_EVENT = "repowiki:theme-change";
  var INERT_NS = "urn:x-repowiki:inert";
  var KEPT = "data-rw-style";

  var blocks = [];
  var latest = 0;
  var sheet = null;
  var sheetCss = "";

  function addCss(text) {
    if (!text || !("adoptedStyleSheets" in document) || !window.CSSStyleSheet) return;
    if (!sheet) {
      sheet = new CSSStyleSheet();
      document.adoptedStyleSheets = document.adoptedStyleSheets.concat([sheet]);
    }
    sheetCss += "\n" + text;
    sheet.replaceSync(sheetCss);
  }

  // ------------------------------------------------------------------ isolation scope

  // Serialized markup always quotes attribute values, so a tag and its attributes tokenize exactly.
  var TAG = /<(\/?)([A-Za-z][^\s\/>]*)((?:\s+[^\s"'>\/=]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)(\s*\/?)>/g;
  var ATTR = /(\s+)([^\s"'>\/=]+)(\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?/g;

  function defuse(markup) {
    return String(markup).replace(TAG, function (m, close, name, attrs, end) {
      var tag = name.toLowerCase() === "style" ? "rw-style" : name;
      var kept = attrs.replace(ATTR, function (a, space, attr, value) {
        return space + (attr.toLowerCase() === "style" ? KEPT : attr) + (value || "");
      });
      return "<" + close + tag + kept + end + ">";
    });
  }

  function restyle(root) {
    if (!root || !root.querySelectorAll) return;
    var found = Array.prototype.slice.call(root.querySelectorAll("[" + KEPT + "]"));
    if (root.hasAttribute && root.hasAttribute(KEPT)) found.unshift(root);
    found.forEach(function (el) {
      if (el.style) el.style.cssText = el.getAttribute(KEPT);
      el.removeAttribute(KEPT);
    });
    Array.prototype.slice.call(root.querySelectorAll("rw-style")).forEach(function (el) {
      addCss(el.textContent);
      el.remove();
    });
  }

  var depth = 0;
  var saved = null;

  function inertStyle(create) {
    var el = create.call(document, INERT_NS, "rw-style");
    var text = "";
    var keep = function (value) {
      text = String(value);
      addCss(text);
    };
    ["innerHTML", "textContent"].forEach(function (prop) {
      Object.defineProperty(el, prop, { configurable: true, get: function () { return text; }, set: keep });
    });
    return el;
  }

  function isolate() {
    if (depth++) return;
    var E = window.Element && window.Element.prototype;
    var inner = E && Object.getOwnPropertyDescriptor(E, "innerHTML");
    var P = window.DOMParser && window.DOMParser.prototype;
    if (!E || !inner || !inner.set || !P || !document.createElementNS) return; // nothing to shield
    saved = {
      E: E, inner: inner, P: P, parse: P.parseFromString, setAttribute: E.setAttribute,
      setAttributeNS: E.setAttributeNS, createElement: document.createElement,
      createElementNS: document.createElementNS,
    };
    var s = saved;
    E.setAttribute = function (name, value) {
      if (String(name).toLowerCase() === "style" && this.style) {
        this.style.cssText = String(value);
        return;
      }
      return s.setAttribute.call(this, name, value);
    };
    E.setAttributeNS = function (ns, name, value) {
      if (!ns && String(name).toLowerCase() === "style" && this.style) {
        this.style.cssText = String(value);
        return;
      }
      return s.setAttributeNS.call(this, ns, name, value);
    };
    Object.defineProperty(E, "innerHTML", {
      configurable: true,
      enumerable: inner.enumerable,
      get: inner.get,
      set: function (value) {
        inner.set.call(this, defuse(value));
        restyle(this);
      },
    });
    P.parseFromString = function (markup, type) {
      return s.parse.call(this, defuse(markup), type);
    };
    document.createElement = function (tag, options) {
      if (String(tag).toLowerCase() === "style") return inertStyle(s.createElementNS);
      return s.createElement.call(document, tag, options);
    };
    document.createElementNS = function (ns, tag, options) {
      if (ns !== INERT_NS && String(tag).toLowerCase() === "style") return inertStyle(s.createElementNS);
      return s.createElementNS.call(document, ns, tag, options);
    };
  }

  function release() {
    if (--depth || !saved) return;
    var s = saved;
    saved = null;
    s.E.setAttribute = s.setAttribute;
    s.E.setAttributeNS = s.setAttributeNS;
    Object.defineProperty(s.E, "innerHTML", s.inner);
    s.P.parseFromString = s.parse;
    delete document.createElement;
    delete document.createElementNS;
  }

  // One render in its own off-screen container, removed afterwards whatever Mermaid did.
  function renderIsolated(id, source) {
    var box = document.createElement("div");
    box.setAttribute("data-repowiki-render", id); // present exactly while this render is in flight
    box.style.cssText = "position:absolute;left:-10000px;top:0;width:1200px;visibility:hidden";
    document.body.appendChild(box);
    isolate();
    var done = function () {
      release();
      box.remove();
    };
    var pending;
    try {
      pending = Promise.resolve(window.mermaid.render(id, source, box));
    } catch (err) {
      done();
      return Promise.reject(err);
    }
    return pending.then(
      function (out) {
        done();
        return out;
      },
      function (err) {
        done();
        throw err;
      }
    );
  }

  // ------------------------------------------------------------------ rendering

  // Labels must stay readable in both themes: light ink on a dark ground and the reverse.
  function palette(dark, font) {
    return dark
      ? {
          darkMode: true, background: "#0d1117", textColor: "#e6edf3", lineColor: "#9198a1",
          mainBkg: "#1c2129", nodeBorder: "#30363d", edgeLabelBackground: "#0d1117",
          clusterBkg: "#151b23", clusterBorder: "#30363d", fontFamily: font, fontSize: "14px",
        }
      : {
          darkMode: false, background: "#ffffff", textColor: "#1f2328", lineColor: "#656d76",
          mainBkg: "#f6f8fa", nodeBorder: "#d8dee4", edgeLabelBackground: "#ffffff",
          clusterBkg: "#f6f8fa", clusterBorder: "#d8dee4", fontFamily: font, fontSize: "14px",
        };
  }

  function mountSvg(item, svgText) {
    // Parse with inline styles already renamed, so even this inert parse meets none.
    var doc = new DOMParser().parseFromString("<!doctype html><body>" + defuse(svgText), "text/html");
    var svg = doc.body.querySelector("svg");
    if (!svg) throw new Error("no svg");
    var css = [];
    svg.querySelectorAll("style, rw-style").forEach(function (s) {
      css.push(s.textContent);
      s.remove();
    });
    var styled = [];
    [svg].concat(Array.prototype.slice.call(svg.querySelectorAll("[style], [" + KEPT + "]"))).forEach(function (el) {
      var value = el.hasAttribute(KEPT) ? el.getAttribute(KEPT) : el.getAttribute("style");
      if (value === null) return;
      styled.push(value);
      el.removeAttribute("style");
      el.removeAttribute(KEPT);
      el.setAttribute("data-rw-index", String(styled.length - 1));
    });
    Array.prototype.slice.call(svg.childNodes || []).forEach(function (node) {
      if (node.nodeType === 3) node.remove(); // stray CSS text left where the inert element was
    });
    addCss(css.join("\n"));
    var box = document.createElement("div");
    box.className = "mermaid-diagram";
    box.appendChild(document.importNode(svg, true));
    box.querySelectorAll("[data-rw-index]").forEach(function (el) {
      el.style.cssText = styled[Number(el.getAttribute("data-rw-index"))];
      el.removeAttribute("data-rw-index");
    });
    item.el.replaceWith(box);
    item.el = box; // the next pass replaces this box: one diagram per block, never a pile
  }

  function renderAll(theme, eventGeneration) {
    latest = typeof eventGeneration === "number" ? eventGeneration : latest + 1;
    var gen = latest;
    var dark = theme === "dark" ? true : theme === "light" ? false
      : !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);  // fmt: skip
    var font = window.getComputedStyle(document.body).fontFamily;
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",
      theme: "base",
      htmlLabels: false,
      flowchart: { htmlLabels: false },
      themeVariables: palette(dark, font),
    });
    sheetCss = "";
    if (sheet) sheet.replaceSync(sheetCss);
    blocks.forEach(function (item, i) {
      renderIsolated("repowiki-mermaid-" + gen + "-" + i, item.source)
        .then(function (out) {
          if (gen !== latest) return; // a newer theme switch took over: discard this completion
          mountSvg(item, out.svg);
        })
        .catch(function () {
          if (gen !== latest) return;
          // A malformed diagram must not break the page: mark the block, keep its source readable.
          item.el.classList.add("mermaid-error");
          item.el.title = "Diagram could not be rendered";
        });
    });
  }

  function start() {
    document.querySelectorAll("pre.mermaid").forEach(function (pre) {
      blocks.push({ el: pre, source: pre.textContent });
    });
    if (!blocks.length) return;
    window.addEventListener(THEME_EVENT, function (e) {
      var detail = (e && e.detail) || {};
      renderAll(detail.theme, detail.generation);
    });
    renderAll();
  }

  // The script is loaded in <head>: the diagrams exist once the document has been parsed.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
