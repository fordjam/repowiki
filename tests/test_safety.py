"""Blocker (a): nothing outside the wiki directory is ever written or deleted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FILES
from repowiki import generate as gen
from repowiki.config import Settings
from repowiki.runners import FakeRunner
from repowiki.safety import GENERATED_MARK, UnsafePathError, check_wiki_dir
from repowiki.validate import validate


def run(repo: Path, **kw: object) -> gen.Result:
    settings = Settings(cheap="fake", min_pages=2, **kw)  # type: ignore[arg-type]
    fake = FakeRunner()
    settings.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return gen.generate(repo, settings, log=lambda _msg: None)


def hostile_index(repo: Path, files: list[str]) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    pages = [{"file": f, "title": "x", "parent": None} for f in files]
    (wiki / "index.json").write_text(json.dumps({"pages": pages}))


def test_hostile_index_cannot_delete_files_outside_the_wiki(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text(f"precious\n{GENERATED_MARK}\n")
    hostile_index(repo, ["../../README.md", str(outside), "../../src/widgetry/cli.py"])
    run(repo)
    assert (repo / "README.md").read_text() == FILES["README.md"]
    assert (repo / "src/widgetry/cli.py").read_text() == FILES["src/widgetry/cli.py"]
    assert outside.read_text().startswith("precious")


def test_pages_repowiki_did_not_write_are_never_deleted(repo: Path) -> None:
    run(repo)
    mine = repo / "docs/wiki/notes.md"
    mine.write_text("# My notes\n\nHand-written.\n")
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    index["pages"].append({"file": "notes.md", "title": "Notes", "parent": None})
    (repo / "docs/wiki/index.json").write_text(json.dumps(index))
    result = run(repo)
    assert mine.read_text() == "# My notes\n\nHand-written.\n"
    assert any("notes.md" in w and "not written by repowiki" in w for w in result.warnings)


@pytest.mark.parametrize("bad", ["../outside", "/tmp/wiki", "docs/../../x", ".git/wiki", "", "~/wiki", "C:/wiki"])
def test_wiki_dir_must_stay_inside_the_repository(bad: str) -> None:
    with pytest.raises(ValueError):
        Settings(wiki_dir=bad)
    with pytest.raises(UnsafePathError):
        check_wiki_dir(bad)
    assert check_wiki_dir("./docs//wiki/") == "docs/wiki"


def test_symlinked_wiki_dir_escaping_the_repo_is_refused(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (repo / "docs").mkdir()
    (repo / "docs/wiki").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError, match="outside the repository"):
        run(repo)
    assert list(outside.iterdir()) == []


def test_writes_never_follow_a_symlinked_page(repo: Path, tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched\n")
    (repo / "docs/wiki").mkdir(parents=True)
    (repo / "docs/wiki/1-overview.md").symlink_to(victim)
    with pytest.raises(UnsafePathError, match="symlink"):
        run(repo)
    assert victim.read_text() == "untouched\n"
    assert not (repo / "docs/wiki/index.json").exists()


def test_validate_refuses_to_read_through_symlinks(repo: Path, tmp_path: Path) -> None:
    run(repo)
    secret = tmp_path / "secret.txt"
    secret.write_text("# not a page\n")
    page = repo / "docs/wiki/1-overview.md"
    page.unlink()
    page.symlink_to(secret)
    assert any("symlink" in p.message for p in validate(repo))
