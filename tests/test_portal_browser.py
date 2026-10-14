"""The portal hooks work in a browser: search, repo navigation, theme persistence and logout.

The shipped ``site.js`` runs in node (skipped where node is absent) against a stub of the browser
surface it touches: a small DOM of the elements the script wires up, a recording ``fetch``, an
in-memory ``localStorage`` and an event registry. The scenarios drive the real script text —
a repeated render must not double a control, a search selection must navigate to the entry's own
repository page, a chosen theme must survive a reload as a ``repowiki:theme-change`` event, and the
logout control must POST to its same-origin action carrying the token fetched at click time, and
must send nothing and say so when no valid token arrives, or say so when the POST fails.

The built pages are then loaded in a real headless browser beside a stand-in portal: search on
every page reads the rendered ``<template>`` index and navigates, and logout posts only with the
token the endpoint issued, showing every failure.
"""

from __future__ import annotations

import json
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
from test_offline_browser import browser_trace, headless_browser

import repowiki.site as site_module
from conftest import commit
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.site import PortalHooks, RepoEntry, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

function makeWorld(options) {
  const log = { listeners: {}, events: [], fetches: [], reloads: 0 };
  const storage = options.storage || {};

  function makeEl(tag) {
    const el = {
      tag, attrs: {}, children: [], parent: null, text: "", title: "", hidden: false,
      listeners: {},
      classList: {
        _set: new Set(),
        add(c) { this._set.add(c); },
        contains(c) { return this._set.has(c); },
        toggle(c) { if (this._set.has(c)) { this._set.delete(c); return false; } this._set.add(c); return true; },
      },
      style: {},
      get textContent() { return el.text !== "" ? el.text : el.children.map(c => c.textContent).join(""); },
      set textContent(v) { el.text = v; el.children = []; },
      set className(v) { el.attrs.class = v; },
      get className() { return el.attrs.class || ""; },
      setAttribute(k, v) { el.attrs[k] = String(v); },
      getAttribute(k) { return k in el.attrs ? el.attrs[k] : null; },
      hasAttribute(k) { return k in el.attrs; },
      removeAttribute(k) { delete el.attrs[k]; },
      appendChild(c) { el.children.push(c); c.parent = el; return c; },
      contains(x) { while (x) { if (x === el) return true; x = x.parent; } return false; },
      querySelector(sel) { return el.querySelectorAll(sel)[0] || null; },
      querySelectorAll(sel) {
        const out = [];
        for (const child of el.children) {
          if (sel === "a" && child.tag === "a") out.push(child);
          out.push(...child.querySelectorAll(sel));
        }
        return out;
      },
      addEventListener(name, fn) { (el.listeners[name] = el.listeners[name] || []).push(fn); },
      fire(name, event) { for (const fn of el.listeners[name] || []) fn(event); },
    };
    return el;
  }

  const root = makeEl("html");
  const documentStub = {
    documentElement: root,
    body: makeEl("body"),
    createElement(tag) { return makeEl(tag); },
    getElementById(id) { return id === "repowiki-search" ? world.dataEl : null; },
    querySelector(sel) {
      if (sel === ".portal-search") return world.input;
      if (sel === ".search-results") return world.box;
      if (sel === ".portal-logout") return world.logout;
      if (sel === ".portal-logout-status") return world.status;
      if (sel === ".menu") return null;
      return null;
    },
    querySelectorAll() { return []; },
    addEventListener(name, fn) {
      (log.listeners["document:" + name] = log.listeners["document:" + name] || []).push(fn);
    },
  };

  const sandbox = {
    document: documentStub,
    localStorage: {
      getItem: k => (k in storage ? storage[k] : null),
      setItem: (k, v) => { storage[k] = String(v); },
    },
    CustomEvent: function (type, opts) { this.type = type; this.detail = opts && opts.detail; },
    fetch: (url, init) => {
      log.fetches.push({ url, init: init || {} });
      if (options.fetch) return options.fetch(url, init, log);
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: "T-123" }) });
    },
    getComputedStyle: () => ({ fontFamily: "TestFont" }),
    matchMedia: () => ({ matches: !!options.systemDark, addEventListener() {} }),
    addEventListener(name, fn) { (log.listeners[name] = log.listeners[name] || []).push(fn); },
    dispatchEvent(e) {
      log.events.push(e);
      for (const fn of log.listeners[e.type] || []) fn(e);
      return true;
    },
    location: { href: options.location || "https://site.test/w/1-overview.html", reload() { log.reloads += 1; } },
    console,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;

  world = {
    sandbox: vm.createContext(sandbox), log, storage, root,
    input: makeEl("input"), box: makeEl("div"), logout: options.logout || null,
    dataEl: makeEl("template"), status: makeEl("span"),
    fire(name, event) { for (const fn of log.listeners[name] || []) fn(event); },
  };
  world.input.attrs.class = "portal-search";
  world.box.attrs.class = "search-results";
  if (world.dataEl) {
    world.dataEl.attrs.id = "repowiki-search";
    // A <template>: its text lives in its content fragment; the element's own textContent is empty.
    world.dataEl.content = { textContent: JSON.stringify(options.search || { pages: [] }) };
  }
  world.status.hidden = true;
  return world;
}

async function settle() { for (let i = 0; i < 6; i++) await new Promise(r => setImmediate(r)); }

function load(world, initPath, times) {
  const source = fs.readFileSync(initPath, "utf8");
  for (let i = 0; i < (times || 1); i++) vm.runInContext(source, world.sandbox);
}

async function runScenario(name, initPath) {
  const results = [];
  const check = (ok, info) => results.push({ name, ok: !!ok, info: info || "" });

  if (name === "repeat-render-no-duplicates") {
    const world = makeWorld({
      logout: makeElLikeLogout(),
      fetch: (url, init, log) => {
        if (url === "/csrf-token") {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: "T-123" }) });
        }
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      },
      search: { pages: [{ r: "w", t: "Overview", u: "../w/1-overview.html" }] },
    });
    function makeElLikeLogout() {
      const el = {
        attrs: { class: "portal-logout", "data-action": "/logout", "data-token-endpoint": "/csrf-token" },
        listeners: {},
        classList: { add() {}, contains() { return false; } },
        getAttribute(k) { return el.attrs[k] || null; },
        addEventListener(n, f) { (el.listeners[n] = el.listeners[n] || []).push(f); },
        fire(name, e) { for (const fn of el.listeners[name] || []) fn(e); },
      };
      return el;
    }
    load(world, initPath, 2); // a repeat render must not double anything
    world.logout.fire("click", {});
    await settle();
    const posts = world.log.fetches.filter(f => f.init.method === "POST");
    check(world.log.fetches.length === 2 && posts.length === 1,
      `one token fetch and one POST after a repeat render (${world.log.fetches.length} fetches)`);
    check(world.log.reloads === 1, "session revoked state refreshed once");
    return results;
  }

  if (name === "search-navigates") {
    const world = makeWorld({
      search: {
        pages: [
          { r: "w", t: "Overview", u: "../w/1-overview.html" },
          { r: "twin", t: "Overview", u: "../twin/1-overview.html" },
          { r: "w", t: "Engine", u: "../w/2-engine.html" },
        ],
      },
    });
    load(world, initPath);
    world.input.value = "overview";
    world.input.fire("input", {});
    check(world.box.children.length === 2, "both repo-scoped matches shown");
    const hrefs = world.box.children.map(a => a.href);
    check(hrefs.includes("../w/1-overview.html") && hrefs.includes("../twin/1-overview.html"),
      "identical titles stay scoped to their repos");
    world.input.fire("keydown", { key: "Enter", preventDefault() {} });
    check(world.sandbox.location.href === "../w/1-overview.html",
      "selection navigates to the first match: " + world.sandbox.location.href);
    return results;
  }

  if (name === "theme-survives-reload") {
    const storage = {};
    const first = makeWorld({ storage });
    load(first, initPath);
    first.fire("load", {});
    check(first.log.events.length === 0, "no theme event without a stored choice");
    first.sandbox.repowiki.setTheme("dark");
    check(storage["repowiki-theme"] === "dark", "the choice is persisted");
    check(first.root.attrs["data-rw-theme"] === "dark", "the document follows the theme");
    const emitted = first.log.events.filter(e => e.type === "repowiki:theme-change");
    check(emitted.length === 1 && emitted[0].detail.theme === "dark" && emitted[0].detail.generation === 1,
      "the theme event carries {theme, generation}");

    const second = makeWorld({ storage }); // a fresh page, same browser: the reload
    load(second, initPath);
    check(second.root.attrs["data-rw-theme"] === "dark", "applied before first paint, no flash");
    second.fire("load", {});
    const again = second.log.events.filter(e => e.type === "repowiki:theme-change");
    check(again.length === 1 && again[0].detail.theme === "dark",
      "the reload announces the stored theme for diagram scripts");
    check(second.log.events.every(e => e.detail.generation >= 1), "generations start at 1");
    return results;
  }

  if (name === "logout-post-token") {
    const posts = [];
    const world = makeWorld({
      logout: makeLogout(),
      fetch: (url, init) => {
        if (url === "/csrf-token") {
          return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: "T-123" }) });
        }
        posts.push({ url, init });
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) });
      },
    });
    function makeLogout() {
      const el = {
        attrs: { class: "portal-logout", "data-action": "/logout", "data-token-endpoint": "/csrf-token" },
        listeners: {},
        classList: { add() {}, contains() { return false; } },
        getAttribute(k) { return el.attrs[k] || null; },
        addEventListener(n, f) { (el.listeners[n] = el.listeners[n] || []).push(f); },
        fire(name, e) { for (const fn of el.listeners[name] || []) fn(e); },
      };
      return el;
    }
    load(world, initPath);
    world.logout.fire("click", {});
    await settle();
    check(world.log.fetches.length === 2 && world.log.fetches[0].url === "/csrf-token",
      "the token is fetched at click time from the token endpoint");
    const post = posts[0];
    check(post && post.url === "/logout" && post.init.method === "POST",
      "logout is a structured POST to the same-origin action");
    check(post && post.init.headers["X-CSRF-Token"] === "T-123",
      "the POST carries the current runtime token");
    check(post && post.init.credentials === "same-origin", "credentials stay same-origin");
    check(world.log.reloads === 1, "the revoked session is refreshed");
    return results;
  }

  if (name === "logout-token-failure") {
    const failures = {
      "network error": () => Promise.reject(new TypeError("Failed to fetch")),
      "HTTP 403": () => Promise.resolve({ ok: false, status: 403, json: () => Promise.resolve({ token: "T" }) }),
      "no token": () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({}) }),
      "blank token": () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: " " }) }),
      "not JSON": () => Promise.resolve({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError("x")) }),
    };
    for (const [why, tokenResponse] of Object.entries(failures)) {
      const posts = [];
      const world = makeWorld({
        logout: makeLogoutButton(),
        fetch: (url, init) => {
          if (url === "/csrf-token") return tokenResponse();
          posts.push({ url, init });
          return Promise.resolve({ ok: true, status: 200 });
        },
      });
      load(world, initPath);
      world.logout.fire("click", {});
      await settle();
      check(posts.length === 0, `${why}: no logout POST is sent without a token (${posts.length})`);
      check(world.status.hidden === false && /Log out failed/.test(world.status.text),
        `${why}: the failure is shown: ${JSON.stringify(world.status.text)}`);
      check(world.log.reloads === 0, `${why}: the page does not pretend the session ended`);
    }
    return results;
  }

  if (name === "logout-post-failure") {
    const outcomes = {
      "HTTP 403": () => Promise.resolve({ ok: false, status: 403 }),
      "HTTP 500": () => Promise.resolve({ ok: false, status: 500 }),
      "network error": () => Promise.reject(new TypeError("Failed to fetch")),
    };
    for (const [why, postResponse] of Object.entries(outcomes)) {
      const posts = [];
      const world = makeWorld({
        logout: makeLogoutButton(),
        fetch: (url, init) => {
          if (url === "/csrf-token") {
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ token: "T-9" }) });
          }
          posts.push({ url, init });
          return postResponse();
        },
      });
      load(world, initPath);
      world.logout.fire("click", {});
      await settle();
      check(posts.length === 1 && posts[0].init.headers["X-CSRF-Token"] === "T-9", `${why}: one POST with the token`);
      check(world.status.hidden === false && /still signed in/.test(world.status.text),
        `${why}: the failure is shown: ${JSON.stringify(world.status.text)}`);
      check(world.log.reloads === 0, `${why}: no reload`);
      world.logout.fire("click", {}); // the control stays usable for a retry
      await settle();
      check(posts.length === 2, `${why}: a retry sends again`);
    }
    return results;
  }

  return [{ name, ok: false, info: "unknown scenario" }];
}

function makeLogoutButton() {
  const el = {
    attrs: { class: "portal-logout", "data-action": "/logout", "data-token-endpoint": "/csrf-token" },
    listeners: {},
    classList: { add() {}, contains() { return false; } },
    getAttribute(k) { return el.attrs[k] || null; },
    addEventListener(n, f) { (el.listeners[n] = el.listeners[n] || []).push(f); },
    fire(name, e) { for (const fn of el.listeners[name] || []) fn(e); },
  };
  return el;
}

(async () => {
  const [initPath, scenarios] = [process.argv[2], process.argv[3].split(",")];
  const out = [];
  for (const s of scenarios) out.push(...await runScenario(s, initPath));
  console.log("RESULT" + JSON.stringify(out));
})();
"""


@pytest.fixture
def site_out(repo: Path, tmp_path: Path) -> Path:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")
    out = tmp_path / "site"
    build_site(
        [RepoEntry("W", repo)],
        out,
        portal=PortalHooks(branding="Acme", logout_action="/logout", token_endpoint="/csrf-token"),
        log=lambda _m: None, now=NOW,
    )
    return out


def test_portal_pages_carry_the_hooks(site_out: Path) -> None:
    page = (site_out / "w/1-overview.html").read_text()
    assert '<span class="branding">Acme</span>' in page
    assert 'data-action="/logout"' in page and 'data-token-endpoint="/csrf-token"' in page
    assert 'id="repowiki-search"' in page
    style = (site_out / "assets/style.css").read_text()
    for needed in (".portal-search", ".portal-logout", ".branding", 'data-rw-theme="dark"'):
        assert needed in style


def test_theme_persistence_and_controls_are_shipped(site_out: Path) -> None:
    js = (site_out / "assets/site.js").read_text()
    for needed in ('"repowiki:theme-change"', "localStorage", "data-rw-ready", "X-CSRF-Token"):
        assert needed in js
    assert 'id="repowiki-search"' not in js  # the index travels in the page, not the script


def run_node(scenarios: str, tmp_path: Path) -> list[dict]:
    if shutil.which("node") is None:
        pytest.skip("behaviour checks need node, which this machine does not offer")
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    proc = subprocess.run(
        ["node", str(harness), str(site_module.ASSETS / "site.js"), scenarios],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(f"harness failed: {proc.stderr}")
    line = next(ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT"))
    return json.loads(line[len("RESULT") :])


@pytest.mark.parametrize(
    "scenario",
    [
        "repeat-render-no-duplicates", "search-navigates", "theme-survives-reload", "logout-post-token",
        "logout-token-failure", "logout-post-failure",
    ],  # fmt: skip
)
def test_the_shipped_site_script_behaves_in_a_browser_like_world(scenario: str, tmp_path: Path) -> None:
    results = run_node(scenario, tmp_path)
    failed = [r for r in results if not r["ok"]]
    assert not failed, failed


# --------------------------------------------------------------------------- the built pages, in a real browser
#
# The same controls on rendered pages in a real headless browser (see test_offline_browser:
# Chrome/Chromium over the DevTools protocol, skipped where none is installed), served together
# with a stand-in portal: a token endpoint and a logout action whose answers each test chooses.

SEARCH = """(() => {
  const input = document.querySelector('.portal-search');
  input.value = %s;
  input.dispatchEvent(new Event('input', {bubbles: true}));
  return Array.from(document.querySelectorAll('.search-results a')).map(a => a.getAttribute('href'));
})()"""
CHOOSE = (
    "(document.querySelector('.portal-search')"
    ".dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})), true)"
)
CLICK_LOGOUT = "(document.querySelector('.portal-logout').click(), true)"
LOGOUT_STATE = """(() => {
  const status = document.querySelector('.portal-logout-status');
  return {
    path: location.pathname,
    shown: !status.hidden && getComputedStyle(status).display !== 'none',
    text: status.textContent,
    navigation: performance.getEntriesByType('navigation')[0].type,
  };
})()"""


@contextmanager
def portal(root: Path, answers: dict) -> Iterator[str]:
    """`root` over loopback beside a stand-in portal: GET /csrf-token answers `answers["token"]`
    (status, body); POST /logout answers `answers["logout"]` and is recorded in `answers["posts"]`."""
    types = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json"}

    class Handler(BaseHTTPRequestHandler):
        def reply(self, status: int, kind: str, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            if path == "/csrf-token":
                status, body = answers["token"]
                self.reply(status, "application/json", body.encode())
                return
            target = root / (path.lstrip("/") or "index.html")
            if not target.is_file():
                self.send_error(404)
                return
            self.reply(200, types.get(target.suffix, "application/octet-stream"), target.read_bytes())

        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if urlsplit(self.path).path != "/logout":
                self.send_error(404)
                return
            answers["posts"].append({"token": self.headers.get("X-CSRF-Token")})
            self.reply(answers["logout"], "application/json", b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/"
    finally:
        server.shutdown()
        server.server_close()


def test_search_on_every_built_page_finds_and_opens_a_page(site_out: Path, tmp_path: Path) -> None:
    pages = ["index.html", *sorted(p.relative_to(site_out).as_posix() for p in (site_out / "w").glob("*.html"))]
    pages = [p for p in pages if p != "w/index.html"]  # the redirect stub has no layout
    answers = {"token": (200, '{"token": "T-1"}'), "logout": 200, "posts": []}
    with portal(site_out, answers) as base, headless_browser(tmp_path) as ws:
        jobs = [{"url": base + p, "steps": [SEARCH % json.dumps("overview"), CHOOSE], "probe": "location.pathname"}
                for p in pages]  # fmt: skip
        traces = browser_trace(ws, jobs, tmp_path)
    assert len(traces) == len(pages) > 2
    for page, trace in zip(pages, traces, strict=True):
        found = trace["steps"][0]
        assert found, (page, "the built page's search found nothing")  # the index is read from the <template>
        assert all(href.endswith("w/1-overview.html") for href in found), (page, found)
        assert trace["probe"] == "/w/1-overview.html", (page, trace["probe"])  # selection navigated
        assert trace["violations"] == [] and trace["exceptions"] == [], (page, trace)


@pytest.mark.parametrize(
    ("token", "logout", "posted", "outcome"),
    [
        ((200, '{"token": "T-1"}'), 200, ["T-1"], "reloaded"),
        ((500, ""), 200, [], "Nothing was sent"),
        ((200, "{}"), 200, [], "carried no token"),
        ((200, "not json"), 200, [], "was not JSON"),
        ((200, '{"token": "T-1"}'), 403, ["T-1"], "still signed in"),
    ],
)
def test_logout_in_a_real_browser_posts_only_with_a_token_and_shows_failures(
    site_out: Path, tmp_path: Path, token: tuple[int, str], logout: int, posted: list[str], outcome: str
) -> None:
    answers = {"token": token, "logout": logout, "posts": []}
    with portal(site_out, answers) as base, headless_browser(tmp_path) as ws:
        (trace,) = browser_trace(
            ws, [{"url": base + "w/1-overview.html", "steps": [CLICK_LOGOUT], "probe": LOGOUT_STATE}], tmp_path
        )
    state = trace["probe"]
    assert [p["token"] for p in answers["posts"]] == posted  # never a tokenless POST
    if outcome == "reloaded":
        assert state["navigation"] == "reload" and not state["shown"], state  # the session ended: refreshed
    else:
        assert state["shown"] and outcome in state["text"] and "Log out failed" in state["text"], state
        assert state["navigation"] == "navigate", state  # no pretend success
    assert trace["exceptions"] == [], trace
