"""R6: the frozen validation reader, and a site that renders exactly the wiki it validated."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import commit, run_git, write
from repowiki import safety, snapshot
from repowiki import site as site_module
from repowiki.git import Snapshot
from repowiki.site import RepoEntry, SiteError, build_site
from repowiki.snapshot import read_frozen
from repowiki.validate import validate

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

DETAILS = "<details>\n<summary>Relevant source files</summary>\n\n- [cli.py](../../src/widgetry/cli.py)\n\n</details>\n"


def page(title: str, body: str) -> str:
    return f"# {title}\n\n{DETAILS}\n{body}\n"


def make_wiki(repo: Path, pages: dict[str, str], *, sha: str | None = None, message: str | None = "wiki") -> None:
    sha = sha or run_git(repo, "rev-parse", "HEAD")
    for name, text in pages.items():
        write(repo, f"docs/wiki/{name}", text)
    index = {
        "repo": "widgetry",
        "indexed_commit": sha,
        "indexed_at": "2026-01-02T03:04:05+00:00",
        "pages": [{"file": n, "title": n[2:-3], "parent": None} for n in pages],
    }
    write(repo, "docs/wiki/index.json", json.dumps(index))
    if message:
        commit(repo, message)


def eight_pages(bad: int | None = None) -> dict[str, str]:
    pages = {}
    for n in range(1, 9):
        line = 99 if n == bad else n
        pages[f"{n}-p{n}.md"] = page(f"P{n}", f"Cites [cli.py:{line}](../../src/widgetry/cli.py#L{line}).")
    return pages


def snapshot_of(out: Path) -> dict[str, str]:
    return {str(p.relative_to(out)): p.read_text() for p in sorted(out.rglob("*")) if p.is_file()}


def test_r6_missing_requested_index_fails(repo: Path, tmp_path: Path) -> None:
    frozen = read_frozen(repo)
    assert not frozen.ok and "not found" in frozen.errors[0].message
    assert not read_frozen(repo, ref="main").ok
    with pytest.raises(SiteError, match="index.json: error: not found"):
        build_site([RepoEntry("W", repo)], tmp_path / "site", log=lambda _m: None, now=NOW)
    assert not (tmp_path / "site").exists()


def test_r6_one_bad_range_among_eight_fails_the_whole_build(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "site"
    make_wiki(repo, eight_pages())
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None, now=NOW)
    good = snapshot_of(out)
    assert len([f for f in good if f.startswith("w/") and f.endswith(".html")]) == 9  # eight pages + index
    make_wiki(repo, eight_pages(bad=5))
    with pytest.raises(SiteError, match=r"5-p5.md:\d+: error: cites lines 99-99 but src/widgetry/cli.py has 9"):
        build_site([RepoEntry("W", repo)], out, log=lambda _m: None, now=NOW)
    assert snapshot_of(out) == good  # zero good-output replacements
    assert not [p for p in out.parent.iterdir() if p.name.startswith(".site-")]  # no stray temp output


def test_r6_duplicate_slug_fails_before_output_creation(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_wiki(repo, eight_pages())
    read: list[Path] = []
    real = snapshot.read_frozen
    monkeypatch.setattr("repowiki.site.read_frozen", lambda root, *a, **k: read.append(root) or real(root, *a, **k))
    with pytest.raises(SiteError, match="duplicate or reserved repo name"):
        build_site(
            [RepoEntry("Widgetry", repo), RepoEntry("widgetry", repo)], tmp_path / "site", log=lambda _m: None, now=NOW
        )
    assert read == [] and not (tmp_path / "site").exists()


def test_r6_l42_passes_l43_fails_and_the_last_good_site_stays(repo: Path, tmp_path: Path) -> None:
    write(repo, "src/widgetry/long.py", "".join(f"x{n} = {n}\n" for n in range(42)))
    sha = commit(repo, "a 42-line file") and run_git(repo, "rev-parse", "HEAD")
    out = tmp_path / "site"
    make_wiki(repo, {"1-a.md": page("A", "[end](../../src/widgetry/long.py#L42)")}, sha=sha)
    entry = RepoEntry("W", repo, url="https://code.example.test/example/widgetry", ref="main")
    build_site([entry], out, log=lambda _m: None, now=NOW)
    good = snapshot_of(out)
    assert "src/widgetry/long.py?plain=1#L42" in good["w/1-a.html"]
    make_wiki(repo, {"1-a.md": page("A", "[past](../../src/widgetry/long.py#L43)")}, sha=sha)
    with pytest.raises(SiteError, match="cites lines 43-43 but src/widgetry/long.py has 42 lines"):
        build_site([entry], out, log=lambda _m: None, now=NOW)
    assert snapshot_of(out) == good


def test_r6_branch_movement_cannot_change_the_opened_commits(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_wiki(repo, {"1-a.md": page("A", "Original [cli.py:1](../../src/widgetry/cli.py#L1).")})
    opened = run_git(repo, "rev-parse", "main")
    real = Snapshot.read
    moved: list[str] = []

    def read_then_move(self: Snapshot, path: str, limit: int | None = None) -> str | None:
        text = real(self, path, limit)
        if path.startswith("docs/wiki/") and not moved:  # main moves while the wiki is being read
            make_wiki(repo, {"1-a.md": page("A", "Moved [cli.py:2](../../src/widgetry/cli.py#L2).")}, message="move")
            moved.append(run_git(repo, "rev-parse", "main"))
        return text

    monkeypatch.setattr(Snapshot, "read", read_then_move)
    frozen = read_frozen(repo, ref="main")
    assert moved and moved[0] != opened
    assert frozen.ok and frozen.wiki_commit == opened and frozen.source_commit == json.loads(
        run_git(repo, "show", f"{opened}:docs/wiki/index.json")
    )["indexed_commit"]
    assert "Original" in frozen.texts["1-a.md"] and "Moved" not in frozen.texts["1-a.md"]


def test_r6_working_tree_mode_reads_one_locked_coherent_set(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_wiki(repo, eight_pages(), message=None)
    wiki = repo / "docs/wiki"
    lock = safety._lock_path(wiki)
    held: list[bool] = []
    real = snapshot.read_wiki_file

    def read(w: Path, name: str) -> str | None:
        held.append((os.getpid(), lock) in getattr(safety._held, "locks", {}))
        return real(w, name)

    monkeypatch.setattr(snapshot, "read_wiki_file", read)
    frozen = read_frozen(repo)
    assert frozen.ok and frozen.wiki_commit is None
    assert len(held) == 9 and all(held)  # the index and all eight pages, every one under the wiki lock


def test_r6_committed_reads_do_not_recover_the_working_tree(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    make_wiki(repo, eight_pages())

    def no(*_a: object) -> bool:
        raise AssertionError("a committed read touched working-tree recovery")

    monkeypatch.setattr(snapshot, "recover", no)
    assert read_frozen(repo, ref="main").ok


def test_r6_committed_symlink_page_is_refused(repo: Path) -> None:
    make_wiki(repo, eight_pages(), message=None)
    target = repo / "docs/wiki/1-p1.md"
    target.unlink()
    target.symlink_to("2-p2.md")
    commit(repo, "symlinked page")
    frozen = read_frozen(repo, ref="main")
    assert not frozen.ok and any("not a regular file" in p.message for p in frozen.errors)


def test_r6_validate_and_site_share_one_reader(repo: Path) -> None:
    make_wiki(repo, eight_pages(bad=3))
    assert validate(repo) == read_frozen(repo).problems
    assert [p.message for p in validate(repo) if p.level == "error"] == [p.message for p in read_frozen(repo).errors]


def test_r6_committed_policy_exclusions_cannot_be_loosened_by_the_working_tree(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from conftest import register

    policy = register(repo, monkeypatch, config_prefix='exclude = ["internal/*"]\n')
    write(repo, "internal/notes.py", "PRIVATE = 1\n")
    commit(repo, "internal notes")
    make_wiki(repo, {"1-a.md": page("A", "See [notes](../../internal/notes.py#L1).")})
    cfg = repo / ".repowiki.toml"
    cfg.write_text(cfg.read_text().replace('exclude = ["internal/*"]\n', ""))  # uncommitted loosening
    entry = RepoEntry("W", repo, ref="main", export_sources=True)
    out = tmp_path / "site"
    with pytest.raises(SiteError, match="excluded by the secret policy"):
        build_site([entry], out, log=lambda _m: None, now=NOW)  # the selected policy commit
    monkeypatch.delenv("REPOWIKI_POLICY_COMMIT")
    with pytest.raises(SiteError, match="excluded by the secret policy"):
        build_site([entry], out, log=lambda _m: None, now=NOW)  # no selection: the wiki commit's own policy
    assert not out.exists()
    frozen = read_frozen(repo, ref="main", policy_commit=policy)
    assert frozen.policy_commit == policy and frozen.policy.secret("internal/notes.py")


@pytest.mark.parametrize("selected", ["0" * 40, "no-such-policy", "--output=x"])
def test_r6_an_unresolved_policy_selection_fails_closed(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str
) -> None:
    """The reviewed policy excluded internal/*; this clone lacks that commit and main's policy does not.
    Falling back to main's policy would validate, link and export the excluded file."""
    write(repo, "internal/notes.py", "PRIVATE = 1\n")
    commit(repo, "internal notes")
    make_wiki(repo, {"1-a.md": page("A", "See [notes](../../internal/notes.py#L1).")})
    entry = RepoEntry("W", repo, ref="main", export_sources=True)
    monkeypatch.setenv("REPOWIKI_POLICY_COMMIT", selected)
    out = tmp_path / "site"
    with pytest.raises(SiteError, match="REPOWIKI_POLICY_COMMIT"):
        build_site([entry], out, log=lambda _m: None, now=NOW)
    with pytest.raises(SiteError, match="REPOWIKI_POLICY_COMMIT"):
        site_module.collect(entry)
    assert not out.exists() and not list(tmp_path.glob(".site-*"))
    monkeypatch.delenv("REPOWIKI_POLICY_COMMIT")
    build_site([entry], out, log=lambda _m: None, now=NOW)  # no selection: main's own policy, as documented
    assert (out / "w/_source/internal/notes.py.html").exists()


def test_r6_every_wiki_is_validated_before_any_is_rendered(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_wiki(repo, eight_pages())
    other = tmp_path / "other"
    other.mkdir()
    run_git(other, "init", "-q", "-b", "main")
    write(other, "a.txt", "a\n")
    commit(other, "no wiki")
    rendered: list[str] = []
    real = site_module.render
    monkeypatch.setattr(site_module, "render", lambda text, *a, **k: rendered.append(text) or real(text, *a, **k))
    with pytest.raises(SiteError, match="other"):
        build_site([RepoEntry("W", repo), RepoEntry("other", other)], tmp_path / "site", log=lambda _m: None, now=NOW)
    assert rendered == []


def test_r6_an_untrusted_transaction_record_is_a_problem_not_a_crash(repo: Path) -> None:
    make_wiki(repo, eight_pages())
    wiki = repo / "docs/wiki"
    safety.transaction_dir(wiki).mkdir(parents=True, exist_ok=True)
    (safety.transaction_dir(wiki) / "stage").mkdir(exist_ok=True)
    safety.transaction_record(wiki).write_text("not json")
    frozen = read_frozen(repo)
    assert not frozen.ok and "untrusted transaction record" in frozen.errors[0].message
    assert read_frozen(repo, ref="main").ok  # a committed read never touches the working tree's transaction
