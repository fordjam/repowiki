"""Blocker (d): generated sites only ever contain http(s), mailto or relative links."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import commit
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.render import Target, render, safe_href
from repowiki.runners import FakeRunner
from repowiki.site import OFFLINE_CSP, RepoEntry, SiteError, build_site, load_repos
from repowiki.validate import diagram_problem, validate

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

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
        build_site(load_repos(cfg), tmp_path / "site", log=lambda _m: None, now=NOW)
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
    build_site([RepoEntry("W", repo, url="https://example.com/o/w")], out, log=lambda _m: None, now=NOW)
    for f in out.rglob("*.html"):
        for href in re.findall(r'(?:href|src)="([^"]*)"', f.read_text()):
            assert safe_href(href), (f.name, href)


# --------------------------------------------------------------------------- offline resource policy


def test_offline_policy_refuses_every_non_local_image(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    cases = [
        ("https://cdn.example.net/tracker.png", "offline site policy"),
        ("//cdn.example.net/tracker.png", "offline site policy"),
        ("data:image/png;base64,iVBORw0KGgo=", "offline site policy"),
        ("..%2F..%2Fsecrets/logo.png", "encoded path separators"),
        ("../../assets/logo.svg", "offline site policy"),  # SVG is not an approved raster type
    ]
    for src, why in cases:
        added = f"\n\n![x]({src})\n"
        page.write_text(page.read_text() + added)
        problems = validate(repo)
        assert any(why in str(p) for p in problems), (src, problems)
        with pytest.raises(SiteError, match="failed validation"):
            build_site([RepoEntry("W", repo)], tmp_path / "site", log=lambda _m: None, now=NOW)
        page.write_text(page.read_text().replace(added, ""))


def test_raw_html_images_stay_escaped_and_fetch_nothing(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + '\n\n<img src="https://cdn.example.net/pixel.png" alt="p">\n\n')
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None, now=NOW)  # escaped text is not a request
    html = (out / "w/1-overview.html").read_text()
    assert "<img" not in html and "&lt;img" in html and "cdn.example.net/pixel.png" in html


def test_offline_images_render_as_alt_text_outside_a_site_build() -> None:
    md = "# T\n\n![ok](pic.png) ![remote](https://cdn.example.net/x.png) ![data](data:image/png;base64,AA==)\n"
    html = render(md).html
    assert '<img src="pic.png"' in html and html.count("<img") == 1
    assert "https://cdn.example.net" not in html and "data:image" not in html


def test_script_shaped_portal_branding_is_escaped(repo: Path, tmp_path: Path) -> None:
    from repowiki.site import PortalHooks

    with_wiki(repo)
    portal = PortalHooks(branding="<script>alert(1)</script>Acme")
    build_site([RepoEntry("W", repo)], tmp_path / "site", portal=portal, log=lambda _m: None, now=NOW)
    page = (tmp_path / "site/w/1-overview.html").read_text()
    assert "&lt;script&gt;alert(1)&lt;/script&gt;Acme" in page
    assert "<script>alert(1)" not in page and "Acme" in page


# Diagrams that would make the browser fetch something, however the resource is spelled.
FETCHING_DIAGRAMS = [
    'graph TD\n  A@{ img: "https://cdn.example.test/logo.png", h: 80, constraint: "on" }',
    "graph TD\n  A@{ label: 'x', img: '//cdn.example.test/logo.png' }",
    'graph TD\n  A@{\n    img: "../../assets/logo.png"\n  }',  # a local one is not routed through the image check
    'graph TD\n  A@{ "img" : "https#58;//cdn.example.test/a.png" }',
    'graph TD\n  A@{ i#109;g: "x.png" }',  # Mermaid entity inside the key
    'graph TD\n  A["<img src=\'https://cdn.example.test/a.png\'>"]',
    'graph TD\n  A["&lt;img src=https://cdn.example.test/a.png&gt;"]',
    'graph TD\n  A["#60;img src=x#62;"]',
    "graph TD\n  A-->B\n  style A fill:url(https://cdn.example.test/p.svg#f)",
    "graph TD\n  A-->B\n  classDef c background-image:u\\72 l(//cdn.example.test/x.png)",
    'graph TD\n  A-->B\n  classDef c background:image-set("https://cdn.example.test/x.png" 1x)',
    '%%{init: {"themeCSS": "@import \\"https://cdn.example.test/t.css\\";"}}%%\ngraph TD\n  A-->B',
    '%%{init: {"themeCSS": ".node rect { fill: \\\\75 rl(https://cdn.example.test/x) }"}}%%\ngraph TD\n  A-->B',
    "---\nconfig:\n  themeCSS: '@font-face { font-family: x; src: local(x) }'\n---\ngraph TD\n  A-->B",
    'graph TD\n  A["%3Cimg src=//cdn.example.test/a.png%3E"]',
]
SAFE_DIAGRAMS = [
    'graph TD\n  A["calls https://api.example.test/v1"] --> B{size < image limit?}',
    'graph TD\n  A-->B\n  click A "https://example.test/docs" _blank',
    'graph TD\n  A@{ shape: rounded, label: "curl (download)" }\n  style A fill:#f9f,stroke:#333',
    "sequenceDiagram\n  Alice->>Bob: GET /img/logo.png\n  Note over Bob: url parsing",
]


def test_diagram_resources_are_refused_however_they_are_spelled() -> None:
    for diagram in FETCHING_DIAGRAMS:
        assert diagram_problem(diagram), diagram
    for diagram in SAFE_DIAGRAMS:
        assert diagram_problem(diagram) is None, diagram


def test_a_diagram_that_loads_a_resource_fails_the_build_and_keeps_the_last_good_site(
    repo: Path, tmp_path: Path
) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    mermaid = tmp_path / "mermaid.min.js"
    mermaid.write_text("/* a local mermaid build */")
    build_site([RepoEntry("W", repo)], out, mermaid=str(mermaid), log=lambda _m: None, now=NOW)
    good = (out / "w/1-overview.html").read_text()
    page = repo / "docs/wiki/1-overview.md"
    for diagram in FETCHING_DIAGRAMS[:1] + FETCHING_DIAGRAMS[5:6] + FETCHING_DIAGRAMS[8:9]:
        added = f"\n\n```mermaid\n{diagram}\n```\n"
        page.write_text(page.read_text() + added)
        problems = validate(repo)
        assert any("offline site policy: this diagram" in str(p) for p in problems), (diagram, problems)
        with pytest.raises(SiteError, match="failed validation"):
            build_site([RepoEntry("W", repo)], out, mermaid=str(mermaid), log=lambda _m: None, now=NOW)
        assert (out / "w/1-overview.html").read_text() == good
        page.write_text(page.read_text().replace(added, ""))


def test_the_build_refuses_a_diagram_resource_even_past_validation(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import repowiki.validate

    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + f"\n\n```mermaid\n{FETCHING_DIAGRAMS[0]}\n```\n")
    monkeypatch.setattr(repowiki.validate, "diagram_problem", lambda _d: None)  # validation misses it
    with pytest.raises(SiteError, match="offline site policy: this diagram has an image node"):
        build_site([RepoEntry("W", repo)], tmp_path / "site", log=lambda _m: None, now=NOW)
    assert not (tmp_path / "site").exists()


def test_every_generated_page_tells_the_browser_to_fetch_nothing_from_elsewhere(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo, export_sources=True)], out, log=lambda _m: None, now=NOW)
    pages = list(out.rglob("*.html"))
    assert len(pages) > 3
    for f in pages:
        head = f.read_text().split("<body", 1)[0]
        assert f'<meta http-equiv="Content-Security-Policy" content="{OFFLINE_CSP}">' in head, f
    assert "default-src 'self'" in OFFLINE_CSP and "img-src" not in OFFLINE_CSP  # images fall back to 'self'
