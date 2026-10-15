from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import adopt_page
from repowiki import generate as gen
from repowiki.config import Settings, load_settings
from repowiki.fake import answer
from repowiki.git import Snapshot
from repowiki.runners import FakeRunner, RunnerError
from repowiki.safety import GENERATED_MARK
from repowiki.survey import citable, survey
from repowiki.validate import validate

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the indexed_at instant is the caller's, never the clock


def run(repo: Path, cheap: FakeRunner | None = None, strong: FakeRunner | None = None, **kw: object) -> gen.Result:
    settings = Settings(cheap="fake", strong="fake", min_pages=2, **kw)  # type: ignore[arg-type]
    cheap = cheap or FakeRunner()
    strong = strong or cheap
    settings.runners = lambda: (cheap, strong)  # type: ignore[method-assign]
    return gen.generate(repo, settings, log=lambda _msg: None, now=NOW)


def test_survey_finds_readme_entry_points_and_tests(repo: Path) -> None:
    s = survey(Snapshot.at(repo), "docs/wiki")
    assert s.readme == "README.md"
    assert "src/widgetry/cli.py" in s.entry_points
    assert s.tests == ["tests/test_engine.py"]
    assert "assets/logo.png" not in s.files
    assert not citable(".env.local", "docs/wiki") and not citable("docs/wiki/1-a.md", "docs/wiki")


def test_generate_with_fake_backend_produces_a_valid_wiki(repo: Path) -> None:
    result = run(repo)
    assert result.problems == []
    wiki = repo / "docs" / "wiki"
    index = json.loads((wiki / "index.json").read_text())
    assert set(index) == {"repo", "indexed_commit", "indexed_at", "pages"}
    assert index["repo"] == "widgetry"
    assert index["indexed_commit"] == result.commit
    assert index["pages"][0] == {"file": "1-overview.md", "title": "Overview", "parent": None}
    for entry in index["pages"]:
        text = (wiki / entry["file"]).read_text()
        assert text.startswith(f"# {entry['title']}\n")
        assert "<summary>Relevant source files</summary>" in text
    assert "(../../src/widgetry/cli.py#L1-L" in (wiki / "1-overview.md").read_text()
    assert validate(repo) == []


def test_r31_indexed_at_is_the_callers_instant_never_the_clock(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class NoClock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            raise AssertionError("the wall clock was read")

    monkeypatch.setattr(gen, "datetime", NoClock)
    assert gen.index_data("widgetry", "abc", [], now=NOW)["indexed_at"] == "2026-06-01T12:00:00+00:00"
    with pytest.raises(TypeError):
        gen.index_data("widgetry", "abc", [])  # type: ignore[call-arg]
    with pytest.raises(gen.GenerateError, match="timezone-aware"):
        gen.index_data("widgetry", "abc", [], now=datetime(2026, 6, 1, 12, 0, 0))
    with pytest.raises(TypeError):
        gen.generate(repo, Settings(cheap="fake"))  # type: ignore[call-arg]
    result = run(repo)
    assert result.problems == []
    index = json.loads((repo / "docs" / "wiki" / "index.json").read_text())
    assert index["indexed_at"] == "2026-06-01T12:00:00+00:00"


def test_plan_json_and_parents(repo: Path) -> None:
    plan = {
        "pages": [
            {"title": "Overview", "parent": None, "summary": "s", "sources": ["README.md", "./src/widgetry/cli.py"]},
            {"title": "Engine", "parent": "Overview", "sources": ["src/widgetry/engine.py", "not/real.py"]},
            {"title": "Engine", "parent": None, "sources": []},
            {"title": "Store & Data!", "parent": 1, "sources": ["src/widgetry/store/db.py"]},
        ]
    }
    fake = FakeRunner(
        respond=lambda p: "Here you go:\n" + json.dumps(plan) if p.startswith("TASK: plan") else answer(p)
    )
    result = run(repo, fake)
    assert [(p.file, p.parent) for p in result.pages] == [
        ("1-overview.md", None),
        ("2-engine.md", "1-overview.md"),
        ("3-store-data.md", "1-overview.md"),
    ]
    assert result.pages[0].sources == ["README.md", "src/widgetry/cli.py"]
    assert result.pages[1].sources == ["src/widgetry/engine.py"]


def test_unusable_plan_falls_back_to_structure(repo: Path) -> None:
    fake = FakeRunner(respond=lambda p: "I cannot do JSON" if p.startswith("TASK: plan") else answer(p))
    result = run(repo, fake)
    assert result.pages[0].title == "Overview"
    assert any(p.title == "Testing" for p in result.pages)
    assert sum(c.startswith("TASK: plan") for c in fake.calls) == 2
    assert result.problems == []


def test_bad_citations_are_corrected_or_their_blocks_removed(repo: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        if prompt.startswith("TASK: repair"):
            return (
                "# Whatever\n\nGood [cli](src/widgetry/cli.py#L1-L3).\n\n"
                "The range is wrong [range](src/widgetry/cli.py#L1-L9).\n"
            )  # the ghost paragraph is gone; the range is corrected
        return (
            "```markdown\n# Whatever\n\nGood [cli](src/widgetry/cli.py#L1-L3).\n\n"
            "Ghosts live here [ghost](src/ghost.py#L1).\n\n"
            "The range is wrong [range](src/widgetry/cli.py#L1-L500).\n```"
        )

    fake = FakeRunner(respond=respond)
    result = run(repo, fake)
    assert any(c.startswith("TASK: repair") for c in fake.calls)
    assert result.problems == []
    text = (repo / "docs/wiki/1-overview.md").read_text()
    assert "[cli](../../src/widgetry/cli.py#L1-L3)" in text
    assert "ghost" not in text and "#L1-L500" not in text
    assert "[range](../../src/widgetry/cli.py#L1-L9)" in text
    assert any(": removed: ghosts live here" in w for w in result.warnings)
    assert any(": corrected: the range is wrong" in w for w in result.warnings)


def test_two_pass_uses_strong_model_for_final_text(repo: Path) -> None:
    cheap = FakeRunner("cheap")
    strong = FakeRunner(
        "strong", respond=lambda p: "# Final\n\nPolished by the strong model ([cli](src/widgetry/cli.py#L1-L2)).\n"
    )
    result = run(repo, cheap, strong)
    assert all(c.startswith(("TASK: plan", "TASK: page")) for c in cheap.calls)
    assert all(c.startswith("TASK: revise") for c in strong.calls)
    assert len(strong.calls) == len(result.pages)
    for page in result.pages:
        text = (repo / "docs/wiki" / page.file).read_text()
        assert text.startswith(f"# {page.title}\n") and "Polished by the strong model" in text


def test_regenerate_removes_pages_it_no_longer_lists(repo: Path) -> None:
    run(repo)
    stale = repo / "docs/wiki/9-old.md"
    stale.write_text(f"# Old\n\n{GENERATED_MARK}\n")
    adopt_page(stale)
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    index["pages"].append({"file": "9-old.md", "title": "Old", "parent": None})
    (repo / "docs/wiki/index.json").write_text(json.dumps(index))
    run(repo)
    assert not stale.exists()


def test_relink_and_clean_reply(repo: Path) -> None:
    snap = Snapshot.at(repo)
    text = gen.clean_reply("Sure! Here it is.\n# T\n\n[a](./src/widgetry/cli.py#L2) [p](2-x.md)\n", "T")
    assert text.startswith("# T\n")
    out = gen.relink(text + "\n```\n[z](src/widgetry/cli.py)\n```\n", "1-a.md", "docs/wiki", snap, {"2-x.md"})
    assert "[a](../../src/widgetry/cli.py#L2)" in out and "[p](2-x.md)" in out
    assert "[z](src/widgetry/cli.py)" in out  # code blocks untouched


def test_settings_file_and_overrides(tmp_path: Path) -> None:
    (tmp_path / ".repowiki.toml").write_text(
        'wiki_dir = "wiki"\nmax_pages = 6\n[models]\ncheap = "ollama:small"\nstrong = "claude:opus"\n'
    )
    s = load_settings(tmp_path)
    assert (s.wiki_dir, s.max_pages, s.cheap_spec, s.strong_spec, s.two_pass) == (
        "wiki",
        6,
        "ollama:small",
        "claude:opus",
        True,
    )
    s = load_settings(tmp_path, {"cheap": "codex", "strong": "codex"})
    assert not s.two_pass
    assert Settings().cheap_spec == Settings().strong_spec == "claude"
    (tmp_path / ".repowiki.toml").write_text("bogus = 1\n")
    with pytest.raises(ValueError):
        load_settings(tmp_path)


# --------------------------------------------------------------------------- R5: fragments and distinct valid evidence


def test_r5_generated_links_to_missing_headings_are_removed(repo: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return (
            "# Whatever\n\n## Usage\n\nGood [cli](src/widgetry/cli.py#L1-L3), [up](1-overview.md#overview), "
            "[here](#usage), [gone](1-overview.md#nowhere) and [self](#nope).\n"
        )

    result = run(repo, FakeRunner(respond=respond))
    assert result.problems == []
    wiki = repo / "docs/wiki"
    later = [p.file for p in result.pages if p.file != "1-overview.md"]
    assert later
    for file in [*later, "1-overview.md"]:
        text = (wiki / file).read_text()
        assert "[up](1-overview.md#overview)" in text and "[here](#usage)" in text
        assert "#nowhere" not in text and "#nope" not in text and "gone" in text and "self" in text
    # a fragment into another page is checked once that page exists, against its candidate text
    assert any(w.startswith(f"{later[0]}: removed 1 link(s) to missing headings") for w in result.warnings)
    assert validate(repo) == []


def test_r5_removed_invalid_citation_lowers_the_count(repo: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return (
            "# W\n\nGood [a](src/widgetry/cli.py#L1), same [b](src/widgetry/cli.py#L1-L1), "
            "bad [c](src/widgetry/cli.py#L1-L500).\n"
        )

    with pytest.raises(gen.GenerateError, match=r"has 1 line citation\(s\); at least 2 required"):
        run(repo, FakeRunner(respond=respond), min_citations=2)
    assert not (repo / "docs/wiki/index.json").exists()


# --------------------------------------------------------------------------- R13: final pass and block-level repair

CLI = "[cli](src/widgetry/cli.py#L1-L3)"
ENGINE = "[engine](src/widgetry/engine.py#L1-L5)"


def scripted(page: str, repair: str | None = None) -> FakeRunner:
    """One backend that drafts `page` and, when asked to repair, answers `repair` (default: unchanged)."""

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return repair if repair is not None and prompt.startswith("TASK: repair") else page

    return FakeRunner(respond=respond)


def no_wiki(repo: Path) -> bool:
    return not (repo / "docs/wiki/index.json").exists()


@pytest.mark.parametrize("reply", ["", "  \n\n", "# Final\n"])
def test_r13_missing_final_output_fails(repo: Path, reply: str) -> None:
    strong = FakeRunner("strong", respond=lambda _p: reply)
    with pytest.raises(gen.GenerateError, match="required final pass returned no page"):
        run(repo, FakeRunner("cheap"), strong)
    assert no_wiki(repo)


def test_r13_a_failing_final_pass_fails_instead_of_keeping_the_draft(repo: Path) -> None:
    def broken(_prompt: str) -> str:
        raise RunnerError("quota exhausted", transient=False)

    with pytest.raises(gen.GenerateError, match="required final pass failed"):
        run(repo, FakeRunner("cheap"), FakeRunner("strong", respond=broken))
    assert no_wiki(repo)


def test_r13_one_failed_page_prevents_all_publication(repo: Path) -> None:
    def respond(prompt: str) -> str:
        if 'page "Testing"' in prompt:
            raise RunnerError("quota exhausted", transient=False)
        return answer(prompt.replace("TASK: revise", "TASK: page"))

    cheap = FakeRunner("cheap")
    with pytest.raises(gen.GenerateError, match=r"1 of 4 page\(s\) not written") as caught:
        run(repo, cheap, FakeRunner("strong", respond=respond))
    assert "4-testing.md" in str(caught.value) and no_wiki(repo)
    kept = list((repo / ".git/repowiki/checkpoints").glob("page-*.txt"))
    assert len(kept) == 3  # the finished pages are resumable; the failed one has no checkpoint at all


def test_r13_an_unresolved_repair_fails(repo: Path) -> None:
    page = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [store](src/ghost.py#L1).\n"
    with pytest.raises(gen.GenerateError, match="ghost.py"):
        run(repo, scripted(page))  # the repair hands back the same page
    assert no_wiki(repo)


def test_r13_exact_minimum_passes(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [store](src/ghost.py#L1).\n"
    fixed = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [store](src/widgetry/store/db.py#L1-L3).\n"
    result = run(repo, scripted(draft, fixed), min_citations=2)
    assert result.problems == [] and validate(repo) == []
    assert any(": corrected: " in w for w in result.warnings)  # the corrected block is what supplies the 2nd


def test_r13_one_fewer_than_the_minimum_fails(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [store](src/ghost.py#L1).\n"
    dropped = f"# P\n\nThe CLI starts here {CLI}.\n"  # the block is removed, leaving one valid citation
    with pytest.raises(gen.GenerateError, match=r"has 1 line citation\(s\); at least 2 required"):
        run(repo, scripted(draft, dropped), min_citations=2)
    assert no_wiki(repo)


def test_r13_removing_only_the_bad_link_and_keeping_the_assertion_fails(repo: Path) -> None:
    """A valid citation elsewhere on the page must not make an unsupported sentence acceptable."""
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [ghost](src/ghost.py#L1).\n"
    unlinked = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows ghost.\n"
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, unlinked))
    assert no_wiki(repo)


BLOCKS = {
    "paragraph": (
        "The store keeps rows [store](src/ghost.py#L1).\n\nThe CLI starts here {cli}.\n",
        "The store keeps rows [store](src/widgetry/store/db.py#L1-L3).\n\nThe CLI starts here {cli}.\n",
        "The CLI starts here {cli}.\n",
    ),
    "list item": (
        "- The store keeps rows [store](src/ghost.py#L1)\n- The CLI starts here {cli}\n",
        "- The store keeps rows [store](src/widgetry/store/db.py#L1-L3)\n- The CLI starts here {cli}\n",
        "- The CLI starts here {cli}\n",
    ),
    "table row": (
        "| Part | Where |\n| --- | --- |\n| store | [store](src/ghost.py#L1) |\n| cli | {cli} |\n",
        "| Part | Where |\n| --- | --- |\n| store | [store](src/widgetry/store/db.py#L1-L3) |\n| cli | {cli} |\n",
        "| Part | Where |\n| --- | --- |\n| cli | {cli} |\n",
    ),
}


@pytest.mark.parametrize("fix", ["corrected", "removed"])
@pytest.mark.parametrize("kind", list(BLOCKS))
def test_r13_corrected_evidence_or_a_removed_block_passes(repo: Path, kind: str, fix: str) -> None:
    bad, corrected, removed = (text.format(cli=CLI) for text in BLOCKS[kind])
    result = run(repo, scripted(f"# P\n\n{bad}", f"# P\n\n{corrected if fix == 'corrected' else removed}"))
    assert result.problems == [] and validate(repo) == []
    text = (repo / "docs/wiki/1-overview.md").read_text()
    assert "ghost" not in text
    assert ("db.py#L1-L3" in text) == (fix == "corrected")
    assert any(w.startswith("repair 1-overview.md:") and f": {fix}: " in w for w in result.warnings)


def test_r13_a_repair_that_rewrites_the_claim_without_evidence_is_still_refused(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [ghost](src/ghost.py#L1).\n"
    unlinked = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows in ghost.\n"  # same claim, link gone
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, unlinked))


def test_r13_a_valid_citation_elsewhere_in_the_paragraph_does_not_repair_a_bad_one(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}. The store keeps rows [store](missing.py#L1).\n"
    unlinked = f"# P\n\nThe CLI starts here {CLI}. The store keeps rows store.\n"
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, unlinked))
    assert no_wiki(repo)


def test_r13_deleting_the_claim_beside_a_valid_citation_is_a_removal(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}. The store keeps rows [store](missing.py#L1).\n"
    result = run(repo, scripted(draft, f"# P\n\nThe CLI starts here {CLI}.\n"))
    assert result.problems == [] and "missing.py" not in (repo / "docs/wiki/1-overview.md").read_text()
    assert any(": removed: the store keeps rows" in w for w in result.warnings)


LONG_LABEL = "implementation details and line references for the persistent storage subsystem"


def test_r13_removing_a_long_citation_label_leaves_the_claim_unresolved(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [{LONG_LABEL}](missing.py#L1)\n"
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows.\n"))
    assert no_wiki(repo)


def test_r13_a_long_label_left_as_plain_text_is_still_the_same_claim(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [{LONG_LABEL}](missing.py#L1)\n"
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows {LONG_LABEL}\n"))


def test_r13_a_long_label_claim_can_be_corrected_or_removed(repo: Path) -> None:
    draft = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [{LONG_LABEL}](missing.py#L1)\n"
    fixed = f"# P\n\nThe CLI starts here {CLI}.\n\nThe store keeps rows [db](src/widgetry/store/db.py#L1-L3)\n"
    assert any(": corrected: " in w for w in run(repo, scripted(draft, fixed)).warnings)


def test_r13_a_link_only_claim_never_matches_another_paragraph(repo: Path) -> None:
    """The second paragraph is all link; unlinked, it must not be taken for the empty-looking first one."""
    draft = "# P\n\n[CLI](src/widgetry/cli.py#L1).\n\n[The store keeps rows](missing.py#L1).\n"
    unlinked = "# P\n\n[CLI](src/widgetry/cli.py#L1).\n\nThe store keeps rows.\n"
    with pytest.raises(gen.GenerateError, match="without valid evidence"):
        run(repo, scripted(draft, unlinked))
    assert no_wiki(repo)
