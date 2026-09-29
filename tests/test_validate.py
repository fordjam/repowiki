from __future__ import annotations

import json
from pathlib import Path

from conftest import commit, run_git, write
from repowiki.links import cited_files, find_links, repo_path
from repowiki.validate import check_index, validate

DETAILS = "<details>\n<summary>Relevant source files</summary>\n\n- [cli.py](../../src/widgetry/cli.py)\n\n</details>\n"


def make_wiki(repo: Path, pages: dict[str, str], parents: dict[str, str] | None = None, sha: str | None = None) -> None:
    sha = sha or run_git(repo, "rev-parse", "HEAD")
    for name, body in pages.items():
        write(repo, f"docs/wiki/{name}", body)
    index = {
        "repo": "example/widgetry",
        "indexed_commit": sha,
        "indexed_at": "2026-01-02T03:04:05+00:00",
        "pages": [{"file": n, "title": n[:-3], "parent": (parents or {}).get(n)} for n in pages],
    }
    write(repo, "docs/wiki/index.json", json.dumps(index))


CITE = "Cited: [cli.py:1](../../src/widgetry/cli.py#L1)."


def page(body: str, cite: bool = True) -> str:
    return f"# Title\n\n{DETAILS}\n{body}\n\n{CITE if cite else ''}\n"


def errors(repo: Path) -> list[str]:
    return [p.message for p in validate(repo) if p.level == "error"]


def test_valid_wiki_passes(repo: Path) -> None:
    make_wiki(
        repo,
        {
            "1-overview.md": page(
                "See [engine](../../src/widgetry/engine.py#L30-L31), [one line](../../src/widgetry/cli.py#L6), "
                "[the store dir](../../src/widgetry/store), [next](2-engine.md#details) and "
                "[the web](https://example.com/docs)."
            ),
            "2-engine.md": page("Back to [overview](1-overview.md)."),
        },
        parents={"2-engine.md": "1-overview.md"},
    )
    assert errors(repo) == []


def test_line_range_past_end_of_file(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[x](../../src/widgetry/cli.py#L5-L99)")})
    (msg,) = errors(repo)
    assert "has 9 lines" in msg


def test_reversed_and_malformed_ranges(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[x](../../src/widgetry/cli.py#L5-L2) [y](../../src/widgetry/cli.py#L3-9)")})
    msgs = errors(repo)
    assert any("empty or reversed" in m for m in msgs)
    assert any("bad line fragment" in m for m in msgs)


def test_citations_are_checked_at_the_indexed_commit(repo: Path) -> None:
    old = run_git(repo, "rev-parse", "HEAD")
    write(repo, "src/widgetry/new.py", "x = 1\n")
    commit(repo, "add new.py")
    make_wiki(repo, {"1-a.md": page("[new](../../src/widgetry/new.py#L1)")}, sha=old)
    (msg,) = errors(repo)
    assert "does not exist at" in msg


def test_broken_page_link_and_escape(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[gone](9-missing.md) [out](../../../outside.txt)")})
    msgs = errors(repo)
    assert any("broken link" in m for m in msgs)
    assert any("escapes the repository" in m for m in msgs)


def test_links_inside_code_are_ignored(repo: Path) -> None:
    body = "```md\n[nope](missing.py#L1-L2)\n```\n\nInline `[nope](missing.py)` too."
    make_wiki(repo, {"1-a.md": page(body)})
    assert errors(repo) == []


def test_page_shape_is_required(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": "No title and no sources block.\n"})
    msgs = errors(repo)
    assert any("'# ' title" in m for m in msgs)
    assert any("Relevant source files" in m for m in msgs)


def test_line_ranges_on_binary_files_and_directories(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[logo](../../assets/logo.png#L1) [dir](../../src#L1)")})
    msgs = errors(repo)
    assert any("binary" in m for m in msgs)
    assert any("directory" in m for m in msgs)


def test_index_schema() -> None:
    good = {
        "repo": "a/b",
        "indexed_commit": "0" * 40,
        "indexed_at": "2026-01-01T00:00:00Z",
        "pages": [{"file": "1-a.md", "title": "A", "parent": None}],
    }
    assert check_index(good)[0] == []
    bad = {
        "repo": "",
        "indexed_commit": "not-a-sha",
        "indexed_at": "yesterday",
        "pages": [
            {"file": "../escape.md", "title": "x", "parent": None},
            {"file": "1-a.md", "title": "", "parent": "2-b.md"},
            {"file": "2-b.md", "title": "B", "parent": "1-a.md"},
            {"file": "1-a.md", "title": "dup", "parent": None},
            {"file": "3-c.md", "title": "C", "parent": "9-z.md"},
        ],
    }
    msgs = [p.message for p in check_index(bad)[0]]
    for needle in (
        '"repo"',
        '"indexed_commit"',
        '"indexed_at"',
        "plain .md",
        "title",
        "cycle",
        "twice",
        "not a listed",
    ):
        assert any(needle in m for m in msgs), needle
    assert check_index({"repo": "a", "indexed_commit": "0" * 40, "indexed_at": "2026-01-01", "pages": []})[0]


def test_missing_index_and_unknown_commit(repo: Path) -> None:
    assert "not found" in errors(repo)[0]
    make_wiki(repo, {"1-a.md": page("hi")}, sha="f" * 40)
    assert any("is not a commit" in m for m in errors(repo))


def test_unlisted_page_is_a_warning(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("hi")})
    write(repo, "docs/wiki/stray.md", "# Stray\n")
    probs = validate(repo)
    assert [p.level for p in probs] == ["warning"]


def test_link_helpers() -> None:
    links = find_links("# T\n\n[a](x.py#L1-L2) ![img](pic.png) [w](https://example.com)\n")
    assert [(link.path, link.lines, link.image, link.external) for link in links] == [
        ("x.py", (1, 2), False, False),
        ("pic.png", None, True, False),
        ("", None, False, True),
    ]
    assert repo_path("1-a.md", "docs/wiki", "../../src/a.py") == "src/a.py"
    assert repo_path("1-a.md", "docs/wiki", "../../../a.py") is None
    text = "[a](../../src/a.py#L1) [b](2-b.md) [c](../../src/a.py) [d](../../lib/b.py)"
    assert cited_files(text, "1-a.md", "docs/wiki") == ["src/a.py", "lib/b.py"]


def test_uncited_pages_fail_with_a_configurable_minimum(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("Claims with no evidence.", cite=False)})
    (msg,) = errors(repo)
    assert "0 line citation" in msg
    assert validate(repo, min_citations=0) == []
    make_wiki(repo, {"1-a.md": page("One citation only.")})
    assert errors(repo) == []
    assert any("at least 2" in p.message for p in validate(repo, min_citations=2))
