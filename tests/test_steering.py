"""Committed exact-page steering: ``.repowiki/wiki.json`` is validated before anything uses it."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from repowiki.config import Settings
from repowiki.policy import Policy
from repowiki.steering import (
    MAX_FILE_BYTES,
    MAX_NOTE_CHARS,
    MAX_NOTES,
    MAX_PAGES,
    STEERING_FILE,
    SteeringError,
    load_steering,
    normalize_text,
    parse_steering,
)

# A synthetic, valid steering file (the same shape the README documents).
VALID: dict = {
    "version": 1,
    "repo_notes": ["A small command-line tool that converts recipes between units."],
    "pages": [
        {
            "id": "overview",
            "file": "overview.md",
            "title": "Overview",
            "purpose": "What the tool does and how the parts fit together.",
            "notes": [{"text": "Start from the command-line entry point.", "author": "maintainer"}],
            "sources": [{"file": "src/units/cli.py"}, {"directory": "src/units"}],
            "coverage": [{"directory": "src/units"}],
        },
        {
            "id": "conversion",
            "file": "conversion.md",
            "title": "Unit conversion",
            "purpose": "How quantities are parsed and converted.",
            "parent": "overview",
            "sources": [{"file": "src/units/convert.py"}],
            "coverage": [{"file": "src/units/convert.py"}],
        },
        {
            "id": "parsing",
            "file": "parsing.md",
            "title": "Parsing quantities",
            "purpose": "The grammar for amounts and units.",
            "parent": "conversion",
        },
        {
            "id": "testing",
            "file": "testing.md",
            "title": "Testing",
            "purpose": "How the test suite is organised.",
            "sources": [{"directory": "tests"}],
        },
    ],
}


def valid() -> dict:
    return copy.deepcopy(VALID)


def write(root: Path, data: object) -> Path:
    path = root / STEERING_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def test_valid_steering_yields_exact_ordered_tree_and_coverage(tmp_path: Path) -> None:
    write(tmp_path, valid())
    steering = load_steering(tmp_path)
    assert steering is not None
    assert [p.id for p in steering.pages] == ["overview", "conversion", "parsing", "testing"]
    assert steering.tree() == [("overview", 0), ("conversion", 1), ("parsing", 2), ("testing", 0)]
    assert steering.children(None) == ("overview", "testing")
    assert steering.children("overview") == ("conversion",)
    assert steering.repo_notes[0].text.startswith("A small command-line tool")
    first = steering.page("overview")
    assert first.notes[0].author == "maintainer"
    assert [(s.kind, s.path) for s in first.sources] == [("file", "src/units/cli.py"), ("directory", "src/units")]
    assert steering.coverage() == {
        "overview": (("directory", "src/units"),),
        "conversion": (("file", "src/units/convert.py"),),
        "parsing": (),
        "testing": (),
    }


def test_coverage_declarations_match_exact_files_or_directory_descendants() -> None:
    steering = parse_steering(valid())
    files = ["src/units/cli.py", "src/units/convert.py", "src/units/sub/x.py", "src/unitsx.py", "README.md"]
    assert steering.covered_files("overview", files) == [
        "src/units/cli.py",
        "src/units/convert.py",
        "src/units/sub/x.py",
    ]
    assert steering.covered_files("conversion", files) == ["src/units/convert.py"]
    assert steering.covered_files("parsing", files) == []


def test_absent_steering_file_is_not_an_error(tmp_path: Path) -> None:
    assert load_steering(tmp_path) is None


def test_repeat_load_is_identical(tmp_path: Path) -> None:
    write(tmp_path, valid())
    first, second = load_steering(tmp_path), load_steering(tmp_path)
    assert first == second
    assert first is not None and first.digest == second.digest  # type: ignore[union-attr]
    assert first.tree() == second.tree() and first.coverage() == second.coverage()  # type: ignore[union-attr]


def test_missing_repo_notes_rejects() -> None:
    data = valid()
    del data["repo_notes"]
    with pytest.raises(SteeringError, match="repo_notes"):
        parse_steering(data)


@pytest.mark.parametrize("pages", [[], None, {}])
def test_empty_or_missing_pages_rejects(pages: object) -> None:
    data = valid()
    if pages is None:
        del data["pages"]
    else:
        data["pages"] = pages
    with pytest.raises(SteeringError, match="pages"):
        parse_steering(data)


def test_self_parent_rejects() -> None:
    data = valid()
    data["pages"][0]["parent"] = "overview"
    with pytest.raises(SteeringError, match="own parent"):
        parse_steering(data)


def test_parent_cycle_rejects() -> None:
    data = valid()
    data["pages"][0]["parent"] = "parsing"  # overview -> parsing -> conversion -> overview
    with pytest.raises(SteeringError, match="cycle"):
        parse_steering(data)


def test_unknown_parent_rejects() -> None:
    data = valid()
    data["pages"][3]["parent"] = "nowhere"
    with pytest.raises(SteeringError, match="unknown parent"):
        parse_steering(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "  ＯＶＥＲＶＩＥＷ "),  # full-width letters fold to the first page's title under NFKC + casefold
        ("purpose", "What the  tool DOES and how the parts\tfit together."),
        ("file", "Overview.md"),
        ("id", "overview"),
    ],
)
def test_duplicate_normalized_title_purpose_file_or_id_rejects(field: str, value: str) -> None:
    data = valid()
    data["pages"][3][field] = value
    with pytest.raises(SteeringError, match="duplicate"):
        parse_steering(data)


@pytest.mark.parametrize("purpose", ["", "   ", "\t\n"])
def test_empty_purpose_rejects(purpose: str) -> None:
    data = valid()
    data["pages"][1]["purpose"] = purpose
    with pytest.raises(SteeringError, match="purpose"):
        parse_steering(data)


def test_empty_title_rejects() -> None:
    data = valid()
    data["pages"][1]["title"] = " "
    with pytest.raises(SteeringError, match="title"):
        parse_steering(data)


@pytest.mark.parametrize("page_id", ["", "../x", "a/b", "Overview", "-lead", "has space", "x" * 65, "é", 7])
def test_unsafe_page_id_rejects(page_id: object) -> None:
    data = valid()
    data["pages"][3]["id"] = page_id
    with pytest.raises(SteeringError, match="id"):
        parse_steering(data)


@pytest.mark.parametrize("file", ["../escape.md", "sub/page.md", "page.txt", ".hidden.md", "/abs.md", "index.json"])
def test_unsafe_page_file_rejects(file: str) -> None:
    data = valid()
    data["pages"][3]["file"] = file
    with pytest.raises(SteeringError, match="file"):
        parse_steering(data)


@pytest.mark.parametrize(
    "source",
    [
        {"file": "../outside.py"},
        {"file": "src/../../outside.py"},
        {"directory": "/etc"},
        {"file": "C:/x.py"},
        {"file": "src\\x.py"},
        {"file": ""},
        {"directory": "."},
        {"file": "src/./x.py"},
        {"file": "src//x.py"},
        {"file": "~/x.py"},
        {"file": "src/x.py", "directory": "src"},
        {"path": "src"},
        {"file": 3},
        "src/x.py",
    ],
)
def test_invalid_or_traversing_source_declaration_rejects(source: object) -> None:
    data = valid()
    data["pages"][3]["sources"] = [source]
    with pytest.raises(SteeringError, match="source"):
        parse_steering(data)


@pytest.mark.parametrize(
    "source",
    [
        {"file": ".env"},
        {"file": "config/credentials.json"},
        {"directory": ".ssh"},
        {"directory": "data"},
        {"file": "notes/session-log.md"},
        {"file": "private/plan.md"},  # excluded by the repository's own exclude pattern below
    ],
)
def test_excluded_source_declaration_rejects(source: dict) -> None:
    data = valid()
    data["pages"][3]["sources"] = [source]
    with pytest.raises(SteeringError, match="excluded"):
        parse_steering(data, Policy(("private",)))


@pytest.mark.parametrize("key", ["sources", "coverage"])
def test_excluded_coverage_declaration_rejects_the_same_way(key: str) -> None:
    data = valid()
    data["pages"][3][key] = [{"directory": ".aws"}]
    with pytest.raises(SteeringError, match="excluded"):
        parse_steering(data)


def test_duplicate_source_declaration_rejects() -> None:
    data = valid()
    data["pages"][3]["sources"] = [{"directory": "tests"}, {"directory": "tests/"}]
    with pytest.raises(SteeringError, match="duplicate"):
        parse_steering(data)


def pages(n: int) -> list[dict]:
    return [{"id": f"p{i}", "file": f"p{i}.md", "title": f"Page {i}", "purpose": f"Purpose {i}"} for i in range(n)]


def test_thirty_pages_pass_and_thirty_one_reject() -> None:
    assert MAX_PAGES == 30
    data = valid()
    data["pages"] = pages(30)
    assert len(parse_steering(data).pages) == 30
    data["pages"] = pages(31)
    with pytest.raises(SteeringError, match="at most 30 pages"):
        parse_steering(data)


def test_hundred_notes_across_the_file_pass_and_one_more_rejects() -> None:
    assert MAX_NOTES == 100
    data = valid()
    data["pages"] = pages(30)
    data["repo_notes"] = [f"repo note {i}" for i in range(40)]
    for i, page in enumerate(data["pages"]):
        page["notes"] = [f"page note {i}.{j}" for j in range(2)]  # 40 + 60 = 100
    assert sum(len(p.notes) for p in parse_steering(data).pages) + 40 == 100
    data["pages"][29]["notes"].append("one too many")
    with pytest.raises(SteeringError, match="at most 100 notes"):
        parse_steering(data)


def test_optional_author_does_not_add_a_note() -> None:
    data = valid()
    data["pages"] = pages(1)
    data["repo_notes"] = [{"text": f"n{i}", "author": f"a{i}"} for i in range(100)]
    steering = parse_steering(data)
    assert len(steering.repo_notes) == 100 and steering.repo_notes[99].author == "a99"


def test_ten_thousand_character_note_passes_and_one_more_rejects() -> None:
    assert MAX_NOTE_CHARS == 10_000
    data = valid()
    data["repo_notes"] = ["x" * 10_000]
    assert parse_steering(data).repo_notes[0].text == "x" * 10_000
    data["pages"][0]["notes"] = [{"text": "y" * 10_001}]
    with pytest.raises(SteeringError, match="10000 characters"):
        parse_steering(data)


@pytest.mark.parametrize(
    "note", ["", "  ", {"author": "x"}, {"text": "ok", "author": ""}, {"text": "ok", "extra": 1}, 5]
)
def test_malformed_note_rejects(note: object) -> None:
    data = valid()
    data["repo_notes"] = [note]
    with pytest.raises(SteeringError, match="note"):
        parse_steering(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.pop("version"),
        lambda d: d.update(extra=True),
        lambda d: d["pages"][0].update(extra=True),
        lambda d: d["pages"][0].pop("file"),
        lambda d: d["pages"][0].update(notes="not a list"),
    ],
)
def test_unknown_version_or_keys_reject(mutate) -> None:
    data = valid()
    mutate(data)
    with pytest.raises(SteeringError):
        parse_steering(data)


@pytest.mark.parametrize("text", ["{", "[]", '{"version": 1, "version": 1, "repo_notes": [], "pages": []}'])
def test_malformed_json_or_duplicate_keys_reject(tmp_path: Path, text: str) -> None:
    write(tmp_path, text)
    with pytest.raises(SteeringError):
        load_steering(tmp_path)


def test_symlinked_steering_file_or_directory_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "clone"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps(valid()))
    (root / ".repowiki").mkdir()
    (root / STEERING_FILE).symlink_to(elsewhere)
    with pytest.raises(SteeringError, match="symlink"):
        load_steering(root)
    other = tmp_path / "clone2"
    other.mkdir()
    real = tmp_path / "real-dir"
    real.mkdir()
    (real / "wiki.json").write_text(json.dumps(valid()))
    (other / ".repowiki").symlink_to(real)
    with pytest.raises(SteeringError, match="symlink"):
        load_steering(other)


def test_oversized_steering_file_rejects_before_parsing(tmp_path: Path) -> None:
    write(tmp_path, " " * MAX_FILE_BYTES + "{}")
    with pytest.raises(SteeringError, match="too large"):
        load_steering(tmp_path)


def test_normalization_is_nfkc_trimmed_collapsed_and_casefolded() -> None:
    assert normalize_text("  Straße\u00a0 ＰＬＡＮ\n") == "strasse plan"
    assert normalize_text(normalize_text("Ａ  b")) == normalize_text("Ａ  b")


def test_settings_load_steering_applies_the_repository_exclusions(tmp_path: Path) -> None:
    data = valid()
    data["pages"][3]["sources"] = [{"directory": "vendor"}]
    write(tmp_path, data)
    assert Settings().load_steering(tmp_path) is not None
    with pytest.raises(SteeringError, match="excluded"):
        Settings(exclude=["vendor"]).load_steering(tmp_path)


def test_readme_example_is_valid() -> None:
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    match = re.search(r"<!-- steering-example -->\s*```json\n(.*?)```", readme, re.S)
    assert match, "README must show a steering example"
    steering = parse_steering(json.loads(match.group(1)))
    assert steering.pages and steering.tree()[0][1] == 0


# --------------------------------------------------------------------------- fix round 1


def test_directory_coverage_never_yields_excluded_descendants(tmp_path: Path) -> None:
    files = ["src/units/cli.py", "src/units/credentials.json", "src/units/private.py", "src/units/.env"]
    assert parse_steering(valid()).covered_files("overview", files) == ["src/units/cli.py", "src/units/private.py"]
    steering = parse_steering(valid(), Policy(("private.py",)))
    assert steering.covered_files("overview", files) == ["src/units/cli.py"]
    write(tmp_path, valid())
    loaded = Settings(exclude=["private.py"]).load_steering(tmp_path)
    assert loaded is not None and loaded.covered_files("overview", files) == ["src/units/cli.py"]


def test_largest_valid_notes_load_from_disk(tmp_path: Path) -> None:
    """100 notes of 10,000 characters each load even when every character is escaped as a surrogate pair."""
    data = valid()
    data["pages"] = pages(1)
    data["repo_notes"] = ["\U0001f600" * MAX_NOTE_CHARS for _ in range(MAX_NOTES)]
    path = tmp_path / STEERING_FILE
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    assert path.stat().st_size > 12_000_000
    steering = load_steering(tmp_path)
    assert steering is not None and len(steering.repo_notes) == MAX_NOTES


@pytest.mark.parametrize("field,value", [("id", "testing\n"), ("file", "testing.md\n"), ("parent", "overview\n")])
def test_trailing_newline_in_id_file_or_parent_rejects(field: str, value: str) -> None:
    data = valid()
    data["pages"][3][field] = value
    with pytest.raises(SteeringError):
        parse_steering(data)
