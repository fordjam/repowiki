from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import adopt_page, commit, run_git, write
from repowiki import update as update_module
from repowiki.approval import receipt_path
from repowiki.cli import parser
from repowiki.config import Settings, load_settings
from repowiki.generate import GenerateError, StaleEnvelopeError, generate
from repowiki.ownership import record_path
from repowiki.runners import FakeRunner, RunnerError
from repowiki.safety import StaleBaseError
from repowiki.update import plan_update, update

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the run's instant is the caller's, never the clock


def settings_with(fake: FakeRunner) -> Settings:
    s = Settings(cheap="fake", min_pages=2)
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return s


def pages_citing(repo: Path, rel: str) -> set[str]:
    wiki = repo / "docs/wiki"
    return {p.name for p in wiki.glob("*.md") if f"../../{rel}" in p.read_text()}


def test_update_rewrites_only_pages_citing_changed_files(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
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
    result = update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
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
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    write(repo, "NOTES.txt", "unrelated\n")
    sha = commit(repo, "notes")
    fake.calls.clear()
    result = update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert result.written == [] and fake.calls == []
    assert json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"] == sha


def test_dry_run_writes_nothing(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    index_before = (repo / "docs/wiki/index.json").read_text()
    write(repo, "src/widgetry/cli.py", "def main():\n    return 0\n")
    commit(repo, "cli")
    result = update(repo, settings_with(fake), now=NOW, log=lambda _m: None, dry_run=True)
    assert result.plan.stale and result.written == []
    assert (repo / "docs/wiki/index.json").read_text() == index_before


def test_r3_dry_run_never_builds_or_verifies_runners(repo: Path) -> None:
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 9\n")
    commit(repo, "cli")
    settings = Settings(cheap="fake", min_pages=2)

    def refuse() -> None:
        raise AssertionError("a dry run must not build or verify runners")

    settings.runners = refuse  # type: ignore[method-assign]
    logged: list[str] = []
    result = update(repo, settings, now=NOW, log=logged.append, dry_run=True)
    assert result.plan.stale and result.written == []
    assert any(line.startswith("Preview digest: ") for line in "\n".join(logged).splitlines())


def test_r4_pages_citing_pinned_web_urls_become_stale(repo: Path) -> None:
    """Dependency extraction reads web citations into the registered identity, not just relative links."""
    from conftest import run_git

    run_git(repo, "remote", "add", "origin", "https://code.example.test/example/widgetry.git")
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
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
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    old = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    wiki = repo / "docs/wiki"
    page = sorted(wiki.glob("*.md"))[0]
    url = f"https://code.example.test/example/widgetry/blob/{old}/src/widgetry/engine.py#L30-L31"
    page.write_text(page.read_text() + f"\nSee [engine.py:30-31]({url}).\n")
    adopt_page(page)
    commit(repo, "hand-edited wiki")
    write(repo, "NOTES.txt", "unrelated\n")
    new = commit(repo, "notes")
    result = update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert result.problems == [] and result.written == []
    assert json.loads((wiki / "index.json").read_text())["indexed_commit"] == new
    text = page.read_text()
    assert url not in text and "[engine.py:30-31](../../src/widgetry/engine.py#L30-L31)" in text


def test_r4_kept_page_citation_forms_all_survive_update(repo: Path) -> None:
    from conftest import run_git

    run_git(repo, "remote", "add", "origin", "https://code.example.test/example/widgetry.git")
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    old = json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"]
    page = sorted((repo / "docs/wiki").glob("*.md"))[0]
    url = f"https://code.example.test/example/widgetry/blob/{old}/src/widgetry/engine.py#L30-L31"
    page.write_text(page.read_text() + f"\nSee [`engine.py`]({url}), <{url}> and [ref].\n\n[ref]:\n  {url}\n")
    adopt_page(page)
    commit(repo, "hand-edited wiki")
    write(repo, "NOTES.txt", "unrelated\n")
    commit(repo, "notes")
    result = update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert result.problems == [] and url not in page.read_text()


def prepared_wiki(repo: Path) -> tuple[FakeRunner, Path, str]:
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    page = next(p for p in (repo / "docs/wiki").glob("*.md") if "cli.py" in p.read_text())
    page.write_text(page.read_text() + "\nCOMMITTED_APPROVED_PROSE\n")
    adopt_page(page)
    wiki_commit = commit(repo, "approved synthetic wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 7\n")
    commit(repo, "source update")
    fake.calls.clear()
    fake.trace.clear()
    return fake, page, wiki_commit


def test_r16_refs_are_explicit_operator_inputs(tmp_path: Path) -> None:
    args = parser().parse_args(["update", "--wiki-ref", "wiki-reviewed", "--policy-ref", "policy-reviewed"])
    assert args.wiki_ref == "wiki-reviewed" and args.policy_commit == "policy-reviewed"
    settings = load_settings(tmp_path, {"wiki_ref": args.wiki_ref, "policy_commit": args.policy_commit})
    assert settings.wiki_ref == args.wiki_ref and settings.policy_commit == args.policy_commit
    write(tmp_path, ".repowiki.toml", 'wiki_ref = "repository-selected"\n')
    with pytest.raises(ValueError, match="selected by the operator"):
        load_settings(tmp_path)


@pytest.mark.parametrize("change", ["page", "index", "untracked", "staged", "ignored"])
def test_r16_dirty_destination_is_never_sent(repo: Path, change: str) -> None:
    fake, page, _ = prepared_wiki(repo)
    if change in ("page", "staged"):
        page.write_text(page.read_text() + "\nDIRTY_PREVIOUS_CANARY\n")
        if change == "staged":
            run_git(repo, "add", str(page.relative_to(repo)))
    elif change == "index":
        index = page.parent / "index.json"
        index.write_text(index.read_text() + " ")
    else:
        if change == "ignored":
            write(repo, ".gitignore", "docs/wiki/local.md\n")
            commit(repo, "ignored local wiki fixture")
        write(repo, "docs/wiki/local.md", "DIRTY_PREVIOUS_CANARY\n")
    before = {p.name: p.read_bytes() for p in page.parent.iterdir() if p.is_file()}
    with pytest.raises(StaleEnvelopeError, match="dirty wiki destination.*nothing was sent"):
        update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert fake.calls == []
    assert {p.name: p.read_bytes() for p in page.parent.iterdir() if p.is_file()} == before


def test_r16_approved_committed_prose_and_independent_commits(repo: Path) -> None:
    fake, page, wiki_commit = prepared_wiki(repo)
    # A source branch with no wiki must not become the previous-wiki input.
    policy = run_git(repo, "rev-parse", "HEAD~2")
    main = run_git(repo, "rev-parse", "HEAD")
    run_git(repo, "checkout", "-q", "-b", "source-only", policy)
    write(repo, "src/widgetry/cli.py", "def main():\n    return 7\n")
    source = commit(repo, "source without wiki")
    run_git(repo, "checkout", "-q", "main")
    settings = settings_with(fake)
    # Explicit approval of this synthetic source ref, independent of the wiki selection.
    receipt = receipt_path(repo)
    approval = json.loads(receipt.read_text())
    approval["refs"].append(source)
    receipt.write_text(json.dumps(approval))
    result = update(repo, settings, ref=source, now=NOW, log=lambda _m: None)
    assert result.plan.new_commit == source and result.plan.wiki_commit == main
    assert len({source, main, policy}) == 3
    assert result.plan.policy_commit == policy
    assert "COMMITTED_APPROVED_PROSE" in "\n".join(fake.calls)
    index = json.loads((page.parent / "index.json").read_text())
    assert index["indexed_commit"] == source
    assert index["previous_wiki_commit"] == result.plan.wiki_commit
    assert index["policy_commit"] == policy
    assert wiki_commit != source and result.problems == []


def test_r16_previous_context_requires_approval(repo: Path) -> None:
    fake, _, _ = prepared_wiki(repo)
    settings = settings_with(fake)
    settings.privacy = {"allowlist": ["*"], "context_classes": ["source", "readme"]}
    with pytest.raises(ValueError, match="previous wiki context is not approved"):
        update(repo, settings, now=NOW, log=lambda _m: None)
    assert fake.calls == []


@pytest.mark.parametrize("selected", [None, "absent-wiki-ref"])
def test_r16_missing_wiki_ref_fails_without_falling_back_to_source(repo: Path, selected: str | None) -> None:
    fake, _, wiki_commit = prepared_wiki(repo)
    run_git(repo, "branch", "-m", "topic")
    with pytest.raises(GenerateError, match="explicit --wiki-ref"):
        update(repo, settings_with(fake), wiki_ref=selected, now=NOW, log=lambda _m: None)
    assert fake.calls == []
    result = update(repo, settings_with(fake), wiki_ref=wiki_commit, now=NOW, log=lambda _m: None)
    assert result.plan.wiki_commit == wiki_commit and result.problems == []


def test_r16_missing_committed_wiki_never_uses_local_pages(repo: Path) -> None:
    fake, _, _ = prepared_wiki(repo)
    settings = settings_with(fake)
    settings.wiki_ref = run_git(repo, "rev-parse", "HEAD~2")
    with pytest.raises(GenerateError, match="committed previous wiki is invalid.*not found"):
        update(repo, settings, now=NOW, log=lambda _m: None)
    assert fake.calls == []


def test_r16_ownership_conflict_refuses_before_sending(repo: Path) -> None:
    fake, page, _ = prepared_wiki(repo)
    record_path(page.parent).unlink()
    with pytest.raises(GenerateError, match="ownership conflict.*nothing was sent"):
        update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert fake.calls == []


def test_r16_moving_wiki_ref_cannot_change_prepared_context(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _, wiki_commit = prepared_wiki(repo)
    original = run_git(repo, "rev-parse", "HEAD")
    policy = run_git(repo, "rev-parse", "HEAD~2")
    run_git(repo, "branch", "previous", wiki_commit)
    real = update_module.read_frozen

    def read(*args: object, **kwargs: object):
        assert kwargs["ref"] == wiki_commit
        run_git(repo, "update-ref", "refs/heads/previous", policy)
        return real(*args, **kwargs)

    monkeypatch.setattr(update_module, "read_frozen", read)
    result = update(repo, settings_with(fake), wiki_ref="previous", now=NOW, log=lambda _m: None)
    assert result.plan.wiki_commit == wiki_commit and result.plan.new_commit == original
    assert "COMMITTED_APPROVED_PROSE" in "\n".join(fake.calls)


def test_r16_destination_modified_during_generation_preserves_edits(repo: Path) -> None:
    from repowiki.fake import answer

    fake, page, _ = prepared_wiki(repo)
    index_before = (page.parent / "index.json").read_bytes()

    def edit(prompt: str) -> str:
        page.write_text(page.read_text() + "\nLOCAL_EDIT_DURING_GENERATION\n")
        return answer(prompt)

    fake.respond = edit
    with pytest.raises(StaleBaseError, match="wiki changed since candidate preparation"):
        update(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    assert "LOCAL_EDIT_DURING_GENERATION" in page.read_text()
    assert all("LOCAL_EDIT_DURING_GENERATION" not in call for call in fake.calls)
    assert (page.parent / "index.json").read_bytes() == index_before


def test_r16_retry_refuses_changed_destination_before_a_second_send(repo: Path) -> None:
    fake, page, _ = prepared_wiki(repo)
    index = page.parent / "index.json"

    def interrupt(prompt: str) -> str:
        index.write_text(index.read_text() + " ")
        raise RunnerError("synthetic interruption")

    fake.respond = interrupt
    settings = settings_with(fake)
    settings.backoff = 0
    with pytest.raises(StaleBaseError):
        update(repo, settings, now=NOW, log=lambda _m: None)
    assert len(fake.calls) == 1
    assert index.read_text().endswith(" ")


def test_r16_fix2_clean_owned_destination_that_differs_from_the_selected_wiki_is_refused(repo: Path) -> None:
    """The selected wiki (main) decides which pages are stale, but a clean, owned, committed destination
    version of a page is what publication would keep: when the two differ the update refuses, and nothing
    is sent, written or re-indexed."""
    fake = FakeRunner()
    generate(repo, settings_with(fake), now=NOW, log=lambda _m: None)
    selected = commit(repo, "selected wiki")
    db = "src/widgetry/store/db.py"
    uncited = sorted((repo / "docs/wiki").glob("*.md"))
    page = next(p for p in uncited if f"../../{db}" not in p.read_text())
    index_before = json.loads((repo / "docs/wiki/index.json").read_text())
    # A clean, committed, owned topic version also documents db.py, which the selected wiki's page never cites.
    page.write_text(page.read_text() + f"\nTOPIC_ONLY_PROSE about [the store](../../{db}#L1-L2).\n")
    adopt_page(page)
    commit(repo, "topic version documents db.py")
    write(repo, db, "class Store:\n    def __init__(self):\n        self.items = [1]\n")  # line ranges stay valid
    commit(repo, "target changes db.py")
    before = {p.name: p.read_bytes() for p in page.parent.iterdir() if p.is_file()}
    fake.calls.clear()

    for dry_run in (True, False):
        with pytest.raises(StaleEnvelopeError, match="differs from the selected previous wiki.*nothing was sent"):
            update(repo, settings_with(fake), dry_run=dry_run, wiki_ref=selected, now=NOW, log=lambda _m: None)
    assert fake.calls == []
    assert {p.name: p.read_bytes() for p in page.parent.iterdir() if p.is_file()} == before
    assert json.loads((page.parent / "index.json").read_text())["indexed_commit"] == index_before["indexed_commit"]
