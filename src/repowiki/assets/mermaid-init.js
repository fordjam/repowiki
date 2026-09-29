// Initialise a classic-script mermaid build (used when mermaid is loaded from a file).
(function () {
  if (!window.mermaid) return;
  var dark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  window.mermaid.initialize({ startOnLoad: true, securityLevel: "strict", theme: dark ? "dark" : "default" });
})();
