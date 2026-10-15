"""Blocker (e): only a wiki that passes validation is ever written, and failures leave the old one intact."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import generate as gen
from repowiki import ownership, safety
from repowiki.config import Settings
from repowiki.fake import answer
from repowiki.links import find_links, unlink
from repowiki.runners import FakeRunner, RunnerError
from repowiki.safety import GENERATED_MARK
from repowiki.update import update
from repowiki.validate import validate

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the run's instant is the caller's, never the clock


def settings_for(fake: FakeRunner) -> Settings:
    s = Settings(cheap="fake", min_pages=2)
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return s


def run(repo: Path, fake: FakeRunner) -> gen.Result:
    return gen.generate(repo, settings_for(fake), now=NOW, log=lambda _m: None)


def snapshot(repo: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((repo / "docs/wiki").iterdir()) if p.is_file()}


def test_reference_style_bad_links_are_repaired(repo: Path) -> None:
    """The reviewer's scenario: the backend keeps returning a reference-style bad citation."""
    uses = "Good [cli](src/widgetry/cli.py#L1-L3).\n\nAlso [bad][ref], [ref][], [ref] and [pic][ref].\n\n"

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        target = "../../src/widgetry/engine.py#L1-L4" if prompt.startswith("TASK: repair") else "../../missing.py#L1"
        return f'# P\n\n{uses}[ref]: {target} "a title"\n'

    fake = FakeRunner(respond=respond)
    result = run(repo, fake)
    assert any(c.startswith("TASK: repair") for c in fake.calls)
    assert result.problems == [] and validate(repo) == []
    text = (repo / "docs/wiki/1-overview.md").read_text()
    assert "missing.py" not in text and "[bad][ref]" in text and "engine.py#L1-L4" in text
    assert "[cli](../../src/widgetry/cli.py#L1-L3)" in text
    assert any(": corrected: " in w for w in result.warnings)


def test_reference_style_bad_links_cannot_be_repaired_by_dropping_the_definition(repo: Path) -> None:
    uses = "Good [cli](src/widgetry/cli.py#L1-L3).\n\nAlso [bad][ref], [ref][], [ref] and [pic][ref].\n\n"

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        definition = '[ref]: ../../missing.py#L1 "a title"\n'
        return f"# P\n\n{uses}{definition}" if not prompt.startswith("TASK: repair") else f"# P\n\n{uses}"

    with pytest.raises(gen.GenerateError):
        run(repo, FakeRunner(respond=respond))
    assert not (repo / "docs/wiki/index.json").exists()


@pytest.mark.parametrize(
    "markdown",
    [
        "[x](gone.py#L1)",
        "[x]( <gone.py#L1> )",
        '[x](gone.py#L1 "title")',
        "[x](gone.py#L1 'title')",
        "[a [nested] label](gone.py#L1)",
        "[x](<dir with space/gone.py>)",
        "[x](gone(1).py)",
        "![img](gone.png)",
        "[x][R]\n\n[r]: gone.py#L1",
        "[R][]\n\n  [r]: <gone.py#L1>",
        "[r]\n\n[r]: gone.py#L1 (title)",
    ],
)
def test_unlink_handles_every_link_form(markdown: str) -> None:
    bad = {link.href for link in find_links(markdown)}
    assert bad
    out = unlink(markdown, bad)
    assert find_links(out) == [], out


def test_unlink_leaves_code_and_good_links_alone() -> None:
    md = "`[x](gone.py)` and [ok](good.py)\n\n```\n[x](gone.py)\n```\n[x](gone.py)\n"
    out = unlink(md, {"gone.py"})
    assert "`[x](gone.py)`" in out and "```\n[x](gone.py)\n```" in out and "[ok](good.py)" in out
    assert out.endswith("\nx\n")


def test_backend_failure_leaves_existing_wiki_untouched(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    write(repo, "src/widgetry/extra.py", "x = 1\n")
    commit(repo, "more code")

    def flaky(prompt: str) -> str:
        if prompt.startswith("TASK: page") and "Title: Testing" in prompt:
            raise RunnerError("quota exhausted", transient=False)
        return answer(prompt)

    with pytest.raises(gen.GenerateError, match="not changed"):
        run(repo, FakeRunner(respond=flaky))
    assert snapshot(repo) == before


def test_a_page_that_stays_invalid_is_never_written(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)

    def respond(prompt: str) -> str:
        return answer(prompt) if prompt.startswith("TASK: plan") else "# P\n\n[x](src/ghost.py#L1)\n"

    with pytest.raises(gen.GenerateError, match="ghost.py"):
        run(repo, FakeRunner(respond=respond))
    assert snapshot(repo) == before


def test_failed_update_does_not_advance_the_index(repo: Path) -> None:
    run(repo, FakeRunner())
    commit(repo, "wiki")
    before = snapshot(repo)
    write(repo, "src/widgetry/cli.py", "def main():\n    return 2\n")
    commit(repo, "cli")

    def broken(prompt: str) -> str:
        raise RunnerError("backend down")

    with pytest.raises(gen.GenerateError):
        update(repo, settings_for(FakeRunner(respond=broken)), now=NOW, log=lambda _m: None)
    assert snapshot(repo) == before
    old = json.loads(before["index.json"])["indexed_commit"]
    assert json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"] == old


# --------------------------------------------------------------------------- transactional publish (round 2)


def published_twice(repo: Path) -> tuple[dict[str, str], Settings]:
    """A first wiki on disk, and settings whose next run changes every page and drops one."""
    run(repo, FakeRunner())
    stale = repo / "docs/wiki/9-old.md"
    stale.write_text(f"# Old\n\n{GENERATED_MARK}\n")
    candidate = ownership.review(stale.parent, stale.name)
    ownership.adopt(stale.parent, stale.name, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    index["pages"].append({"file": "9-old.md", "title": "Old", "parent": None})
    (repo / "docs/wiki/index.json").write_text(json.dumps(index))
    write(repo, "src/widgetry/extra.py", "x = 1\n")
    commit(repo, "more code")
    return snapshot(repo), settings_for(FakeRunner())


def failing_replace(monkeypatch: pytest.MonkeyPatch, fail_at: int) -> list[int]:
    """Make the `fail_at`-th rename of the publish transaction fail: into the wiki directory or into its
    transaction state in Git administration (R9 moved the record and staging there). Checkpoint
    writes are not counted."""
    calls = [0]
    real = safety.os.replace

    def replace(src: object, dst: object) -> None:
        if "docs/wiki" in str(dst) or safety.TXN_ROOT in str(dst):
            calls[0] += 1
            if calls[0] == fail_at:
                raise OSError("disk full")
        real(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(safety.os, "replace", replace)
    return calls


@pytest.mark.parametrize("fail_at", [1, 2, 3, 5, 8, 11])
def test_a_failure_mid_publish_rolls_everything_back(repo: Path, monkeypatch: pytest.MonkeyPatch, fail_at: int) -> None:
    """The reviewer's case: a failure on the second write must not leave a new page next to an old index."""
    before, settings = published_twice(repo)
    failing_replace(monkeypatch, fail_at)
    with pytest.raises(OSError, match="disk full"):
        gen.generate(repo, settings, now=NOW, log=lambda _m: None)
    assert snapshot(repo) == before  # pages, the dropped page and index.json all as they were
    assert not list((repo / "docs/wiki").glob(".repowiki-*"))
    assert not list(safety.transaction_dir(repo / "docs/wiki").glob("*"))  # no record or staging left


def test_an_interrupted_publish_is_rolled_back_on_the_next_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before, settings = published_twice(repo)
    # R9: the 6th transaction rename (was the 4th while the journal lived in the checkout) fails after a
    # changed page has moved in, so the half-published checkout differs from `before` by page content.
    failing_replace(monkeypatch, 6)
    monkeypatch.setattr(safety, "_rollback", lambda wiki, journal: None)  # the process "dies" mid-publish
    with pytest.raises(OSError):
        gen.generate(repo, settings, now=NOW, log=lambda _m: None)
    # R9: the pending record lives in Git administration, not in the checkout.
    assert snapshot(repo) != before and safety.transaction_record(repo / "docs/wiki").exists()
    assert not (repo / "docs/wiki" / safety.JOURNAL).exists()
    monkeypatch.undo()
    probs = validate(repo)
    assert any("rolled back" in p.message for p in probs)
    assert snapshot(repo) == before and not list((repo / "docs/wiki").glob(".repowiki-*"))
    assert not list(safety.transaction_dir(repo / "docs/wiki").glob("*"))


def test_a_committed_publish_is_kept_by_recovery(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, settings = published_twice(repo)
    monkeypatch.setattr(safety, "_cleanup", lambda wiki, txn: None)  # "dies" after the commit point
    gen.generate(repo, settings, now=NOW, log=lambda _m: None)
    after = {k: v for k, v in snapshot(repo).items() if not k.startswith(".repowiki")}
    monkeypatch.undo()
    assert safety.recover(repo / "docs/wiki")
    assert snapshot(repo) == after and "9-old.md" not in after


# R8: real OS contention and interruption; every child imports this checkout.


@contextmanager
def r8_child(repo: Path, body: str):
    env = dict(os.environ, PYTHONPATH=str(Path(gen.__file__).resolve().parents[1]))
    script = (
        "from pathlib import Path\nfrom repowiki import safety\nimport sys\nwiki = Path(sys.argv[1]) / 'docs/wiki'\n"
        + body
    )
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(repo)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        import selectors

        with selectors.DefaultSelector() as ready:
            ready.register(child.stdout, selectors.EVENT_READ)
            assert ready.select(10), "child did not reach synchronization point"
        assert child.stdout.readline().strip() == "ready"
        yield child
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()


def test_r8_busy_lock_zero_mutation(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    body = "with safety.wiki_lock(wiki):\n    print('ready', flush=True)\n    sys.stdin.readline()\n"
    with r8_child(repo, body):
        with pytest.raises(safety.LockBusyError):
            safety.transact(repo / "docs/wiki", {"new.md": "new"}, [])
        with pytest.raises(safety.LockBusyError):
            safety.recover(repo / "docs/wiki")
        assert snapshot(repo) == before


def test_r8_validator_cannot_rollback_live_writer_and_kill_recovers(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    body = """real = safety.os.replace
def replace(src, dst):
    real(src, dst)
    if Path(src).name == 'new-1-overview.md':
        print('ready', flush=True)
        sys.stdin.readline()
safety.os.replace = replace
safety.transact(wiki, {'1-overview.md': 'interrupted', 'index.json': '{}'}, [])
"""
    with r8_child(repo, body) as child:
        during = snapshot(repo)
        assert during != before
        with pytest.raises(safety.LockBusyError):
            validate(repo)
        assert snapshot(repo) == during
        child.kill()
        child.wait(timeout=10)
    assert safety.recover(repo / "docs/wiki")
    assert snapshot(repo) == before
    assert not safety.recover(repo / "docs/wiki")  # recovery replay


def test_r8_killed_prepublication_writer_releases_lock(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    with r8_child(
        repo, "with safety.wiki_lock(wiki):\n    print('ready', flush=True)\n    sys.stdin.readline()\n"
    ) as child:
        child.kill()
        child.wait(timeout=10)
    assert not safety.recover(repo / "docs/wiki")
    assert snapshot(repo) == before


def test_r8_two_generators_sharing_base_second_conflicts(repo: Path) -> None:
    run(repo, FakeRunner())
    winner = {}

    def respond(prompt: str) -> str:
        if not winner:
            run(repo, FakeRunner())
            page = repo / "docs/wiki/1-overview.md"
            # A distinct valid winning publication even when timestamps match.
            safety.transact(page.parent, {page.name: page.read_text() + "\nWinning text.\n"}, [])
            winner.update(snapshot(repo))
        return answer(prompt)

    with pytest.raises(safety.StaleBaseError):
        run(repo, FakeRunner(respond=respond))
    assert snapshot(repo) == winner


def test_r8_repeated_site_reads_are_complete(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from repowiki import site

    run(repo, FakeRunner())
    original = site._read
    attempts = []

    def read(entry, rel):
        result = original(entry, rel)
        if rel == "index.json":
            script = """try:
    safety.transact(wiki, {'1-overview.md': 'mixed'}, [])
except safety.LockBusyError:
    print('ready', flush=True)
else:
    print('unlocked', flush=True)
"""
            with r8_child(repo, script):
                attempts.append(rel)
        return result

    monkeypatch.setattr(site, "_read", read)
    for _ in range(3):
        collected = site.collect(site.RepoEntry(name="Synthetic", path=repo))
        assert collected.pages
        assert all(p.source != "mixed" for p in collected.pages)
    assert len(attempts) == 3


def test_r8_worktree_aliases_share_lock(repo: Path) -> None:
    alias = repo.parent / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    body = "with safety.wiki_lock(wiki):\n    print('ready', flush=True)\n    sys.stdin.readline()\n"
    with r8_child(repo, body):
        with pytest.raises(safety.LockBusyError):
            with safety.wiki_lock(alias / "docs/./wiki"):
                pass


def test_r8_base_digest_never_reads_wiki_files(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run(repo, FakeRunner())
    wiki = repo / "docs/wiki"
    forbidden = [wiki / name for name in ("credentials.md", ".env.production.md", "agent-transcript.md")]
    for path in forbidden:
        path.write_text("SYNTHETIC_FORBIDDEN_CANARY\n")
    opened: list[str] = []
    real_open = os.open

    def spy_open(path, *args, **kwargs):
        opened.append(Path(path).name)
        return real_open(path, *args, **kwargs)

    for path in forbidden:
        path.chmod(0o000)  # any read attempt fails with PermissionError
    try:
        with monkeypatch.context() as patched:
            patched.setattr(safety.os, "open", spy_open)
            before = safety.base_digest(wiki)
            assert safety.base_digest(wiki) == before
        assert opened and not {p.name for p in forbidden} & set(opened)
        run(repo, FakeRunner())  # generate digests and checks the base without reading them
        unchanged = safety.base_digest(wiki)
        assert safety.base_digest(wiki) == unchanged  # stable while nothing changes
        forbidden[0].unlink()
        assert safety.base_digest(wiki) != unchanged  # removing one unread file alone changes it
    finally:
        for path in forbidden:
            if path.exists():
                path.chmod(0o600)


# R9: authoritative transaction state lives in Git administration; checkout journals never authorize mutation.


def r9_wiki(repo: Path) -> Path:
    run(repo, FakeRunner())
    wiki = repo / "docs/wiki"
    (wiki / "notes.md").write_text("# Notes\n\nHand-written, never generated.\n")
    # R10: transaction deletion fixtures must explicitly review/adopt the handwritten page.
    candidate = ownership.review(wiki, "notes.md")
    ownership.adopt(wiki, candidate.page, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    return wiki


def r9_record(wiki: Path, entries: list[dict[str, object]], state: str = "prepared", **extra: object) -> Path:
    txn = safety.transaction_dir(wiki)
    txn.mkdir(parents=True, exist_ok=True)
    (txn / "stage").mkdir(exist_ok=True)
    record = {"version": 1, "wiki": str(wiki.resolve()), "state": state, "entries": entries, **extra}
    path = safety.transaction_record(wiki)
    path.write_text(json.dumps(record))
    return path


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_r9_forged_checkout_journal_preserves_notes(repo: Path) -> None:
    wiki = r9_wiki(repo)
    (wiki / ".repowiki-txn-forged").mkdir()
    forged = {"txn": ".repowiki-txn-forged", "existed": {"notes.md": False, "1-overview.md": False}, "committed": False}
    (wiki / safety.JOURNAL).write_text(json.dumps(forged))
    before = snapshot(repo)
    assert not safety.recover(wiki)
    assert snapshot(repo) == before and (wiki / "notes.md").exists()
    assert not any("rolled back" in p.message for p in validate(repo))
    assert snapshot(repo) == before


def test_r9_forged_absent_target_record_preserves_notes(repo: Path) -> None:
    wiki = r9_wiki(repo)
    record = r9_record(wiki, [{"name": "notes.md", "existed": False, "old": None, "new": sha("something else")}])
    before = snapshot(repo)
    with pytest.raises(safety.RecoveryError, match="not the file this transaction added"):
        safety.recover(wiki)
    assert snapshot(repo) == before and record.exists()  # authority is kept for inspection, not discarded


@pytest.mark.parametrize("record", [
    "not json",
    json.dumps({"version": 2, "wiki": "W", "state": "prepared", "entries": []}),
    json.dumps({"version": 1, "wiki": "W", "state": "maybe", "entries": []}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared", "entries": [], "extra": 1}),
    json.dumps({"version": 1, "wiki": "/elsewhere/wiki", "state": "prepared", "entries": []}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared",
                "entries": [{"name": "../../README.md", "existed": True, "old": "0" * 64, "new": None}]}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared",
                "entries": [{"name": "notes.md", "existed": True, "old": "nothex", "new": None}]}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared",
                "entries": [{"name": "notes.md", "existed": "yes", "old": None, "new": None}]}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared",
                "entries": [{"name": "notes.md", "existed": False, "old": 5, "new": None}]}),
    json.dumps({"version": 1, "wiki": "W", "state": "committed",
                "entries": [{"name": "notes.md", "existed": True, "old": "0" * 64, "new": 7}]}),
    json.dumps({"version": 1, "wiki": "W", "state": "prepared", "entries": [
        {"name": "notes.md", "existed": False, "old": None, "new": None},
        {"name": "NOTES.md", "existed": False, "old": None, "new": None}]}),
])  # fmt: skip
def test_r9_malformed_evidence_fails_closed(repo: Path, record: str) -> None:
    wiki = r9_wiki(repo)
    path = r9_record(wiki, [])
    path.write_text(record.replace('"W"', json.dumps(str(wiki.resolve()))))
    before = snapshot(repo)
    for _ in range(2):  # never discarded: every attempt refuses again
        with pytest.raises(safety.RecoveryError, match="untrusted transaction record"):
            safety.recover(wiki)
    assert snapshot(repo) == before and path.exists()
    assert any("untrusted transaction record" in p.message for p in validate(repo))


def test_r9_transaction_and_target_symlinks_refuse(repo: Path, tmp_path: Path) -> None:
    wiki = r9_wiki(repo)
    before = snapshot(repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    txn = safety.transaction_dir(wiki)
    if txn.exists():
        shutil.rmtree(txn)  # the earlier publish left an empty transaction directory
    txn.parent.mkdir(parents=True, exist_ok=True)
    txn.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(safety.RecoveryError, match="not a plain directory"):
        safety.transact(wiki, {"1-overview.md": "new"}, [])
    assert snapshot(repo) == before and list(elsewhere.iterdir()) == []
    txn.unlink()

    # a prepared record whose target became a symlink: nothing is touched
    victim = tmp_path / "victim.md"
    victim.write_text("victim\n")
    text = (wiki / "1-overview.md").read_text()
    r9_record(wiki, [{"name": "1-overview.md", "existed": True, "old": sha(text), "new": sha("new")}])
    (safety.transaction_dir(wiki) / "stage" / "old-1-overview.md").write_text(text)
    (wiki / "1-overview.md").unlink()
    (wiki / "1-overview.md").symlink_to(victim)
    with pytest.raises(safety.RecoveryError, match="not a regular file"):
        safety.recover(wiki)
    assert (wiki / "1-overview.md").is_symlink() and victim.read_text() == "victim\n"

    # a staging directory that is a symlink
    (wiki / "1-overview.md").unlink()
    (wiki / "1-overview.md").write_text(text)
    stage = safety.transaction_dir(wiki) / "stage"
    shutil.rmtree(stage)
    stage.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(safety.RecoveryError, match="not a plain directory"):
        safety.recover(wiki)
    assert list(elsewhere.iterdir()) == []


def test_r9_conflicting_duplicate_targets_refuse(repo: Path) -> None:
    wiki = r9_wiki(repo)
    before = snapshot(repo)
    cases = (({"5-a.md": "x"}, ["5-a.md"]), ({"5-A.md": "x", "5-a.md": "y"}, []), ({}, ["notes.md", "notes.md"]))
    for writes, deletes in cases:
        with pytest.raises(safety.UnsafePathError, match="conflicting duplicate targets"):
            safety.transact(wiki, writes, deletes)
    assert snapshot(repo) == before
    assert not list(safety.transaction_dir(wiki).glob("*"))


def test_r9_durability_order(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = r9_wiki(repo)
    txn_root = safety.transaction_dir(wiki).parent
    shutil.rmtree(txn_root)  # the transaction directories are created (and made durable) again
    events: list[str] = []
    real_fsync = safety.os.fsync
    monkeypatch.setattr(safety.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(safety, "_fsync_dir", lambda p: events.append(f"dir:{Path(p).name}"))  # order only
    monkeypatch.setattr(safety, "_step", lambda name: events.append(name))
    safety.transact(wiki, {"1-overview.md": "new\n", "5-extra.md": "extra\n"}, ["notes.md"])
    staged = events.index("staged")
    assert events[:staged].count("fsync") >= 2 and "dir:stage" in events[:staged]  # each new file, then staging
    admin = txn_root.parent.name
    created = [e for e in events[:staged] if e.startswith("dir:")]
    assert created[:3] == [f"dir:{admin}", f"dir:{txn_root.name}", f"dir:{safety.transaction_dir(wiki).name}"]
    for n, event in enumerate(events):
        if event.startswith("moved-old:"):  # the original is durable in staging before its replacement moves in
            parent = ownership.ROOT if event == "moved-old:@ownership" else "wiki"
            assert events[n - 2 : n] == ["dir:stage", f"dir:{parent}"]
    prepared = events.index("prepared")
    assert events[staged + 1 : prepared] == ["fsync", "dir:" + safety.transaction_dir(wiki).name]  # the record
    synced, committed = events.index("synced"), events.index("committed")
    assert "dir:wiki" in events[prepared:synced] and "dir:stage" in events[prepared:synced]
    assert events[synced + 1 : committed] == ["fsync", "dir:" + safety.transaction_dir(wiki).name]  # commit point


R9_WRITES = {"1-overview.md": "# Overview\n\nnew\n", "5-extra.md": "# Extra\n\nnew\n", "index.json": "{}\n"}
R9_DELETES = ["notes.md"]
R9_STEPS = [
    "staged",
    "prepared",
    "moved-old:1-overview.md",
    "moved-new:1-overview.md",
    "moved-new:5-extra.md",
    "moved-old:index.json",
    "moved-new:index.json",
    "moved-old:notes.md",
    "moved-old:@ownership",
    "moved-new:@ownership",
    "synced",
    "committed",
    "cleanup:staging",
    "cleaned",
]


@pytest.mark.parametrize("step", R9_STEPS)
def test_r9_kill_at_every_transition_recovers_old_or_new(repo: Path, step: str) -> None:
    wiki = r9_wiki(repo)
    before = snapshot(repo)
    before_ownership = ownership.load(wiki)
    env = dict(os.environ, PYTHONPATH=str(Path(gen.__file__).resolve().parents[1]))
    script = f"""import os, sys
from pathlib import Path
from repowiki import safety
safety._step = lambda name: os.kill(os.getpid(), 9) if name == {step!r} else None
safety.transact(Path(sys.argv[1]), {R9_WRITES!r}, {R9_DELETES!r})
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(wiki)], env=env, capture_output=True, text=True, timeout=60
    )
    assert child.returncode == -9, child.stderr
    after = {k: v for k, v in before.items() if k not in R9_DELETES} | R9_WRITES
    committed = R9_STEPS.index(step) >= R9_STEPS.index("committed")
    found = safety.recover(wiki)
    assert found == (step != "cleaned")
    assert snapshot(repo) == (after if committed else before)
    after_ownership = {k: v for k, v in before_ownership.items() if k not in R9_DELETES}
    after_ownership.update({k: sha(v) for k, v in R9_WRITES.items() if k != safety.INDEX})
    assert ownership.load(wiki) == (after_ownership if committed else before_ownership)
    settled = snapshot(repo)
    assert not safety.recover(wiki) and snapshot(repo) == settled  # repeated recovery: zero further changes
    assert not list(safety.transaction_dir(wiki).glob("*"))


def r9_kill(wiki: Path, body: str, step: str) -> None:
    """Run `body` in a new process that is SIGKILLed at transaction transition `step`."""
    env = dict(os.environ, PYTHONPATH=str(Path(gen.__file__).resolve().parents[1]))
    script = f"""import os, sys
from pathlib import Path
from repowiki import safety
wiki = Path(sys.argv[1])
safety._step = lambda name: os.kill(os.getpid(), 9) if name.startswith({step!r}) else None
{body}
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(wiki)], env=env, capture_output=True, text=True, timeout=60
    )
    assert child.returncode == -9, child.stderr


def test_r9_interrupt_after_commit_finishes_and_never_rolls_back(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = r9_wiki(repo)
    after = {k: v for k, v in snapshot(repo).items() if k not in R9_DELETES} | R9_WRITES

    def step(name: str) -> None:
        if name == "committed":
            raise KeyboardInterrupt  # Ctrl-C once the committed record is installed

    monkeypatch.setattr(safety, "_step", step)
    with pytest.raises(KeyboardInterrupt):
        safety.transact(wiki, R9_WRITES, R9_DELETES)
    assert snapshot(repo) == after and not list(safety.transaction_dir(wiki).glob("*"))


def test_r9_recovery_killed_mid_rollback_resumes(repo: Path) -> None:
    wiki = r9_wiki(repo)
    before = snapshot(repo)
    r9_kill(wiki, f"safety.transact(wiki, {R9_WRITES!r}, {R9_DELETES!r})", "moved-new:index.json")
    r9_kill(wiki, "safety.recover(wiki)", "rolled-back:")  # recovery itself is killed after one restore
    assert safety.transaction_record(wiki).exists()
    assert safety.recover(wiki)
    assert snapshot(repo) == before
    assert not safety.recover(wiki) and snapshot(repo) == before


def test_r9_committed_recovery_checks_evidence(repo: Path) -> None:
    wiki = r9_wiki(repo)
    r9_kill(wiki, f"safety.transact(wiki, {R9_WRITES!r}, {R9_DELETES!r})", "committed")
    (wiki / "1-overview.md").write_text("")  # a careless process truncates a published page
    kept = safety.transaction_dir(wiki) / "stage" / "old-1-overview.md"
    original = kept.read_bytes()
    with pytest.raises(safety.RecoveryError, match="committed transaction evidence does not match"):
        safety.recover(wiki)
    assert kept.read_bytes() == original and safety.transaction_record(wiki).exists()  # nothing discarded


def test_r9_both_records_fit_the_recovery_limit(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A prepared record at the limit would make the committed one (a byte longer) unloadable: refuse first."""
    wiki = r9_wiki(repo)
    before = snapshot(repo)
    receipt = ownership.record_path(wiki).read_bytes()
    updated = ownership.load(wiki) | {"5-a.md": sha("x")}
    journal = {
        "version": 2,
        "wiki": str(wiki.resolve()),
        "state": "prepared",
        "entries": [
            {"name": "5-a.md", "existed": False, "old": None, "new": sha("x")},
            {
                "name": ownership.TARGET,
                "existed": True,
                "old": hashlib.sha256(receipt).hexdigest(),
                "new": hashlib.sha256(ownership.encode(wiki, updated)).hexdigest(),
            },
        ],
        "reviewed": {},
    }
    size = len(json.dumps(journal).encode())
    monkeypatch.setattr(safety, "MAX_RECORD_BYTES", size)  # the prepared record fits exactly
    with pytest.raises(safety.UnsafePathError, match="too many files"):
        safety.transact(wiki, {"5-a.md": "x"}, [])
    assert snapshot(repo) == before and not list(safety.transaction_dir(wiki).glob("*"))
    assert ownership.record_path(wiki).read_bytes() == receipt


def test_r9_unreadable_record_after_an_error_is_left_for_recovery(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = r9_wiki(repo)

    def step(name: str) -> None:
        if name == "committed":
            safety.transaction_record(wiki).write_text("garbled")  # the record cannot be read back
            raise KeyboardInterrupt

    monkeypatch.setattr(safety, "_step", step)
    with pytest.raises(KeyboardInterrupt):
        safety.transact(wiki, R9_WRITES, R9_DELETES)
    during = snapshot(repo)
    assert (safety.transaction_dir(wiki) / "stage").exists()  # no rollback, no cleanup: evidence kept
    monkeypatch.undo()
    with pytest.raises(safety.RecoveryError, match="untrusted transaction record"):
        safety.recover(wiki)
    assert snapshot(repo) == during


@pytest.mark.parametrize("marked", [False, True])
def test_r10_generate_refuses_a_collision_and_preserves_the_whole_wiki(repo: Path, marked: bool) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    original = "# Handwritten\n\nSynthetic manual prose.\n"
    if marked:
        original += GENERATED_MARK + "\n"
    (wiki / "1-overview.md").write_text(original)
    before = snapshot(repo)
    with pytest.raises(ownership.OwnershipError):
        run(repo, FakeRunner())
    assert snapshot(repo) == before and not ownership.record_path(wiki).exists()


def test_r10_marked_manual_replacement_refuses_without_changing_the_index(repo: Path) -> None:
    run(repo, FakeRunner())
    wiki = repo / "docs/wiki"
    page = wiki / "1-overview.md"
    page.write_bytes(page.read_bytes() + b" ")
    before = snapshot(repo)
    authority = ownership.record_path(wiki).read_bytes()
    with pytest.raises(ownership.OwnershipError):
        run(repo, FakeRunner())
    assert snapshot(repo) == before and ownership.record_path(wiki).read_bytes() == authority


@pytest.mark.parametrize("edited", [False, True])
def test_r10_dropped_marked_page_is_kept_unless_exactly_owned(repo: Path, edited: bool) -> None:
    run(repo, FakeRunner())
    wiki = repo / "docs/wiki"
    text = f"# Old\n\n{GENERATED_MARK}\n"
    if edited:
        safety.transact(wiki, {"old.md": text}, [])
        text += " "
    (wiki / "old.md").write_text(text)
    result = run(repo, FakeRunner())  # explicitly supply the dropped index entry below via save
    index = json.loads((wiki / "index.json").read_bytes())
    warnings = gen.save(wiki, {}, index, {"old.md"})
    assert result.problems == []
    assert (wiki / "old.md").read_text() == text
    assert any("old.md" in warning and "no exact local ownership" in warning for warning in warnings)


def test_r10_cli_adoption_requires_explicit_digests_and_calls_no_runner(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from repowiki.cli import main

    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    page = wiki / "manual.md"
    page.write_bytes(b"# Manual\r\n")
    monkeypatch.setattr(Settings, "runners", lambda _: pytest.fail("offline adoption must not select a runner"))
    command = ["adopt", page.name, "--repo", str(repo)]
    assert main(command) == 0
    assert "sha256:" in capsys.readouterr().out
    assert not ownership.record_path(wiki).exists()
    candidate = ownership.review(wiki, page.name)
    assert main(command + ["--reviewed-sha256", candidate.sha256]) == 2
    assert "requires both" in capsys.readouterr().err
    assert main(command + ["--reviewed-sha256", candidate.sha256, "--expect-base", candidate.base]) == 0
    assert ownership.load(wiki) == {page.name: candidate.sha256}
    assert page.read_bytes() == b"# Manual\r\n"


def test_r13_a_failed_final_pass_leaves_the_last_good_wiki_untouched(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    write(repo, "src/widgetry/extra.py", "x = 1\n")
    commit(repo, "more code")

    def final(prompt: str) -> str:
        if 'page "Testing"' in prompt:
            raise RunnerError("quota exhausted", transient=False)
        return answer(prompt.replace("TASK: revise", "TASK: page"))

    cheap, strong = FakeRunner("cheap"), FakeRunner("strong", respond=final)
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (cheap, strong)  # type: ignore[method-assign]
    with pytest.raises(gen.GenerateError, match="required final pass failed"):
        gen.generate(repo, settings, now=NOW, log=lambda _m: None)
    assert snapshot(repo) == before


def test_r13_a_repair_that_only_unlinks_never_replaces_the_last_good_wiki(repo: Path) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    write(repo, "src/widgetry/extra.py", "x = 1\n")
    commit(repo, "more code")
    cli = "[cli](src/widgetry/cli.py#L1-L3)"

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        if prompt.startswith("TASK: repair"):
            return f"# P\n\nThe CLI starts here {cli}.\n\nThe store keeps rows ghost.\n"
        return f"# P\n\nThe CLI starts here {cli}.\n\nThe store keeps rows [ghost](src/ghost.py#L1).\n"

    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, FakeRunner(respond=respond))
    assert snapshot(repo) == before
