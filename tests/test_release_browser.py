"""A release switch is invisible to a reader: the same URLs keep serving, first old then new.

The built output is a pointer into the store; a small loopback server serves it the way a static
server would (resolving the pointer per request). Publishing a second release keeps every URL
working — pages, stylesheet, bundled scripts — while the content becomes the new release's, and the
previous complete release stays retained in the store.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

import pytest

from conftest import commit
from repowiki import site_publish
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock


@pytest.fixture
def repo(repo: Path) -> Path:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")
    return repo


def test_switching_releases_keeps_every_url_working(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, site_title="Before", log=lambda _m: None, now=NOW)
    old_release = Path(site_publish.store_path(out) / "current").resolve()

    served = out
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.lstrip("/")
            target = served / path if path else served / "index.html"
            if not target.is_file():
                self.send_error(404)
                return
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}/"
    try:
        for url in ("index.html", "w/1-overview.html", "assets/style.css", "assets/site.js"):
            with urlopen(base + url) as response:
                assert response.status == 200, url
        build_site([RepoEntry("W", repo)], out, site_title="After", log=lambda _m: None, now=NOW)
        with urlopen(base + "index.html") as response:
            assert b"After" in response.read() and b"Before" not in response.read()
        for url in ("w/1-overview.html", "assets/style.css", "assets/site.js"):
            with urlopen(base + url) as response:
                assert response.status == 200, url  # every asset of the live release keeps loading
    finally:
        server.shutdown()
        server.server_close()
    new_release = Path(site_publish.store_path(out) / "current").resolve()
    assert new_release != old_release
    assert (old_release / "index.html").is_file()  # the previous complete release stays retained
    assert "Before" in (old_release / "index.html").read_text()
