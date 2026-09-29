from __future__ import annotations

import json
from pathlib import Path

from conftest import commit, write
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
