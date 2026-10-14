"""The pinned Mermaid artifact renders diagrams offline under a strict CSP.

Three layers:

- build-level: the vendored bundle is verified against the digest recorded in its manifest before a
  build uses it; a missing or wrong-digest asset fails the build.
- page-level: the exact strict CSP used by the embedding portal (``default-src 'self'; script-src
  'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'``) is
  checked against every built page: no inline style attribute, no ``<style>`` or src-less
  ``<script>`` element, no reference outside the site itself.
- real-browser: the shipped bundle and script render every supported diagram type in a headless
  browser under that exact CSP, sent as a response header, with zero CSP violations, no external
  request, one diagram per block across theme generations and no error diagram left behind
  (skipped where no Chrome/Chromium is installed).
- behaviour-level: the shipped ``mermaid-init.js`` runs in node (skipped where node is absent)
  against a stub of the browser surface it touches, exercising the real script text: malformed
  diagrams are contained, per-diagram render ids stay independent, one diagram lands per block
  across rerenders, delayed overlapping renders retain only the newest theme, and no ``<style>``
  element or ``style`` attribute is ever inserted (styles go through CSSOM).
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_offline_browser import browser_trace, headless_browser, sent_elsewhere

import repowiki.site as site_module
from conftest import commit
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.site import MERMAID_MANIFEST, VENDORED_MERMAID, RepoEntry, SiteError, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

ATLAS_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'"
)


def with_wiki(repo: Path) -> None:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")


def build_default_site(repo: Path, tmp_path: Path) -> Path:
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None, now=NOW)
    return out


# --------------------------------------------------------------------------- pinned artifact


def test_the_vendored_artifact_matches_its_recorded_digest() -> None:
    manifest = json.loads(MERMAID_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["version"] == "12.0.0" and manifest["license"] == "MIT"
    digest = hashlib.sha256(VENDORED_MERMAID.read_bytes()).hexdigest()
    assert digest == manifest["sha256"]
    assert VENDORED_MERMAID.with_name("mermaid.LICENSE").is_file()


@pytest.mark.parametrize("broken", ["manifest", "asset", "digest"])
def test_a_missing_or_wrong_digest_asset_fails_the_build(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str
) -> None:
    with_wiki(repo)
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    shutil.copy(VENDORED_MERMAID, vendor / "mermaid.min.js")
    shutil.copy(MERMAID_MANIFEST, vendor / "manifest.json")
    if broken == "manifest":
        (vendor / "manifest.json").unlink()
    elif broken == "digest":
        manifest = json.loads((vendor / "manifest.json").read_text(encoding="utf-8"))
        manifest["sha256"] = "0" * 64
        (vendor / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    else:
        (vendor / "mermaid.min.js").write_bytes(b"/* tampered */")
    monkeypatch.setattr(site_module, "VENDORED_MERMAID", vendor / "mermaid.min.js")
    monkeypatch.setattr(site_module, "MERMAID_MANIFEST", vendor / "manifest.json")
    with pytest.raises(SiteError, match="mermaid"):
        build_site(
            [RepoEntry("W", repo)],
            tmp_path / "site",
            mermaid=str(vendor / "mermaid.min.js"),
            log=lambda _m: None,
            now=NOW,
        )
    assert not (tmp_path / "site").exists()


# --------------------------------------------------------------------------- exact CSP


def test_every_built_page_satisfies_the_exact_atlas_csp(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = build_default_site(repo, tmp_path)
    assert (out / "assets/vendor/mermaid.min.js").is_file()
    assert (out / "assets/vendor/manifest.json").is_file()
    assert (out / "assets/mermaid-init.js").is_file()
    for page in out.rglob("*.html"):
        html = page.read_text()
        for href in re.findall(r'(?:href|src)="([^"]*)"', html):
            assert not re.match(r"[a-z]+://|^//", href), (page.name, href)  # nothing outside 'self'
        assert not re.search(r"<[^>]*\sstyle=", html), page.name  # style-src 'self': no style attribute
        assert "<style" not in html, page.name  # no style element
        assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html), page.name  # external scripts only
        assert "javascript:" not in html and "data:" not in html, page.name
    css = (out / "assets/style.css").read_text()
    assert not re.search(r"url\(\s*['\"]?https?:", css) and not re.search(r"@import\s+['\"]?https?:", css)
    init = (out / "assets/mermaid-init.js").read_text()
    assert '"repowiki:theme-change"' in init and "addEventListener" in init  # the portal's theme event drives rerenders


# --------------------------------------------------------------------------- behaviour in node

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

function makeWorld(options) {
  const log = { initialize: [], render: [], styles: [], cssText: [], createdTags: [], eventListeners: {} };
  const world = { log, options };

  function makeEl(tag) {
    const el = {
      tag, attrs: {}, children: [], parent: null, text: "", title: "",
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        contains(c) { return this._set.has(c); },
      },
      style: {
        _css: "",
        set cssText(v) { this._css = v; log.cssText.push(v); },
        get cssText() { return this._css; },
      },
      get textContent() { return this.text !== "" ? this.text : this.children.map(c => c.textContent).join(""); },
      set className(v) { el.attrs.class = v; },
      get className() { return el.attrs.class || ""; },
      setAttribute(k, v) { el.attrs[k] = String(v); },
      getAttribute(k) { return k in el.attrs ? el.attrs[k] : null; },
      hasAttribute(k) { return k in el.attrs; },
      removeAttribute(k) { delete el.attrs[k]; },
      remove() {
        if (!el.parent) return;
        const i = el.parent.children.indexOf(el);
        if (i >= 0) el.parent.children.splice(i, 1);
        el.parent = null;
      },
      replaceWith(other) {
        const p = el.parent;
        if (!p) return;
        const i = p.children.indexOf(el);
        p.children[i] = other;
        other.parent = p;
        el.parent = null;
      },
      appendChild(c) { el.children.push(c); c.parent = el; return c; },
      querySelector(sel) { return queryAll(el, sel)[0] || null; },
      querySelectorAll(sel) { return queryAll(el, sel); },
    };
    return el;
  }

  function queryAll(root, sel) {
    const out = [];
    const parts = sel.split(",").map(s => s.trim());
    const matches = el => parts.some(s => s.startsWith("[") ? s.slice(1, -1) in el.attrs : el.tag === s);
    for (const child of root.children) {
      if (matches(child)) out.push(child);
      out.push(...queryAll(child, sel));
    }
    return out;
  }

  // Parses exactly the SVG shapes the stub mermaid returns: nested tags with attributes,
  // <style> text, and text nodes.
  function parseFragment(html) {
    const body = makeEl("body");
    let stack = [body], buf = html;
    const re = /<\/?([a-zA-Z][-a-zA-Z0-9]*)((?:\s[-a-zA-Z]+="[^"]*")*)\s*\/?>|([^<]+)/g;
    let m;
    while ((m = re.exec(buf)) !== null) {
      if (m[1] !== undefined) {
        const closing = m[0].startsWith("</");
        const el = makeEl(m[1].toLowerCase());
        const attrRe = /([-a-zA-Z]+)="([^"]*)"/g;
        let a;
        while ((a = attrRe.exec(m[2] || "")) !== null) el.setAttribute(a[1], a[2]);
        if (closing) {
          stack.pop();
        } else {
          stack[stack.length - 1].appendChild(el);
          if (!m[0].endsWith("/>")) stack.push(el);
        }
      } else {
        const top = stack[stack.length - 1];
        const t = m[3];
        if (top.tag === "style" || top.tag === "rw-style") top.text += t;
      }
    }
    return body;
  }

  world.body = makeEl("body");
  world.pres = options.sources.map(source => {
    const pre = makeEl("pre");
    pre.attrs.class = "mermaid";
    pre.text = source;
    world.body.appendChild(pre);
    return pre;
  });

  const documentStub = {
    adoptedStyleSheets: [],
    body: world.body,
    createElement(tag) { log.createdTags.push(tag); return makeEl(tag); },
    importNode(node) { return node; },
    querySelectorAll(sel) { return sel === "pre.mermaid" ? world.pres : []; },
  };
  class CSSStyleSheet { replaceSync(css) { log.styles.push(css); } }
  const sandbox = {
    document: documentStub,
    CSSStyleSheet,
    DOMParser: class { parseFromString(html) { return { body: parseFragment(html) }; } },
    getComputedStyle() { return { fontFamily: "TestFont" }; },
    matchMedia() { return { matches: !!options.systemDark }; },
    addEventListener(name, fn) { (log.eventListeners[name] = log.eventListeners[name] || []).push(fn); },
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  world.mermaid = {
    initialize(cfg) { log.initialize.push(cfg); },
    render(id, source) {
      log.render.push({ id, source });
      return options.render(id, source, world);
    },
  };
  sandbox.mermaid = world.mermaid;
  world.sandbox = vm.createContext(sandbox);
  world.svg = marker => `<svg><style>.marker{fill:${marker};}</style>` +
    `<g style="stroke:#abc456"><rect/><text>label ${marker}</text></g></svg>`;
  world.dispatchTheme = (theme, generation) => {
    for (const fn of log.eventListeners["repowiki:theme-change"] || []) fn({ detail: { theme, generation } });
  };
  world.settle = () => new Promise(resolve => setImmediate(resolve));
  return world;
}

async function runScenario(name, initPath) {
  const results = [];
  const check = (ok, info) => results.push({ name, ok: !!ok, info: info || "" });
  const settleAll = async () => { for (let i = 0; i < 5; i++) await new Promise(r => setImmediate(r)); };

  if (name === "malformed-diagram") {
    const world = makeWorld({
      sources: ["graph TD\nA-->B", "BROKEN~~~"],
      render: (id, source) => source.includes("BROKEN")
        ? Promise.reject(new Error("parse error"))
        : Promise.resolve({ svg: world.svg("light") }),
    });
    vm.runInContext(fs.readFileSync(initPath, "utf8"), world.sandbox);
    await settleAll();
    const broken = world.pres[1];
    const boxes = world.body.children.filter(c => c.className === "mermaid-diagram");
    check(broken.classList.contains("mermaid-error") && broken.title.includes("could not be rendered"),
      "malformed block marked, source stays");
    check(broken.tag === "pre", "the malformed block is not replaced");
    check(boxes.length === 1 && boxes[0].children.some(c => c.tag === "svg"),
      "good diagram still mounted");
    check(world.log.render.length === 2, "both diagrams attempted");
    check(!world.log.createdTags.includes("style"), "no <style> element ever created");
    check(world.log.styles.length > 0, "svg styles lifted into CSSOM");
    return results;
  }

  if (name === "independent-ids") {
    const world = makeWorld({
      sources: ["graph TD\nA-->B", "graph TD\nA-->C"],
      render: () => Promise.resolve({ svg: world.svg("light") }),
    });
    vm.runInContext(fs.readFileSync(initPath, "utf8"), world.sandbox);
    await settleAll();
    const ids = world.log.render.map(r => r.id);
    check(new Set(ids).size === 2, "two distinct render ids: " + ids.join(","));
    check(world.body.children.filter(c => c.className === "mermaid-diagram").length === 2,
      "both diagrams mounted");
    check(!world.log.createdTags.includes("style"), "no <style> element ever created");
    return results;
  }

  if (name === "one-diagram-per-block") {
    const world = makeWorld({
      sources: ["graph TD\nA-->B"],
      render: () => Promise.resolve({ svg: world.svg("light") }),
    });
    vm.runInContext(fs.readFileSync(initPath, "utf8"), world.sandbox);
    await settleAll();
    world.dispatchTheme("dark", 2); await settleAll();
    world.dispatchTheme("light", 3); await settleAll();
    const boxes = world.body.children.filter(c => c.className === "mermaid-diagram").length;
    check(boxes === 1, `one diagram after 3 renders (${boxes})`);
    check(!world.log.createdTags.includes("style"), "no <style> element ever created");
    return results;
  }

  if (name === "newest-theme-wins") {
    let resolvers = [];
    const world = makeWorld({
      sources: ["graph TD\nA-->B"],
      render: id => new Promise(resolve => {
        resolve.gen = Number(String(id).split("-")[2]);
        resolvers.push(resolve);
      }),
    });
    vm.runInContext(fs.readFileSync(initPath, "utf8"), world.sandbox);
    await world.settle();
    const firstInitialize = world.log.initialize[world.log.initialize.length - 1];
    world.dispatchTheme("dark", 7); await world.settle();
    const darkInitialize = world.log.initialize[world.log.initialize.length - 1];
    const old = resolvers.filter(r => r.gen === 1), fresh = resolvers.filter(r => r.gen === 7);
    check(old.length === 1 && fresh.length === 1, "one render per generation: " + resolvers.length);
    old.forEach(r => r({ svg: world.svg("STALE-LIGHT") })); await settleAll();
    fresh.forEach(r => r({ svg: world.svg("DARK") })); await settleAll();
    const boxes = world.body.children.filter(c => c.className === "mermaid-diagram");
    const box = boxes[0];
    check(firstInitialize.themeVariables.darkMode === false, "initial render follows the system theme");
    check(darkInitialize.themeVariables.darkMode === true, "theme event switches to dark");
    check(firstInitialize.themeVariables.textColor !== darkInitialize.themeVariables.textColor,
      "labels change ink between themes");
    check(world.log.styles.some(css => css.includes("DARK")) &&
            !world.log.styles.some(css => css.includes("STALE-LIGHT")),
      "the stale completion never mounted; the newest one did");
    check(box.className === "mermaid-diagram", "only the newest completion mounted");
    check(!world.log.createdTags.includes("style"), "no <style> element ever created");
    return results;
  }

  return [{ name, ok: false, info: "unknown scenario" }];
}

(async () => {
  const [initPath, scenarios] = [process.argv[2], process.argv[3].split(",")];
  const out = [];
  for (const s of scenarios) out.push(...await runScenario(s, initPath));
  console.log("RESULT" + JSON.stringify(out));
})();
"""


@pytest.fixture
def init_path() -> Path:
    return site_module.ASSETS / "mermaid-init.js"


def run_node(scenarios: str, init_path: Path, tmp_path: Path) -> list[dict]:
    if shutil.which("node") is None:
        pytest.skip("behaviour checks need node, which this machine does not offer")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(init_path), scenarios], capture_output=True, text=True, timeout=120
    )
    if proc.returncode != 0:
        raise AssertionError(f"harness failed: {proc.stderr}")
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT"))
    return json.loads(line[len("RESULT") :])


@pytest.mark.parametrize(
    "scenario",
    ["malformed-diagram", "independent-ids", "one-diagram-per-block", "newest-theme-wins"],
)
def test_the_shipped_init_script_behaves_in_a_browser_like_world(
    scenario: str, init_path: Path, tmp_path: Path
) -> None:
    results = run_node(scenario, init_path, tmp_path)
    failed = [r for r in results if not r["ok"]]
    assert not failed, failed


# --------------------------------------------------------------------------- the shipped bundle, in a real browser
#
# The node world above exercises the script's logic; these load built pages in a real headless browser
# (see test_offline_browser: Chrome/Chromium over the DevTools protocol, skipped where none is
# installed) with the shipped bundle, the exact CSP sent as a response header, every CSP violation
# and every request recorded.

DIAGRAM_TYPES = {
    "flowchart": (
        'graph TD\n  Styled["A fairly long label here"] --> F0["src/widgetry/cli.py"]\n  style Styled fill:#ff99ff'
    ),
    "sequence": "sequenceDiagram\n  Alice->>Bob: Hello\n  Note over Bob: thinks\n  Bob-->>Alice: Hi",
    "class": "classDiagram\n  class Animal{\n    +int age\n    +eat()\n  }\n  Animal <|-- Dog",
    "state": "stateDiagram-v2\n  [*] --> Still\n  Still --> Moving\n  Moving --> [*]",
    "er": "erDiagram\n  CUSTOMER ||--o{ ORDER : places",
    "gantt": "gantt\n  dateFormat YYYY-MM-DD\n  section A\n  Task :a1, 2024-01-01, 3d",
    "pie": 'pie title Pets\n  "Dogs" : 386\n  "Cats" : 85',
    "mindmap": "mindmap\n  root((mindmap))\n    Origins\n    Research",
    "git": "gitGraph\n  commit\n  branch dev\n  commit\n  checkout main\n  merge dev",
    "journey": "journey\n  title My day\n  section Go\n    Make tea: 5: Me",
}
MALFORMED = "graph TD\n  A-->"
# Resolves once no render is in flight (each one holds an off-screen container until it settles).
IDLE = """new Promise((resolve, reject) => {
  const start = Date.now();
  const poll = () => !document.querySelector('[data-repowiki-render]') ? resolve()
    : Date.now() - start > 30000 ? reject(new Error('renders never settled')) : setTimeout(poll, 50);
  poll();
})"""
STATE = """(() => {
  const label = document.querySelector('.mermaid-diagram svg g.node text, .mermaid-diagram svg text');
  const styled = document.querySelector('.mermaid-diagram svg g.node[id*="flowchart-Styled-"] :is(rect, path)');
  return {
    pres: document.querySelectorAll('pre.mermaid').length,
    diagrams: document.querySelectorAll('.mermaid-diagram').length,
    svgs: document.querySelectorAll('.mermaid-diagram > svg').length,
    marked: document.querySelectorAll('pre.mermaid.mermaid-error').length,
    errorSvgs: document.querySelectorAll('svg[aria-roledescription="error"]').length,
    styleElements: document.querySelectorAll('style').length,
    loose: Array.from(document.querySelectorAll('[data-repowiki-render], [id^="drepowiki-mermaid"], '
      + '[id^="irepowiki-mermaid"], body > div:not(.layout), body > svg'))
      .map(e => e.tagName.toLowerCase() + '.' + e.className),
    label: label ? getComputedStyle(label).fill : null,
    styledFill: styled ? getComputedStyle(styled).fill : null,
    navigation: document.querySelectorAll('nav a, a.brand').length,
    widths: Array.from(document.querySelectorAll('.mermaid-diagram > svg')).map(s => s.getBoundingClientRect().width),
  };
})()"""


SETTLED = f"(async () => {{ await {IDLE}; return {STATE}; }})()"


def theme_change(theme: str, generation: int) -> str:
    """Waits for the renders in flight, records the page, then announces a theme change."""
    detail = json.dumps({"theme": theme, "generation": generation})
    event = f"new CustomEvent('repowiki:theme-change', {{detail: {detail}}})"
    return f"(async () => {{ await {IDLE}; const state = {STATE}; window.dispatchEvent({event}); return state; }})()"


@contextmanager
def serve_with_csp(root: Path, csp: str | None) -> Iterator[str]:
    """`root` over loopback, every response carrying `csp` (when given) as its Content-Security-Policy."""
    types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json"}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target = root / (urlsplit(self.path).path.lstrip("/") or "index.html")
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", types.get(target.suffix, "application/octet-stream"))
            if csp:
                self.send_header("Content-Security-Policy", csp)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def site_with_diagrams(repo: Path, tmp_path: Path, diagrams: list[str]) -> Path:
    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "".join(f"\n\n```mermaid\n{d}\n```\n" for d in diagrams))
    return build_default_site(repo, tmp_path)


def test_every_diagram_type_renders_under_the_exact_csp_with_no_violation(repo: Path, tmp_path: Path) -> None:
    out = site_with_diagrams(repo, tmp_path, [*DIAGRAM_TYPES.values(), MALFORMED])
    with serve_with_csp(out, ATLAS_CSP) as base, headless_browser(tmp_path) as ws:
        steps = [theme_change("dark", 2), theme_change("light", 3), theme_change("dark", 4)]
        (trace,) = browser_trace(ws, [{"url": base + "w/1-overview.html", "steps": steps, "probe": SETTLED}], tmp_path)
    # Each step records the page as the previous generation left it: system theme, dark, light, dark.
    first, dark, light, last = (*trace["steps"], trace["probe"])
    assert trace["violations"] == [], trace["violations"][:5]  # the whole visit, all four generations
    assert trace["exceptions"] == [] and sent_elsewhere(trace, urlsplit(base).netloc) == []
    good = (out / "w/1-overview.html").read_text().count('<pre class="mermaid">') - 1  # all but the malformed one
    assert good > len(DIAGRAM_TYPES)  # the generated page's own diagram too
    for state in (first, dark, light, last):
        assert state["diagrams"] == state["svgs"] == good, state  # one diagram per block, every time
        assert state["pres"] == state["marked"] == 1, state  # the malformed block keeps its source, marked
        assert state["errorSvgs"] == 0 and state["styleElements"] == 0 and state["loose"] == [], state
        assert state["navigation"] > 0, state  # the page around a broken diagram still works
        assert all(w > 40 for w in state["widths"]), state  # text was measured: real layout, not 0-sized
    assert {s["styledFill"] for s in (first, dark, light, last)} == {"rgb(255, 153, 255)"}  # styles survive
    assert light["label"] != dark["label"] == last["label"]  # labels change ink with the theme, both ways
    assert first["label"] in (light["label"], dark["label"])


def test_real_mermaid_fetches_an_image_node_only_where_nothing_stops_it(repo: Path, tmp_path: Path) -> None:
    external = 'graph TD\n  A@{ img: "https://cdn.example.test/logo.png", h: 80 }\n  A --> B'
    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + f"\n\n```mermaid\n{external}\n```\n")
    with pytest.raises(SiteError, match="offline site policy: this diagram has an image node"):
        build_default_site(repo, tmp_path)  # refused at build time
    page.write_text(page.read_text().replace(f"\n\n```mermaid\n{external}\n```\n", ""))
    out = build_default_site(repo, tmp_path)
    built = out / "w/1-overview.html"
    slipped = re.sub(r'(<pre class="mermaid">).*?(</pre>)', lambda m: m[1] + html.escape(external) + m[2],
                     built.read_text(), count=1, flags=re.S)  # fmt: skip
    built.with_name("slipped.html").write_text(slipped)
    open_page = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", slipped)
    built.with_name("open.html").write_text(open_page)
    with serve_with_csp(out, ATLAS_CSP) as guarded_base, serve_with_csp(out, None) as open_base:
        with headless_browser(tmp_path) as ws:
            guarded, control = browser_trace(
                ws, [{"url": guarded_base + "w/slipped.html"}, {"url": open_base + "w/open.html"}], tmp_path
            )
    # With nothing in the way, the shipped Mermaid really fetches an image node's URL by itself ...
    assert "https://cdn.example.test/logo.png" in sent_elsewhere(control, urlsplit(open_base).netloc)
    # ... which is why such diagrams are refused, and the page's CSP blocks one that slipped through.
    assert sent_elsewhere(guarded, urlsplit(guarded_base).netloc) == []
    assert any(v["blocked"].startswith("https://cdn.example.test/") for v in guarded["violations"])
