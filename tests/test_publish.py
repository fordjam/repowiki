"""Blocker (e): only a wiki that passes validation is ever written, and failures leave the old one intact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import generate as gen
from repowiki import safety
from repowiki.config import Settings
from repowiki.fake import answer
from repowiki.links import find_links, unlink
from repowiki.runners import FakeRunner, RunnerError
from repowiki.safety import GENERATED_MARK
from repowiki.update import update
from repowiki.validate import validate


def settings_for(fake: FakeRunner) -> Settings:
    s = Settings(cheap="fake", min_pages=2)
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return s


def run(repo: Path, fake: FakeRunner) -> gen.Result:
    return gen.generate(repo, settings_for(fake), log=lambda _m: None)


def snapshot(repo: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((repo / "docs/wiki").iterdir()) if p.is_file()}


def test_reference_style_bad_links_are_removed(repo: Path) -> None:
    """The reviewer's scenario: the backend keeps returning a reference-style bad citation."""

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return (
            "# P\n\nGood [cli](src/widgetry/cli.py#L1-L3). Also [bad][ref], [ref][], [ref] and ![pic][ref].\n\n"
            '[ref]: ../../missing.py#L1 "a title"\n'
        )

    fake = FakeRunner(respond=respond)
    result = run(repo, fake)
    assert any(c.startswith("TASK: repair") for c in fake.calls)
    assert result.problems == [] and validate(repo) == []
    text = (repo / "docs/wiki/1-overview.md").read_text()
    assert "missing.py" not in text and "[ref]" not in text
    assert "Also bad, ref, ref and pic." in text
    assert "[cli](../../src/widgetry/cli.py#L1-L3)" in text


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


def test_a_page_that_stays_invalid_is_never_written(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run(repo, FakeRunner())
    before = snapshot(repo)
    monkeypatch.setattr(gen, "unlink", lambda text, bad: text)  # pretend link removal cannot help

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
        update(repo, settings_for(FakeRunner(respond=broken)), log=lambda _m: None)
    assert snapshot(repo) == before
    old = json.loads(before["index.json"])["indexed_commit"]
    assert json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"] == old


# --------------------------------------------------------------------------- transactional publish (round 2)


def published_twice(repo: Path) -> tuple[dict[str, str], Settings]:
    """A first wiki on disk, and settings whose next run changes every page and drops one."""
    run(repo, FakeRunner())
    stale = repo / "docs/wiki/9-old.md"
    stale.write_text(f"# Old\n\n{GENERATED_MARK}\n")
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    index["pages"].append({"file": "9-old.md", "title": "Old", "parent": None})
    (repo / "docs/wiki/index.json").write_text(json.dumps(index))
    write(repo, "src/widgetry/extra.py", "x = 1\n")
    commit(repo, "more code")
    return snapshot(repo), settings_for(FakeRunner())


def failing_replace(monkeypatch: pytest.MonkeyPatch, fail_at: int) -> list[int]:
    """Make the `fail_at`-th rename inside the wiki directory fail (checkpoint writes are not counted)."""
    calls = [0]
    real = safety.os.replace

    def replace(src: object, dst: object) -> None:
        if "docs/wiki" in str(dst):
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
        gen.generate(repo, settings, log=lambda _m: None)
    assert snapshot(repo) == before  # pages, the dropped page and index.json all as they were
    assert not list((repo / "docs/wiki").glob(".repowiki-*"))


def test_an_interrupted_publish_is_rolled_back_on_the_next_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before, settings = published_twice(repo)
    failing_replace(monkeypatch, 4)
    monkeypatch.setattr(safety, "_rollback", lambda wiki, journal: None)  # the process "dies" mid-publish
    with pytest.raises(OSError):
        gen.generate(repo, settings, log=lambda _m: None)
    assert snapshot(repo) != before and (repo / "docs/wiki" / safety.JOURNAL).exists()
    monkeypatch.undo()
    probs = validate(repo)
    assert any("rolled back" in p.message for p in probs)
    assert snapshot(repo) == before and not list((repo / "docs/wiki").glob(".repowiki-*"))


def test_a_committed_publish_is_kept_by_recovery(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, settings = published_twice(repo)
    monkeypatch.setattr(safety, "_cleanup", lambda wiki, txn: None)  # "dies" after the commit point
    gen.generate(repo, settings, log=lambda _m: None)
    after = {k: v for k, v in snapshot(repo).items() if not k.startswith(".repowiki")}
    monkeypatch.undo()
    assert safety.recover(repo / "docs/wiki")
    assert snapshot(repo) == after and "9-old.md" not in after


# R8: real OS contention and interruption; every child imports this checkout.
import os
import subprocess
import sys
from contextlib import contextmanager


@contextmanager
def r8_child(repo: Path, body: str):
    env = dict(os.environ, PYTHONPATH=str(Path(gen.__file__).resolve().parents[1]))
    script = "from pathlib import Path\nfrom repowiki import safety\nimport sys\nwiki = Path(sys.argv[1]) / 'docs/wiki'\n" + body
    child = subprocess.Popen([sys.executable, "-u", "-c", script, str(repo)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, env=env)
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
    with r8_child(repo, "with safety.wiki_lock(wiki):\n    print('ready', flush=True)\n    sys.stdin.readline()\n") as child:
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
