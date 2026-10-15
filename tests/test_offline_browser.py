"""The automatic requests a reader's browser would make on a built site all stay on the site itself.

Every built page is served over loopback and crawled the way a browser is: each page's resource
references (scripts, stylesheets, images, embedded frames, meta-refresh targets) are collected,
checked for a foreign origin, and fetched to prove they exist. Stylesheets are checked too, since
``url()`` and ``@import`` fetch without any tag. Navigation links are followed only to find pages;
a reader clicking one is explicit user navigation, not an automatic request.

The crawl cannot see what scripts fetch, so the site is also loaded in a real headless browser (Chrome
or Chromium, driven over the DevTools protocol by node) with a local Mermaid build: every request each
page makes and every Content-Security-Policy violation is recorded. The browser's own proxy points at a
closed loopback port, so nothing can leave the machine even if a page tried. Where no such browser (or
no node with WebSocket) is installed those checks skip; the repository adds no browser dependency.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import urlopen

import pytest

from conftest import commit
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, SiteError, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

AUTOMATIC = {"img": ("src", "srcset"), "script": ("src",), "link": ("href", "src"),
             "source": ("src", "srcset"), "iframe": ("src",), "embed": ("src",), "object": ("data",)}  # fmt: skip


class _Refs(HTMLParser):
    """Resource references a browser fetches by itself, and pages it may be sent to by links."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fetches: list[str] = []
        self.pages: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = dict(attrs)
        if tag == "meta" and data.get("http-equiv", "").lower() == "refresh":
            m = re.search(r"url=([^;]+)\s*$", data.get("content", ""))
            if m:
                self.fetches.append(m.group(1).strip())
            return
        for name in AUTOMATIC.get(tag, ()):
            value = data.get(name)
            if value:
                self.fetches.extend(part.strip().split()[0] for part in value.split(",") if part.strip())
        if tag == "a" and data.get("href"):
            self.pages.append(data["href"])


@pytest.fixture
def site_dir(repo: Path, tmp_path: Path) -> Path:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, now=NOW, log=lambda _m: None)
    commit(repo, "wiki")
    out = tmp_path / "site"
    build_site(
        [RepoEntry("Widgetry", repo, url="https://github.com/example/widgetry", export_sources=True)],
        out,
        log=lambda _m: None, now=NOW,
    )
    return out


def test_browser_trace_of_every_page_makes_no_external_request(site_dir: Path) -> None:
    served = site_dir

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.lstrip("/") or "index.html"
            target = served / path
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            kind = {"html": "text/html", "css": "text/css", ".js": "text/javascript"}.get(target.suffix.lstrip("."), "")
            self.send_response(200)
            self.send_header("Content-Type", kind or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    here = urlsplit(base).netloc
    try:
        seen: set[str] = set()
        queue = [base]
        fetches: list[tuple[str, str]] = []
        while queue:
            url = queue.pop()
            if url in seen or not urlsplit(url).path.endswith(".html") and url != base:
                continue
            seen.add(url)
            with urlopen(url) as response:
                page = response.read().decode()
            refs = _Refs()
            refs.feed(page)
            for ref in refs.fetches:
                target = urljoin(url, ref)
                parts = urlsplit(target)
                assert parts.scheme in ("", "http") and parts.netloc in ("", here), (url, ref)
                fetches.append((url, target))
            queue.extend(urljoin(url, href).split("#")[0] for href in refs.pages)
        assert len(seen) > 2 and fetches  # the crawl reached the repo's pages and found real resources
        for url, target in fetches:
            with urlopen(target) as response:
                assert response.status == 200, (url, target)
        css = urlopen(urljoin(base, "assets/style.css")).read().decode()
        assert not re.search(r"url\(\s*['\"]?https?:", css) and not re.search(r"@import\s+['\"]?https?:", css)
    finally:
        server.shutdown()
        server.server_close()


# --------------------------------------------------------------------------- a real browser

BROWSER_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
MAC_BROWSERS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
BROWSER_FLAGS = (
    "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0",
    "--disable-background-networking", "--disable-component-update", "--disable-sync", "--disable-default-apps",
    "--disable-extensions", "--no-pings", "--mute-audio", "--password-store=basic", "--use-mock-keychain",
    # Nothing leaves the machine: every non-loopback request goes to a closed loopback port.
    "--proxy-server=http://127.0.0.1:9", "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
)  # fmt: skip
DRIVER_JS = r"""
// Drives one headless browser tab over the DevTools protocol: for each page, every request the page
// makes, every Content-Security-Policy violation, exceptions, and the value of an optional probe.
const [wsUrl, jobFile] = process.argv.slice(2);
const pages = JSON.parse(require("fs").readFileSync(jobFile, "utf8"));
const ws = new WebSocket(wsUrl);
let nextId = 0;
const pending = new Map();
const waiters = [];
let session = null;
let current = null;
let lastActivity = Date.now();
const call = (method, params = {}, sessionId = session) =>
  new Promise((resolve, reject) => {
    const id = ++nextId;
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params, sessionId: sessionId || undefined }));
  });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    return msg.error ? reject(new Error(msg.error.message)) : resolve(msg.result);
  }
  if (!current || msg.sessionId !== session) return;
  const p = msg.params || {};
  if (msg.method === "Network.requestWillBeSent") {
    lastActivity = Date.now();
    current.requests[p.requestId] = { url: p.request.url, type: p.type || "", blocked: "" };
  } else if (msg.method === "Network.loadingFailed") {
    lastActivity = Date.now();
    const r = current.requests[p.requestId];
    if (r) r.blocked = p.blockedReason || p.errorText || "failed";
  } else if (msg.method === "Network.loadingFinished") {
    lastActivity = Date.now();
  } else if (msg.method === "Runtime.exceptionThrown") {
    current.exceptions.push(p.exceptionDetails.exception?.description || p.exceptionDetails.text);
  } else if (msg.method === "Page.loadEventFired") {
    current.loaded = true;
  }
};
async function settle(quietMs, maxMs) {
  const start = Date.now();
  while (Date.now() - start < maxMs) {
    if (current.loaded && Date.now() - lastActivity >= quietMs) return;
    await sleep(50);
  }
}
async function evaluate(expression) {
  const out = await call("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (out.exceptionDetails) throw new Error(out.exceptionDetails.exception?.description || out.exceptionDetails.text);
  return out.result.value;
}
ws.onopen = async () => {
  try {
    const { targetId } = await call("Target.createTarget", { url: "about:blank" }, null);
    session = (await call("Target.attachToTarget", { targetId, flatten: true }, null)).sessionId;
    for (const domain of ["Network", "Page", "Runtime"]) await call(`${domain}.enable`);
    await call("Network.setCacheDisabled", { cacheDisabled: true });
    await call("Page.addScriptToEvaluateOnNewDocument", {
      source:
        "window.__violations = [];" +
        "document.addEventListener('securitypolicyviolation', e => window.__violations.push(" +
        "{directive: e.effectiveDirective, blocked: e.blockedURI, sample: e.sample}));",
    });
    const results = [];
    for (const page of pages) {
      current = { url: page.url, requests: {}, exceptions: [], loaded: false };
      lastActivity = Date.now();
      await call("Page.navigate", { url: page.url });
      await settle(400, 15000);
      for (const step of page.steps || []) {
        current.steps = current.steps || [];
        current.steps.push(await evaluate(step));
        lastActivity = Date.now();
        await settle(400, 15000);
      }
      const probe = page.probe ? await evaluate(page.probe) : null;
      const violations = await evaluate("window.__violations");
      results.push({
        url: page.url,
        requests: Object.values(current.requests),
        violations,
        exceptions: current.exceptions,
        steps: current.steps || [],
        probe,
      });
    }
    console.log(JSON.stringify(results));
    process.exit(0);
  } catch (err) {
    console.error(String(err && err.stack ? err.stack : err));
    process.exit(2);
  }
};
ws.onerror = (e) => {
  console.error("websocket error: " + (e.message || e));
  process.exit(2);
};
"""
TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".png": "image/png"}
# Stands in for a local Mermaid build where none ships yet: like Mermaid, it replaces each diagram with
# an SVG and renders an image node (`A@{ img: "…" }`) as an SVG <image> the browser loads by itself.
MERMAID_STAND_IN = """
window.mermaid = { initialize: function () {
  var run = function () {
    document.querySelectorAll("pre.mermaid").forEach(function (pre) {
      var ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg");
      var images = pre.textContent.matchAll(/img:\\s*"([^"]+)"/g);
      for (var m of images) {
        var image = document.createElementNS(ns, "image");
        image.setAttribute("href", m[1]); image.setAttribute("width", "80"); image.setAttribute("height", "80");
        svg.appendChild(image);
      }
      pre.replaceChildren(svg); pre.setAttribute("data-processed", "true");
    });
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
} };
"""
EXTERNAL_DIAGRAM = 'graph TD\n  A@{ img: "https://cdn.example.test/logo.png", h: 80 }\n  A --> B'


def find_browser() -> str | None:
    """A Chrome/Chromium binary: REPOWIKI_BROWSER if set, else the usual names and macOS locations."""
    chosen = os.environ.get("REPOWIKI_BROWSER")
    if chosen:
        return chosen if os.access(chosen, os.X_OK) else None
    for name in BROWSER_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return next((path for path in MAC_BROWSERS if os.access(path, os.X_OK)), None)


@contextmanager
def headless_browser(tmp_path: Path) -> Iterator[str]:
    """The DevTools WebSocket URL of a fresh headless browser with its own empty profile."""
    exe = find_browser()
    if exe is None:
        pytest.skip("real-browser checks need Chrome or Chromium (set REPOWIKI_BROWSER), which is not installed")
    probe = "process.exit(typeof WebSocket === 'function' ? 0 : 3)"
    if shutil.which("node") is None or subprocess.run(["node", "-e", probe], timeout=60).returncode != 0:
        pytest.skip("real-browser checks need node with a built-in WebSocket (node 22 or later)")
    profile = tmp_path / "browser-profile"
    profile.mkdir()
    proc = subprocess.Popen(
        [exe, *BROWSER_FLAGS, f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        port_file = profile / "DevToolsActivePort"
        deadline = time.monotonic() + 60
        while not (port_file.is_file() and len(port_file.read_text().split()) >= 2):
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.skip(f"the browser at {exe} could not start here")
            time.sleep(0.1)
        port, path = port_file.read_text().split()[:2]
        yield f"ws://127.0.0.1:{port}{path}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def browser_trace(ws_url: str, pages: list[dict], tmp_path: Path) -> list[dict]:
    """Loads each page ({url, steps?, probe?}) in the browser; per page: requests (url, type, blocked),
    CSP violations, uncaught exceptions, the value of each step and of the probe expression."""
    job, driver = tmp_path / "trace-job.json", tmp_path / "trace-driver.js"
    job.write_text(json.dumps(pages))
    driver.write_text(DRIVER_JS)
    out = subprocess.run(["node", str(driver), ws_url, str(job)], capture_output=True, text=True, timeout=600)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def sent_elsewhere(trace: dict, here: str) -> list[str]:
    """URLs the page asked the network for outside the site (a request CSP blocked never left)."""
    return [
        r["url"]
        for r in trace["requests"]
        if urlsplit(r["url"]).scheme in ("http", "https", "ws", "wss", "ftp")
        and urlsplit(r["url"]).netloc != here
        and r["blocked"] != "csp"
    ]


@contextmanager
def serve(root: Path) -> Iterator[str]:
    """`root` served over loopback; yields the base URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            target = root / (urlsplit(self.path).path.lstrip("/") or "index.html")
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", TYPES.get(target.suffix, "application/octet-stream"))
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


@pytest.fixture
def mermaid_site(repo: Path, tmp_path: Path) -> Path:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, now=NOW, log=lambda _m: None)
    commit(repo, "wiki")
    script = tmp_path / "mermaid.min.js"
    script.write_text(MERMAID_STAND_IN)
    out = tmp_path / "site"
    entry = RepoEntry("Widgetry", repo, url="https://github.com/example/widgetry", export_sources=True)
    build_site([entry], out, mermaid=str(script), log=lambda _m: None, now=NOW)
    return out


DIAGRAMS = (
    "({diagrams: document.querySelectorAll('pre.mermaid').length,"
    " rendered: document.querySelectorAll('pre.mermaid[data-processed] svg').length,"
    " styled: getComputedStyle(document.body).margin === '0px'})"
)


def test_a_real_browser_loads_every_page_with_no_external_request(mermaid_site: Path, tmp_path: Path) -> None:
    pages = sorted(p.relative_to(mermaid_site).as_posix() for p in mermaid_site.rglob("*.html"))
    with serve(mermaid_site) as base, headless_browser(tmp_path) as ws:
        traces = browser_trace(ws, [{"url": base + p, "probe": DIAGRAMS} for p in pages], tmp_path)
    here = urlsplit(base).netloc
    assert len(traces) == len(pages) > 3
    for trace in traces:
        assert sent_elsewhere(trace, here) == [], trace["url"]
        assert trace["violations"] == [] and trace["exceptions"] == [], trace
        assert any(urlsplit(r["url"]).path.endswith("style.css") for r in trace["requests"]), trace["url"]
    diagrams = [t["probe"] for t in traces if t["probe"] and t["probe"]["diagrams"]]
    assert diagrams and all(d["rendered"] == d["diagrams"] for d in diagrams)  # the scripts really ran


def test_an_external_diagram_resource_is_refused_and_a_browser_would_never_fetch_it(
    repo: Path, mermaid_site: Path, tmp_path: Path
) -> None:
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + f"\n\n```mermaid\n{EXTERNAL_DIAGRAM}\n```\n")
    with pytest.raises(SiteError, match="offline site policy: this diagram has an image node"):
        build_site([RepoEntry("Widgetry", repo)], mermaid_site, mermaid=str(tmp_path / "mermaid.min.js"), now=NOW)
    # Had the diagram slipped through anyway, the page's own policy keeps the browser from fetching it.
    # The same page without that policy shows the trace does see a diagram's image fetch.
    built = next(p for p in mermaid_site.rglob("*.html") if '<pre class="mermaid">' in p.read_text())
    slipped = re.sub(r'(<pre class="mermaid">).*?(</pre>)', lambda m: m[1] + html.escape(EXTERNAL_DIAGRAM) + m[2],
                     built.read_text(), count=1, flags=re.S)  # fmt: skip
    (built.parent / "slipped.html").write_text(slipped)
    unguarded = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', "", slipped)
    (built.parent / "unguarded.html").write_text(unguarded)
    rel = built.parent.relative_to(mermaid_site).as_posix() + "/"
    with serve(mermaid_site) as base, headless_browser(tmp_path) as ws:
        guarded, control = browser_trace(
            ws, [{"url": base + rel + name, "probe": DIAGRAMS} for name in ("slipped.html", "unguarded.html")], tmp_path
        )
    here = urlsplit(base).netloc
    for trace in (guarded, control):
        assert trace["probe"]["rendered"] == trace["probe"]["diagrams"] >= 1  # the diagram really rendered
    assert sent_elsewhere(guarded, here) == []
    assert any(v["blocked"].startswith("https://cdn.example.test/") for v in guarded["violations"]), guarded
    assert sent_elsewhere(control, here) == ["https://cdn.example.test/logo.png"]
