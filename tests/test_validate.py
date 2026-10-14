from __future__ import annotations

import json
from pathlib import Path

from conftest import commit, run_git, write
from repowiki.git import Snapshot
from repowiki.links import cited_files, find_links, repo_path
from repowiki.render import heading_ids, render
from repowiki.validate import check_index, check_wiki, validate

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
            "2-engine.md": page("## Details\n\nBack to [overview](1-overview.md)."),
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


# --------------------------------------------------------------------------- R4: canonical citations, HTML evidence

HOST = "https://code.example.test/example/widgetry"  # synthetic registered identity (the origin remote)


def with_origin(repo: Path) -> str:
    run_git(repo, "remote", "add", "origin", "git@code.example.test:example/widgetry.git")
    return run_git(repo, "rev-parse", "HEAD")


def test_r4_pinned_web_citation_is_validated_and_counts(repo: Path) -> None:
    sha = with_origin(repo)
    make_wiki(repo, {"1-a.md": page(f"[cli]({HOST}/blob/{sha}/src/widgetry/cli.py#L1-L3)", cite=False)})
    assert errors(repo) == []  # the web citation satisfies min_citations=1 like a relative one
    make_wiki(repo, {"1-a.md": page(f"[cli]({HOST}/blob/{sha}/src/widgetry/cli.py#L5-L99)", cite=False)})
    msgs = errors(repo)
    assert any("has 9 lines" in m for m in msgs)  # range checked at the indexed commit, exactly as for a relative link
    assert any("has 0 line citation(s)" in m for m in msgs)  # R5: an invalid citation is never counted
    make_wiki(repo, {"1-a.md": page(f"[gone]({HOST}/blob/{sha}/src/widgetry/missing.py#L1)")})
    assert any("does not exist at" in m for m in errors(repo))


def test_r4_unpinned_or_foreign_web_links_are_not_silent_evidence(repo: Path) -> None:
    sha = with_origin(repo)
    make_wiki(repo, {"1-a.md": page(f"[cli]({HOST}/blob/main/src/widgetry/cli.py#L1)")})
    assert any("not the full indexed commit" in m for m in errors(repo))
    foreign = f"https://code.example.test/other/widgetry/blob/{sha}/src/widgetry/cli.py#L1"
    make_wiki(repo, {"1-a.md": page(f"[elsewhere]({foreign})", cite=False)})
    (msg,) = errors(repo)
    assert "has 0 line citation(s)" in msg  # a link to another repository is never a citation


def test_r4_aliases_register_more_identities(repo: Path) -> None:
    sha = with_origin(repo)
    alias = "https://gitlab.example.test/mirror/widgetry"
    make_wiki(repo, {"1-a.md": page(f"[cli]({alias}/-/blob/{sha}/src/widgetry/cli.py#L1-2)", cite=False)})
    assert "has 0 line citation(s)" in errors(repo)[0]  # unregistered: an external link, not evidence
    assert [p.message for p in validate(repo, aliases=(alias,)) if p.level == "error"] == []
    make_wiki(repo, {"1-a.md": page(f"[cli]({alias}/-/blob/main/src/widgetry/cli.py#L1-2)")})
    assert any("not the full indexed commit" in p.message for p in validate(repo, aliases=(alias,)))


def test_r4_html_source_anchor_fails_with_a_markdown_conversion_diagnostic(repo: Path) -> None:
    sha = with_origin(repo)
    for html in (
        '<a href="../../src/widgetry/cli.py#L1-L2">cli</a>',
        f'<a href="{HOST}/blob/{sha}/src/widgetry/cli.py#L1">cli</a>',
        '<img src="../../assets/logo.png">',
        f'<p><a href="{HOST}/blob/main/src/widgetry/cli.py">cli</a></p>',
    ):
        make_wiki(repo, {"1-a.md": page(f"Text.\n\n{html}\n")})
        (msg,) = errors(repo)
        assert "raw HTML" in msg and "write it as a Markdown link" in msg, html
    make_wiki(repo, {"1-a.md": page('<a href="https://example.test/">web</a> and <a href="2-b.md">b</a>')})
    assert errors(repo) == []  # HTML that does not point at source stays escaped text, not an error
    make_wiki(repo, {"1-a.md": page("```html\n<a href=\"../../src/widgetry/cli.py#L1\">x</a>\n```")})
    assert errors(repo) == []  # code examples are ignored


def test_r4_html_anchor_never_satisfies_the_citation_minimum(repo: Path) -> None:
    with_origin(repo)
    make_wiki(repo, {"1-a.md": page('<a href="../../src/widgetry/cli.py#L1">cli</a>', cite=False)})
    msgs = errors(repo)
    assert any("has 0 line citation(s)" in m for m in msgs) and any("raw HTML" in m for m in msgs)


def test_r4_encoded_traversal_is_an_error(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[x](../../src/%2e%2e/widgetry/cli.py#L1)")})
    assert any("encoded" in m for m in errors(repo))


# --------------------------------------------------------------------------- R5: fragments, distinct valid evidence


def test_r5_missing_page_or_heading_fragment_fails(repo: Path) -> None:
    b = page("## Setup\n\nSteps.")
    make_wiki(repo, {"1-a.md": page("[ok](2-b.md#setup) [top](#title) [same](1-a.md#title)"), "2-b.md": b})
    assert errors(repo) == []
    make_wiki(repo, {"1-a.md": page("[gone](9-missing.md#setup)"), "2-b.md": b})
    assert any("broken link" in m for m in errors(repo))
    make_wiki(repo, {"1-a.md": page("[nope](2-b.md#install)"), "2-b.md": b})
    (msg,) = errors(repo)
    assert "no heading with id #install on 2-b.md" in msg
    make_wiki(repo, {"1-a.md": page("[nope](#install)"), "2-b.md": b})
    (msg,) = errors(repo)
    assert "no heading with id #install on this page" in msg


def test_r5_fragment_into_an_unlisted_file_cannot_be_checked(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[stray](stray.md#stray)")})
    write(repo, "docs/wiki/stray.md", "# Stray\n")
    assert any("cannot be checked" in m for m in errors(repo))


def test_r5_reversed_zero_out_of_range_binary_and_non_line_fragments_fail(repo: Path) -> None:
    cases = {
        "[r](../../src/widgetry/cli.py#L5-L2)": "empty or reversed",
        "[z](../../src/widgetry/cli.py#L0)": "empty or reversed",
        "[o](../../src/widgetry/cli.py#L10)": "has 9 lines",
        "[b](../../assets/logo.png#L1)": "binary",
        "[f](../../src/widgetry/cli.py#main)": "unsupported fragment on a source link",
        "[d](../../src/widgetry#readme)": "unsupported fragment on a source link",
    }
    for link, needle in cases.items():
        make_wiki(repo, {"1-a.md": page(link)})
        (msg,) = errors(repo)
        assert needle in msg, link


def test_r5_duplicate_headings_get_numbered_ids_shared_with_rendering(repo: Path) -> None:
    text = "# T\n\n## Setup\n\n## Setup\n\n### Setup!\n"
    assert heading_ids(text) == {"t", "setup", "setup-1", "setup-2"}
    rendered = render(text).html
    for anchor in ("setup", "setup-1", "setup-2"):
        assert f'id="{anchor}"' in rendered
    b = page("## Setup\n\nOne.\n\n## Setup\n\nTwo.")
    make_wiki(repo, {"1-a.md": page("[second](2-b.md#setup-1)"), "2-b.md": b})
    assert errors(repo) == []
    make_wiki(repo, {"1-a.md": page("[third](2-b.md#setup-2)"), "2-b.md": b})
    assert any("no heading with id #setup-2" in m for m in errors(repo))


def test_r5_line_ranges_use_the_indexed_blob_not_the_working_tree(repo: Path) -> None:
    write(repo, "src/widgetry/long.py", "".join(f"x{n} = {n}\n" for n in range(42)))
    commit(repo, "a 42-line file")
    sha = run_git(repo, "rev-parse", "HEAD")
    write(repo, "src/widgetry/long.py", "".join(f"x{n} = {n}\n" for n in range(60)))  # longer, uncommitted
    make_wiki(repo, {"1-a.md": page("[end](../../src/widgetry/long.py#L42)")}, sha=sha)
    assert errors(repo) == []
    make_wiki(repo, {"1-a.md": page("[past](../../src/widgetry/long.py#L43)")}, sha=sha)
    (msg,) = errors(repo)
    assert "has 42 lines" in msg
    write(repo, "src/widgetry/long.py", "x = 1\n")  # shorter, uncommitted
    make_wiki(repo, {"1-a.md": page("[end](../../src/widgetry/long.py#L42)")}, sha=sha)
    assert errors(repo) == []


def test_r5_invalid_citations_do_not_count(repo: Path) -> None:
    body = "[ok](../../src/widgetry/cli.py#L2) [bad](../../src/widgetry/cli.py#L5-L99)"
    make_wiki(repo, {"1-a.md": page(body, cite=False)})
    msgs = [p.message for p in validate(repo, min_citations=2) if p.level == "error"]
    assert msgs[0].startswith("has 1 line citation(s); at least 2 required")  # the invalid one is not counted
    assert any("has 9 lines" in m for m in msgs)


def test_r5_equivalent_citations_count_once(repo: Path) -> None:
    sha = with_origin(repo)
    body = (
        "[a](../../src/widgetry/cli.py#L1) [b](../../src/widgetry/cli.py#L1-L1) "
        f"[c]({HOST}/blob/{sha}/src/widgetry/cli.py#L1) [d][ref]\n\n[ref]: ../../src/widgetry/./cli.py#L1\n"
    )
    make_wiki(repo, {"1-a.md": page(body, cite=False)})
    msgs = [p.message for p in validate(repo, min_citations=2) if p.level == "error"]
    assert len(msgs) == 1 and msgs[0].startswith("has 1 line citation(s); at least 2 required")


def test_r5_two_distinct_valid_ranges_pass(repo: Path) -> None:
    body = "[a](../../src/widgetry/cli.py#L1) [b][ref]\n\n[ref]: ../../src/widgetry/cli.py#L2-L3\n"
    make_wiki(repo, {"1-a.md": page(body, cite=False)})
    assert [p for p in validate(repo, min_citations=2) if p.level == "error"] == []
    html = page(body + '\n<a href="../../src/widgetry/engine.py#L1">x</a>\n', cite=False)
    make_wiki(repo, {"1-a.md": html})
    msgs = [p.message for p in validate(repo, min_citations=3) if p.level == "error"]
    assert any(m.startswith("has 2 line citation(s)") for m in msgs)  # raw HTML never counts (R4)


def test_r5_cross_page_fragment_only_in_the_working_tree_fails(repo: Path) -> None:
    make_wiki(repo, {"1-a.md": page("[s](2-b.md#setup)"), "2-b.md": page("## Setup\n\nOn disk only.")})
    assert errors(repo) == []
    data = json.loads((repo / "docs/wiki/index.json").read_text())
    candidate = {
        "1-a.md": (repo / "docs/wiki/1-a.md").read_text(),
        "2-b.md": page("## Install\n\nThe candidate renamed the heading."),
    }
    probs = check_wiki(
        data, candidate, root=repo, wiki_dir="docs/wiki", wiki=repo / "docs/wiki", snap=Snapshot.at(repo),
    )  # fmt: skip
    assert [p.message for p in probs if p.level == "error"] == ["no heading with id #setup on 2-b.md: 2-b.md#setup"]


def test_r5_reference_links_follow_the_same_fragment_rules(repo: Path) -> None:
    b = page("## Setup\n\nSteps.")
    make_wiki(repo, {"1-a.md": page("[s][r]\n\n[r]: 2-b.md#nope\n"), "2-b.md": b})
    assert any("no heading with id #nope" in m for m in errors(repo))
    make_wiki(repo, {"1-a.md": page('<a href="2-b.md#nope">b</a>'), "2-b.md": b})
    assert errors(repo) == []  # raw HTML is escaped text when rendered (R4): not a link to check
