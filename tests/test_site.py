from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit, run_git, write
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.render import clean_html, render
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, SiteError, badge, build_site, load_repos, web_link


def with_wiki(repo: Path) -> None:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")


def test_badge_thresholds() -> None:
    assert "badge-fresh" in badge(0)
    assert "badge-amber" in badge(1) and "1 commit behind" in badge(1)
    assert "badge-amber" in badge(50)
    assert "badge-red" in badge(51)
    assert "badge-unknown" in badge(None)


def test_site_renders_pages_chips_sources_and_staleness(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    for n in range(3):
        write(repo, f"src/widgetry/f{n}.py", "x = 1\n")
        commit(repo, f"code {n}")
    out = tmp_path / "site"
    sites = build_site([RepoEntry("Widgetry", repo, export_sources=True)], out, log=lambda _m: None)
    assert sites[0].behind == 3  # the wiki-only commit is not counted
    home = (out / "index.html").read_text()
    assert "3 commits behind" in home and "badge-amber" in home
    page = (out / "widgetry/1-overview.html").read_text()
    assert 'class="cite"' in page and '<span class="cite-file">src/widgetry/cli.py</span>' in page
    assert 'href="_source/src/widgetry/cli.py.html#L1-L' in page
    assert '<pre class="mermaid">' in page and "mermaid.esm.min.mjs" in page
    assert "<details>" in page and "<summary>" in page
    src = (out / "widgetry/_source/src/widgetry/cli.py.html").read_text()
    assert 'id="L1"' in src and "../../../../assets/style.css" in src
    assert (out / "assets/style.css").is_file() and (out / ".repowiki-site").is_file()
    assert "1-overview.html" in (out / "widgetry/index.html").read_text()


def test_web_url_links_are_pinned_to_indexed_commit(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    sha = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo, url="https://github.com/example/widgetry")], out, log=lambda _m: None)
    page = (out / "w/1-overview.html").read_text()
    assert f"https://github.com/example/widgetry/blob/{sha}/src/widgetry/cli.py?plain=1#L1-L" in page
    assert not (out / "w/_source").exists()


def test_web_link_styles() -> None:
    assert web_link("https://gitlab.example.com/a/b", "abc", "x.py", (3, 9), False).endswith("/-/blob/abc/x.py#L3-9")
    assert web_link("https://github.com/a/b", "abc", "src", None, True) == "https://github.com/a/b/tree/abc/src"
    assert web_link("https://github.com/a/b", "abc", "x.py", (4, 4), False).endswith("x.py?plain=1#L4")


def test_red_badge_after_many_commits(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    for n in range(51):
        write(repo, "CHANGELOG.md", f"change {n}\n")
        commit(repo, f"c{n}")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", "empty commits touch nothing and are not counted")
    sites = build_site([RepoEntry("W", repo)], tmp_path / "site", log=lambda _m: None)
    assert sites[0].behind == 51
    assert "badge-red" in (tmp_path / "site/index.html").read_text()


def test_many_repos_from_repos_json(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    empty = tmp_path / "empty"
    empty.mkdir()
    run_git(empty, "init", "-q", "-b", "main")
    write(empty, "a.txt", "a\n")
    commit(empty)
    cfg = tmp_path / "repos.json"
    cfg.write_text(json.dumps({"repos": [{"name": "Widgetry", "path": str(repo)}, {"path": "empty"}]}))
    entries = load_repos(cfg)
    assert [e.name for e in entries] == ["Widgetry", "empty"]
    build_site(entries, tmp_path / "site", log=lambda _m: None)
    home = (tmp_path / "site/index.html").read_text()
    assert "Widgetry" in home and "wiki not written yet" in home


def test_reading_wiki_from_a_git_ref(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    (repo / "docs/wiki/1-overview.md").write_text("# Uncommitted edit\n")
    build_site([RepoEntry("W", repo, ref="main")], tmp_path / "site", log=lambda _m: None)
    assert "Uncommitted edit" not in (tmp_path / "site/w/1-overview.html").read_text()


def test_refuses_to_overwrite_foreign_directory(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "precious"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    with pytest.raises(SiteError, match="refusing"):
        build_site([RepoEntry("W", repo)], out, log=lambda _m: None)
    assert (out / "keep.txt").exists()


def test_raw_html_is_sanitised() -> None:
    html = clean_html('<details open class="x"><summary onclick="x()">S</summary><script>alert(1)</script></details>')
    assert html.startswith("<details open><summary>S</summary>")
    assert "<script>" not in html and "&lt;script&gt;" in html
    r = render('# T\n\n<img src=x onerror="alert(1)">\n\n[a](../../x.py#L1-L2)\n')
    assert "<img" not in r.html and r.title == "T"


def test_citation_chip_versus_prose_link() -> None:
    from repowiki.render import Target

    def resolve(href: str) -> Target:
        return Target("x.html#L4-L5", "src/tool/core.py", (4, 5))

    chip = render("[core.py:4-5](a)", resolve).html
    assert 'class="cite"' in chip and "cite-file" in chip
    prose = render("[the add function](a)", resolve).html
    assert 'class="cite-labelled"' in prose and "the add function" in prose and "cite-file" not in prose


# --------------------------------------------------------------------------- source export (blocker f)


def test_sources_are_not_exported_by_default(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None)
    assert not (out / "w/_source").exists()
    page = (out / "w/1-overview.html").read_text()
    assert "_source/" not in page and "cli.py" in page


def test_exported_sources_ship_with_licence_and_notice_files(repo: Path, tmp_path: Path) -> None:
    write(repo, "LICENSE", "MIT License\n\nCopyright (c) Example\n")
    write(repo, "src/widgetry/NOTICE", "Portions from an example project.\n")
    commit(repo, "licence")
    with_wiki(repo)
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo, export_sources=True)], out, log=lambda _m: None)
    assert (out / "w/_source/LICENSE.html").is_file()
    assert (out / "w/_source/src/widgetry/NOTICE.html").is_file()
    src = (out / "w/_source/src/widgetry/cli.py.html").read_text()
    assert 'href="../../../_source/src/widgetry/NOTICE.html"' in src
    assert 'href="../../../_source/LICENSE.html"' in src
    assert "MIT License" in (out / "w/_source/LICENSE.html").read_text()


def test_export_without_a_licence_warns(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    logged: list[str] = []
    build_site([RepoEntry("W", repo, export_sources=True)], tmp_path / "site", log=logged.append)
    assert any("no LICENSE" in m for m in logged)
    assert "No LICENSE" in (tmp_path / "site/w/_source/src/widgetry/cli.py.html").read_text()


def test_export_sources_opt_in_from_repos_json_and_cli(repo: Path, tmp_path: Path) -> None:
    from repowiki.cli import main

    with_wiki(repo)
    cfg = tmp_path / "repos.json"
    cfg.write_text(json.dumps({"repos": [{"name": "W", "path": str(repo), "export_sources": True}]}))
    assert load_repos(cfg)[0].export_sources
    assert main(["site", str(repo), "-o", str(tmp_path / "a")]) == 0
    assert not (tmp_path / "a/widgetry/_source").exists()
    assert main(["site", str(repo), "-o", str(tmp_path / "b"), "--export-sources"]) == 0
    assert (tmp_path / "b/widgetry/_source/src/widgetry/cli.py.html").is_file()
