from __future__ import annotations

import json
from pathlib import Path

from conftest import adopt_page, commit, write
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.update import plan_update, update


def settings_with(fake: FakeRunner) -> Settings:
    s = Settings(cheap="fake", min_pages=2)
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return s


def pages_citing(repo: Path, rel: str) -> set[str]:
    wiki = repo / "docs/wiki"
    return {p.name for p in wiki.glob("*.md") if f"../../{rel}" in p.read_text()}


def test_update_rewrites_only_pages_citing_changed_files(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    commit(repo, "add wiki")  # wiki-only commits never make pages stale
    before = {p.name: p.read_text() for p in (repo / "docs/wiki").glob("*.md")}

    write(repo, "src/widgetry/store/db.py", "class Store:\n    pass\n")
    new_sha = commit(repo, "shrink store")
    expected = pages_citing(repo, "src/widgetry/store/db.py")
    assert expected and len(expected) < len(before)

    up, _, _ = plan_update(repo, settings_with(fake))
    assert up.changed == {"src/widgetry/store/db.py"}
    assert set(up.stale) == expected

    fake.calls.clear()
    result = update(repo, settings_with(fake), log=lambda _m: None)
    assert set(result.written) == expected
    assert result.problems == []
    assert all("## Previous version" in c for c in fake.calls)
    for name, text in before.items():
        if name not in expected:
            assert (repo / "docs/wiki" / name).read_text() == text
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    assert index["indexed_commit"] == new_sha


def test_update_without_relevant_changes_only_moves_the_commit(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    write(repo, "NOTES.txt", "unrelated\n")
    sha = commit(repo, "notes")
    fake.calls.clear()
    result = update(repo, settings_with(fake), log=lambda _m: None)
    assert result.written == [] and fake.calls == []
    assert json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"] == sha


def test_dry_run_writes_nothing(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    index_before = (repo / "docs/wiki/index.json").read_text()
    write(repo, "src/widgetry/cli.py", "def main():\n    return 0\n")
    commit(repo, "cli")
    result = update(repo, settings_with(fake), log=lambda _m: None, dry_run=True)
    assert result.plan.stale and result.written == []
    assert (repo / "docs/wiki/index.json").read_text() == index_before


def test_r3_dry_run_never_builds_or_verifies_runners(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 9\n")
    commit(repo, "cli")
    settings = Settings(cheap="fake", min_pages=2)

    def refuse() -> None:
        raise AssertionError("a dry run must not build or verify runners")

    settings.runners = refuse  # type: ignore[method-assign]
    logged: list[str] = []
    result = update(repo, settings, log=logged.append, dry_run=True)
    assert result.plan.stale and result.written == []
    assert any(line.startswith("Preview digest: ") for line in "\n".join(logged).splitlines())


def test_r4_pages_citing_pinned_web_urls_become_stale(repo: Path) -> None:
    """Dependency extraction reads web citations into the registered identity, not just relative links."""
    from conftest import run_git

    run_git(repo, "remote", "add", "origin", "https://code.example.test/example/widgetry.git")
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    sha = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    wiki = repo / "docs/wiki"
    target = next(p for p in sorted(wiki.glob("*.md")) if "../../src/widgetry/store/db.py" not in p.read_text())
    url = f"https://code.example.test/example/widgetry/blob/{sha}/src/widgetry/store/db.py#L1-L2"
    target.write_text(target.read_text() + f"\nSee [db.py:1-2]({url}).\n")
    foreign = next(p for p in sorted(wiki.glob("*.md")) if p != target and "store/db.py" not in p.read_text())
    other = f"https://code.example.test/other/widgetry/blob/{sha}/src/widgetry/store/db.py#L1"
    foreign.write_text(foreign.read_text() + f"\nSee [elsewhere]({other}).\n")
    commit(repo, "wiki with web citations")
    write(repo, "src/widgetry/store/db.py", "class Store:\n    pass\n")
    commit(repo, "shrink store")
    up, _, _ = plan_update(repo, settings_with(fake))
    assert target.name in up.stale and up.stale[target.name] == ["src/widgetry/store/db.py"]
    assert foreign.name not in up.stale  # another repository's file is not a dependency


def test_r4_unchanged_page_with_a_pinned_web_citation_survives_update(repo: Path) -> None:
    """A page that is not rewritten keeps its evidence: web citations pinned to the old indexed commit are
    re-expressed as canonical relative links before the index moves to the new commit."""
    from conftest import run_git

    run_git(repo, "remote", "add", "origin", "https://code.example.test/example/widgetry.git")
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    old = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    wiki = repo / "docs/wiki"
    page = sorted(wiki.glob("*.md"))[0]
    url = f"https://code.example.test/example/widgetry/blob/{old}/src/widgetry/engine.py#L30-L31"
    page.write_text(page.read_text() + f"\nSee [engine.py:30-31]({url}).\n")
    adopt_page(page)
    commit(repo, "hand-edited wiki")
    write(repo, "NOTES.txt", "unrelated\n")
    new = commit(repo, "notes")
    result = update(repo, settings_with(fake), log=lambda _m: None)
    assert result.problems == [] and result.written == []
    assert json.loads((wiki / "index.json").read_text())["indexed_commit"] == new
    text = page.read_text()
    assert url not in text and "[engine.py:30-31](../../src/widgetry/engine.py#L30-L31)" in text


def test_r4_kept_page_citation_forms_all_survive_update(repo: Path) -> None:
    from conftest import run_git

    run_git(repo, "remote", "add", "origin", "https://code.example.test/example/widgetry.git")
    fake = FakeRunner()
    generate(repo, settings_with(fake), log=lambda _m: None)
    old = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    page = sorted((repo / "docs/wiki").glob("*.md"))[0]
    url = f"https://code.example.test/example/widgetry/blob/{old}/src/widgetry/engine.py#L30-L31"
    page.write_text(page.read_text() + f"\nSee [`engine.py`]({url}), <{url}> and [ref].\n\n[ref]:\n  {url}\n")
    adopt_page(page)
    commit(repo, "hand-edited wiki")
    write(repo, "NOTES.txt", "unrelated\n")
    commit(repo, "notes")
    result = update(repo, settings_with(fake), log=lambda _m: None)
    assert result.problems == [] and url not in page.read_text()
