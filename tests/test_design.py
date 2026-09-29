"""Design fixes: checkpoints and retries, citation minimums, renames, byte budgets, safe site swaps."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit, run_git, write
from repowiki import generate as gen
from repowiki.config import Settings
from repowiki.fake import answer
from repowiki.git import Snapshot
from repowiki.runners import FakeRunner, RunnerError
from repowiki.site import RepoEntry, build_site
from repowiki.survey import excerpts, tree_listing
from repowiki.update import plan_update, update


def settings_for(cheap: FakeRunner, strong: FakeRunner | None = None, **kw: object) -> Settings:
    s = Settings(**{"cheap": "fake", "min_pages": 2, "backoff": 0, **kw})  # type: ignore[arg-type]
    s.runners = lambda: (cheap, strong or cheap)  # type: ignore[method-assign]
    return s


def run(repo: Path, cheap: FakeRunner, **kw: object) -> gen.Result:
    return gen.generate(repo, settings_for(cheap, **kw), log=lambda _m: None)


# --------------------------------------------------------------------------- checkpoints and retries


def test_a_failed_run_resumes_from_checkpoints(repo: Path) -> None:
    state = {"broken": True}

    def respond(prompt: str) -> str:
        if state["broken"] and "Title: Testing" in prompt:
            raise RunnerError("rate limited", transient=False)
        return answer(prompt)

    fake = FakeRunner(respond=respond)
    with pytest.raises(gen.GenerateError, match="checkpointed, rerun to resume"):
        run(repo, fake)
    assert not (repo / "docs/wiki").exists()
    pages_before = sum(c.startswith("TASK: page") for c in fake.calls)

    state["broken"] = False
    fake.calls.clear()
    result = run(repo, fake)
    assert result.problems == []
    assert not any(c.startswith("TASK: plan") for c in fake.calls)  # the outline was checkpointed
    assert [c for c in fake.calls if c.startswith("TASK: page")] == [
        c for c in fake.calls if "Title: Testing" in c
    ]  # only the failed page was written again
    assert pages_before == len(result.pages)


def test_checkpoints_can_be_ignored_or_turned_off(repo: Path) -> None:
    fake = FakeRunner()
    run(repo, fake)
    fake.calls.clear()
    run(repo, fake, resume=False)
    assert any(c.startswith("TASK: plan") for c in fake.calls)
    fake.calls.clear()
    run(repo, fake, checkpoints=False)
    assert any(c.startswith("TASK: plan") for c in fake.calls)


def test_transient_failures_are_retried_with_backoff(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(gen, "_sleep", sleeps.append)
    failures = {"left": 2}

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: page") and failures["left"]:
            failures["left"] -= 1
            raise RunnerError("exit 1: overloaded")
        return answer(prompt)

    result = gen.generate(repo, settings_for(FakeRunner(respond=respond), backoff=3), log=lambda _m: None)
    assert result.problems == [] and sleeps == [3, 6]


def test_retries_are_bounded_and_skip_permanent_errors(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen, "_sleep", lambda _s: None)
    attempts: list[str] = []

    def always(prompt: str) -> str:
        attempts.append(prompt)
        raise RunnerError("timed out")

    with pytest.raises(gen.GenerateError):
        run(repo, FakeRunner(respond=always), retries=2)
    assert len(attempts) == 3  # the plan call: 1 try + 2 retries, then give up
    attempts.clear()

    def permanent(prompt: str) -> str:
        attempts.append(prompt)
        raise RunnerError("not signed in", transient=False)

    with pytest.raises(gen.GenerateError):
        run(repo, FakeRunner(respond=permanent), retries=5)
    assert len(attempts) == 1


# --------------------------------------------------------------------------- evidence


def test_uncited_pages_fail_generation_unless_allowed(repo: Path) -> None:
    def uncited(prompt: str) -> str:
        return answer(prompt) if prompt.startswith("TASK: plan") else "# P\n\nConfident claims, no evidence.\n"

    with pytest.raises(gen.GenerateError, match="line citation"):
        run(repo, FakeRunner(respond=uncited))
    assert not (repo / "docs/wiki").exists()
    assert run(repo, FakeRunner(respond=uncited), min_citations=0).problems == []


def test_sources_block_lists_only_files_the_model_saw(repo: Path) -> None:
    write(repo, "src/widgetry/big.py", "x = 1\n" * 30_000)
    commit(repo, "big")
    plan = {"pages": [
        {"title": "Overview", "sources": ["src/widgetry/cli.py", "src/widgetry/big.py", "src/widgetry/engine.py"]},
        {"title": "Store", "sources": ["src/widgetry/store/db.py"]},
    ]}  # fmt: skip

    def respond(prompt: str) -> str:
        return json.dumps(plan) if prompt.startswith("TASK: plan") else answer(prompt)

    run(repo, FakeRunner(respond=respond), context_chars=2_000)
    text = (repo / "docs/wiki/1-overview.md").read_text()
    block = text.split("</details>")[0]
    assert "src/widgetry/cli.py" in block and "src/widgetry/engine.py" not in block


# --------------------------------------------------------------------------- update


def test_update_follows_a_renamed_file(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    commit(repo, "wiki")
    run_git(repo, "mv", "src/widgetry/store/db.py", "src/widgetry/store/database.py")
    commit(repo, "rename")
    up, _, _ = plan_update(repo, settings_for(fake))
    assert up.renamed == {"src/widgetry/store/db.py": "src/widgetry/store/database.py"}
    stale = [p for p, hits in up.stale.items() if "src/widgetry/store/db.py" in hits]
    assert stale
    fake.calls.clear()
    result = update(repo, settings_for(fake), log=lambda _m: None)
    assert result.problems == []
    prompt = next(c for c in fake.calls if "store/database.py" in c)
    assert "=== src/widgetry/store/database.py" in prompt and "renamed to src/widgetry/store/database.py" in prompt
    for page in stale:
        assert "store/database.py#L" in (repo / "docs/wiki" / page).read_text()


def test_directory_citations_make_pages_stale(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text().replace("\n<!--", "\nSee [the store](../../src/widgetry/store).\n\n<!--"))
    commit(repo, "wiki")
    write(repo, "src/widgetry/store/cache.py", "CACHE = {}\n")
    commit(repo, "add cache")
    up, _, _ = plan_update(repo, settings_for(fake))
    assert up.stale.get("1-overview.md") == ["src/widgetry/store/cache.py"]
    fake.calls.clear()
    update(repo, settings_for(fake), log=lambda _m: None)
    assert any("=== src/widgetry/store/cache.py" in c for c in fake.calls)


# --------------------------------------------------------------------------- budgets


def test_excerpts_never_read_past_the_budget(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "huge.txt", "line\n" * 400_000)  # 2 MB
    commit(repo, "huge")
    snap = Snapshot.at(repo)
    limits: list[int | None] = []
    real = Snapshot._bytes

    def spy(self: Snapshot, path: str, limit: int | None) -> bytes | None:
        limits.append(limit)
        return real(self, path, limit)

    monkeypatch.setattr(Snapshot, "_bytes", spy)
    ex = excerpts(snap, ["huge.txt", "src/widgetry/cli.py"], 5_000)
    assert limits and all(lim is not None and lim <= 5_000 for lim in limits)
    assert ex.shown["huge.txt"][1] == 400_000 and ex.shown["huge.txt"][0] < 2_000
    assert len(ex.text) < 5_200


def test_tree_listing_respects_its_limit() -> None:
    class Sizes:
        sizes: dict[str, int] = {}

    wide = [f"d{n}/f.py" for n in range(1_000)]
    deep = [f"a/b{n % 30}/c{n}.py" for n in range(3_000)]
    for files in (wide, deep):
        listing = tree_listing(Sizes(), files, limit=600)  # type: ignore[arg-type]
        assert len(listing.splitlines()) <= 600


# --------------------------------------------------------------------------- site


def test_rebuilding_a_site_keeps_the_old_one_until_the_new_one_is_ready(repo: Path, tmp_path: Path) -> None:
    run(repo, FakeRunner())
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None)
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None)
    assert (out / "index.html").is_file()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".site")] == []
