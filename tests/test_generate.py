from __future__ import annotations

import json
from pathlib import Path

import pytest

from repowiki import generate as gen
from repowiki.config import Settings, load_settings
from repowiki.fake import answer
from repowiki.git import Snapshot
from repowiki.runners import FakeRunner
from repowiki.survey import citable, survey
from repowiki.validate import validate


def run(repo: Path, cheap: FakeRunner | None = None, strong: FakeRunner | None = None, **kw: object) -> gen.Result:
    settings = Settings(cheap="fake", strong="fake", min_pages=2, **kw)  # type: ignore[arg-type]
    cheap = cheap or FakeRunner()
    strong = strong or cheap
    settings.runners = lambda: (cheap, strong)  # type: ignore[method-assign]
    return gen.generate(repo, settings, log=lambda _msg: None)


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


def test_bad_citations_are_repaired_then_removed(repo: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return (
            "```markdown\n# Whatever\n\nGood [cli](src/widgetry/cli.py#L1-L3), "
            "bad [ghost](src/ghost.py#L1), wrong [range](src/widgetry/cli.py#L1-L500).\n```"
        )

    fake = FakeRunner(respond=respond)
    result = run(repo, fake)
    assert any(c.startswith("TASK: repair") for c in fake.calls)
    assert result.problems == []
    text = (repo / "docs/wiki/1-overview.md").read_text()
    assert "[cli](../../src/widgetry/cli.py#L1-L3)" in text
    assert "ghost" in text and "src/ghost.py" not in text
    assert "#L1-L500" not in text
    assert any("removed" in w for w in result.warnings)


def test_two_pass_uses_strong_model_for_final_text(repo: Path) -> None:
    cheap = FakeRunner("cheap")
    strong = FakeRunner("strong", respond=lambda p: "# Final\n\nPolished by the strong model.\n")
    result = run(repo, cheap, strong)
    assert all(c.startswith(("TASK: plan", "TASK: page")) for c in cheap.calls)
    assert all(c.startswith("TASK: revise") for c in strong.calls)
    assert len(strong.calls) == len(result.pages)
    for page in result.pages:
        text = (repo / "docs/wiki" / page.file).read_text()
        assert text.startswith(f"# {page.title}\n") and "Polished by the strong model." in text


def test_regenerate_removes_pages_it_no_longer_lists(repo: Path) -> None:
    run(repo)
    stale = repo / "docs/wiki/9-old.md"
    stale.write_text("# Old\n")
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
