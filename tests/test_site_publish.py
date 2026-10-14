"""Publication is durable: immutable releases in an owned store, one atomic pointer, recovery.

Every kill-point scenario simulates a termination at the dangerous step (the pointer replacement, a
legacy move) by failing that step after everything before it succeeded, then checks what a reader
would see through the output path. Migration scenarios drive the journal the way a crash would have
left it and let recovery finish the work.
"""

from __future__ import annotations

import fcntl
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import commit
from repowiki import site_publish
from repowiki.config import Settings
from repowiki.generate import generate
from repowiki.runners import FakeRunner
from repowiki.site import MARKER, RepoEntry, SiteError, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def with_wiki(repo: Path) -> None:
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    commit(repo, "wiki")


def build(repo: Path, out: Path, title: str = "Repository wikis", **kwargs: object) -> None:
    build_site(
        [RepoEntry("W", repo)], out, site_title=title, now=kwargs.pop("now", NOW), log=lambda _m: None, **kwargs
    )


def served(out: Path) -> str:
    return (out / "index.html").read_text()


def releases(out: Path) -> list[str]:
    store = site_publish.store_path(out)
    return sorted(p.name for p in (store / "releases").iterdir() if not p.name.startswith("."))


def test_output_is_a_managed_pointer_into_an_owned_store(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out)
    assert out.is_symlink() and (out / MARKER).is_file()
    store = site_publish.store_path(out)
    assert (store / "releases").is_dir() and (store / "current").is_symlink()
    assert (store / ".repowiki-store.json").is_file()
    assert len(releases(out)) == 1
    assert "Repository wikis" in served(out)


def test_repeated_publish_and_recovery_are_unchanged(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out)
    first = releases(out)
    target = Path(os.path.realpath(out))
    store = site_publish.store_path(out)
    site_publish.recover(store, out, marker=MARKER)
    site_publish.recover(store, out, marker=MARKER)
    build(repo, out)  # identical content: the same release is reused, nothing new appears
    assert releases(out) == first and Path(os.path.realpath(out)) == target
    assert "Repository wikis" in served(out)


def test_kill_before_or_after_the_pointer_swap_serves_a_complete_release(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out, title="Old")
    original = site_publish._point_current

    def die_before(store: Path, digest: str) -> None:
        raise site_publish.PublicationError("killed before the pointer replacement")

    def die_after(store: Path, digest: str) -> None:
        original(store, digest)
        raise site_publish.PublicationError("killed after the pointer replacement")

    monkeypatch.setattr(site_publish, "_point_current", die_before)
    with pytest.raises(SiteError, match="killed"):
        build(repo, out, title="New")
    assert "Old" in served(out) and served(out) == (out / "index.html").read_text()
    monkeypatch.setattr(site_publish, "_point_current", die_after)
    with pytest.raises(SiteError, match="killed"):
        build(repo, out, title="New")
    assert "New" in served(out)  # the flip had already happened: the complete new release is live
    monkeypatch.setattr(site_publish, "_point_current", original)
    store = site_publish.store_path(out)
    site_publish.recover(store, out, marker=MARKER)
    build(repo, out, title="New")
    assert "New" in served(out) and len(releases(out)) == 2  # both complete releases are retained


def test_failure_before_the_pointer_retains_the_last_good_site(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out, title="Good")
    good = served(out)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "\n\n![gone](../../assets/missing.png)\n")
    with pytest.raises(SiteError, match="failed validation"):
        build(repo, out, title="Bad")
    assert served(out) == good  # the last good release is untouched and still served


def test_two_publishers_conflict_safely(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out)
    store = site_publish.store_path(out)
    fd = os.open(store / site_publish.LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SiteError, match="already in progress"):
            build(repo, out, title="Second")
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    build(repo, out, title="Second")  # the lock is released with the holder: publishing works again
    assert "Second" in served(out)


def test_corrupt_or_symlinked_release_manifest_refuses(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out)
    store = site_publish.store_path(out)
    manifest = Path(os.path.realpath(out)) / site_publish.RELEASE_MANIFEST
    manifest.write_text("{ not json")
    with pytest.raises(SiteError, match="corrupt"):
        build(repo, out, title="Again")
    manifest.unlink()
    manifest.symlink_to(store / "journal.json")
    with pytest.raises(SiteError, match="symlink"):
        build(repo, out, title="Again")


def test_legacy_output_needs_explicit_migration_then_converts(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    out.mkdir()
    (out / MARKER).write_text("generated by repowiki; safe to delete\n", encoding="utf-8")
    (out / "index.html").write_text("<html>legacy</html>")
    with pytest.raises(SiteError, match="migration"):
        build(repo, out)
    assert (out / "index.html").read_text() == "<html>legacy</html>"  # nothing moved without consent
    build(repo, out, migrate=True)
    assert out.is_symlink() and "Repository wikis" in served(out)
    store = site_publish.store_path(out)
    legacy = [p for p in (store / "releases").iterdir() if p.is_dir()]
    assert any((p / "index.html").read_text() == "<html>legacy</html>" for p in legacy)  # kept intact


def test_unowned_output_refuses_migration(tmp_path: Path) -> None:
    out = tmp_path / "site"
    out.mkdir()
    (out / "keep.txt").write_text("mine")
    with pytest.raises(site_publish.PublicationError, match="not a repowiki site"):
        site_publish.migrate_legacy(out, now=NOW, marker=MARKER)
    assert (out / "keep.txt").exists()


def test_interrupted_legacy_migration_recovers(repo: Path, tmp_path: Path) -> None:
    # Killed after the journal, before the move: the legacy directory is still the live site.
    out = tmp_path / "site"
    out.mkdir()
    (out / MARKER).write_text("generated by repowiki; safe to delete\n", encoding="utf-8")
    (out / "index.html").write_text("<html>legacy</html>")
    store = site_publish.store_path(out)
    site_publish.ensure_store(store)
    digest = site_publish.tree_digest(out)
    site_publish._write_journal(store, {"op": "migrate", "id": digest, "created": NOW.isoformat()})
    site_publish.recover(store, out, marker=MARKER)
    assert out.is_symlink() and "legacy" in served(out)

    # Killed after the move, before the pointer: the release is in the store, the output is missing.
    out2 = tmp_path / "other"
    out2.mkdir()
    (out2 / MARKER).write_text("generated by repowiki; safe to delete\n", encoding="utf-8")
    (out2 / "index.html").write_text("<html>old</html>")
    store2 = site_publish.store_path(out2)
    site_publish.ensure_store(store2)
    digest2 = site_publish.tree_digest(out2)
    site_publish._write_release_manifest(out2, digest2, NOW, "legacy")
    site_publish._write_journal(store2, {"op": "migrate", "id": digest2, "created": NOW.isoformat()})
    os.rename(out2, store2 / "releases" / digest2)
    site_publish.recover(store2, out2, marker=MARKER)
    assert out2.is_symlink() and "old" in (out2 / "index.html").read_text()


def test_previous_releases_are_kept_for_thirty_days(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out, title="First", now=NOW - timedelta(days=40))
    build(repo, out, title="Second", now=NOW - timedelta(days=10))
    build(repo, out, title="Third", now=NOW)
    names = releases(out)
    assert len(names) == 2  # the 40-day-old release is pruned, the 10-day-old one is retained
    assert "Third" in served(out)
    store = site_publish.store_path(out)
    for name in names:
        manifest = json.loads((store / "releases" / name / site_publish.RELEASE_MANIFEST).read_text())
        assert datetime.fromisoformat(manifest["created"]) >= NOW - site_publish.RETENTION


def test_search_data_and_assets_carry_no_release_dependency(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    out = tmp_path / "site"
    build(repo, out)
    page = (out / "w/1-overview.html").read_text()
    assert 'href="../assets/style.css"' in page  # asset urls are stable within a served release


def test_a_build_never_reads_the_clock_itself(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import repowiki.site as site_module

    class NoClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            raise AssertionError("build_site read the wall clock")

    monkeypatch.setattr(site_module, "datetime", NoClock)
    with_wiki(repo)
    out = tmp_path / "site"
    with pytest.raises(TypeError, match="now"):
        build_site([RepoEntry("W", repo)], out, log=lambda _m: None)  # type: ignore[call-arg]
    with pytest.raises(SiteError, match="timezone-aware"):
        build_site([RepoEntry("W", repo)], out, now=datetime(2026, 6, 1, 12, 0), log=lambda _m: None)
    build(repo, out, now=NOW + timedelta(minutes=7))
    assert "Built 2026-06-01 12:07 UTC" in served(out)  # the home page stamp is the given instant
    manifest = json.loads((out / site_publish.RELEASE_MANIFEST).read_text())
    assert manifest["created"] == (NOW + timedelta(minutes=7)).isoformat()  # and so is the release's


def test_the_cli_reads_the_clock_once_and_passes_it_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from repowiki import cli

    seen: list[datetime] = []
    reads: list[int] = []

    class OneClock(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:  # type: ignore[override]
            reads.append(1)
            return NOW

    monkeypatch.setattr(cli, "datetime", OneClock)
    monkeypatch.setattr(cli, "build_site", lambda *a, now, **k: seen.append(now) or [])
    args = cli.parser().parse_args(["site", str(tmp_path), "--out", str(tmp_path / "out")])
    monkeypatch.setattr(cli, "entries_for_paths", lambda *a, **k: [])
    assert cli.cmd_site(args) == 0
    assert seen == [NOW] and reads == [1]
