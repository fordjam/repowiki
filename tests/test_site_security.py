"""Blocker (d): generated sites only ever contain http(s), mailto or relative links."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from conftest import commit
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.render import Target, render, safe_href
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, SiteError, build_site, load_repos

HOSTILE_URLS = [
    "javascript:alert(1)//",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    'https://example.com/a"onmouseover="alert(1)',
    "https://user:pw@example.com/a/b",
    "https://example.com/a?x=<b>",
    "//example.com/a",
    "file:///etc/passwd",
]


def with_wiki(repo: Path) -> None:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_repos_json_url_must_be_http(url: str, repo: Path, tmp_path: Path) -> None:
    cfg = tmp_path / "repos.json"
    cfg.write_text(json.dumps({"repos": [{"name": "W", "path": str(repo), "url": url}]}))
    with pytest.raises(SiteError, match="http"):
        load_repos(cfg)
    with pytest.raises(SiteError):
        RepoEntry("W", repo, url=url)


def test_reviewer_scenario_produces_no_script_urls(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    cfg = tmp_path / "repos.json"
    cfg.write_text(json.dumps({"repos": [{"name": "W", "path": str(repo), "url": "javascript:alert(1)//"}]}))
    with pytest.raises(SiteError):
        build_site(load_repos(cfg), tmp_path / "site", log=lambda _m: None)
    assert not (tmp_path / "site").exists()


def test_page_links_with_script_schemes_render_as_text() -> None:
    md = (
        "# T\n\n[a](javascript:alert(1)) [b](JAVASCRIPT:alert(1)) [c](vbscript:x) [d](data:text/html,x) "
        "![i](javascript:alert(1)) [ok](https://example.com) [rel](2-x.md)\n\n"
        '<a href="javascript:alert(1)">raw</a>\n'
    )
    html = render(md).html
    assert not re.search(r'(href|src)="\s*(javascript|vbscript|data):', html, re.I)
    assert 'href="https://example.com"' in html and 'href="2-x.md"' in html
    evil = render("[x](a.py)", lambda _h: Target("javascript:alert(1)", "a.py", (1, 2))).html
    assert "javascript:" not in evil


def test_safe_href() -> None:
    assert safe_href("https://example.com/x") and safe_href("../a.py#L1") and safe_href("#top")
    assert safe_href("mailto:someone@example.com")
    assert not safe_href("javascript:alert(1)") and not safe_href('x" onclick="y')


def test_every_href_in_a_built_site_is_safe(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo, url="https://example.com/o/w")], out, log=lambda _m: None)
    for f in out.rglob("*.html"):
        for href in re.findall(r'(?:href|src)="([^"]*)"', f.read_text()):
            assert safe_href(href), (f.name, href)
