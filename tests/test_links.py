"""Canonical source citations: relative links and registered web URLs resolve to one target (R4).

All hosts and identities here are synthetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repowiki.links import RepoIdentity, SourceTarget, cited_files, find_links, identities, resolve_href

SHA = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
HUB = "https://code.example.test/acme/widgetry"  # GitHub-style host
LAB = "https://gitlab.example.test/group/sub/widgetry"  # GitLab-style host with a nested namespace
IDS = identities([HUB, LAB])
PAGE, WIKI = "1-overview.md", "docs/wiki"


def resolve(href: str, ids: tuple[RepoIdentity, ...] = IDS, commit: str | None = SHA):
    return resolve_href(href, PAGE, WIKI, repo_identities=ids, commit=commit)


def source(href: str) -> SourceTarget:
    res = resolve(href)
    assert res.kind == "source", (href, res)
    assert res.target is not None
    return res.target


def problem(href: str) -> str:
    res = resolve(href)
    assert res.kind == "invalid" and res.problem, (href, res)
    return res.problem


# --------------------------------------------------------------------------- accepted forms


def test_same_repo_full_indexed_sha_is_accepted() -> None:
    assert source(f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1-L42") == SourceTarget("src/widgetry/cli.py", (1, 42))
    assert source(f"{HUB}/blob/{SHA.upper()}/src/widgetry/cli.py") == SourceTarget("src/widgetry/cli.py")
    assert source(f"{HUB}/blob/{SHA}/src/widgetry/cli.py?plain=1#L3") == SourceTarget("src/widgetry/cli.py", (3, 3))


def test_relative_and_absolute_equivalents_share_a_target() -> None:
    pairs = [
        ("../../src/widgetry/cli.py#L1-L42", f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1-L42"),
        ("../../src/widgetry/cli.py#L1-L42", f"{LAB}/-/blob/{SHA}/src/widgetry/cli.py#L1-42"),
        ("../../src/widgetry/cli.py", f"{HUB}/blob/{SHA}/src/widgetry/cli.py"),
        ("../../src/widgetry/store", f"{HUB}/tree/{SHA}/src/widgetry/store"),
        ("../../src/widgetry/store", f"{LAB}/-/tree/{SHA}/src/widgetry/store/"),
        (
            "../../src/widgetry/cli.py#L7",
            f"HTTPS://CODE.example.test:443/ACME/Widgetry/blob/{SHA}/src/widgetry/cli.py#L7",
        ),
    ]
    for relative, absolute in pairs:
        assert source(relative) == source(absolute), (relative, absolute)


def test_github_and_gitlab_line_fragments_and_nested_namespaces() -> None:
    assert source(f"{HUB}/blob/{SHA}/a/b.py#L1-L42").lines == (1, 42)
    assert source(f"{LAB}/-/blob/{SHA}/a/b.py#L1-42").lines == (1, 42)
    assert source(f"{LAB}/-/blob/{SHA}/a/b.py#L9").lines == (9, 9)
    assert "unsupported fragment" in problem(f"{HUB}/blob/{SHA}/a/b.py#L1-42")  # GitLab style on GitHub
    assert "unsupported fragment" in problem(f"{LAB}/-/blob/{SHA}/a/b.py#L1-L42")  # GitHub style on GitLab
    deep = identities(["https://gitlab.example.test/a/b/c/d/project"])
    res = resolve(f"https://gitlab.example.test/a/b/c/d/project/-/blob/{SHA}/x.py#L2-3", deep)
    assert res.kind == "source" and res.target == SourceTarget("x.py", (2, 3))


def test_normalization_is_idempotent() -> None:
    hrefs = [
        "../../src/widgetry/cli.py#L1-L42",
        "../../src/./widgetry/../widgetry/cli.py#L5",
        f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1-L42",
        f"{LAB}/-/blob/{SHA}/src/widgetry/cli.py#L4-4",
        f"{HUB}/tree/{SHA}/src/widgetry/store",
    ]
    for href in hrefs:
        once = source(href)
        canonical = once.href(PAGE, WIKI)
        assert source(canonical) == once
        assert source(canonical).href(PAGE, WIKI) == canonical
        assert canonical.startswith("../../") and "://" not in canonical


# --------------------------------------------------------------------------- refused forms


@pytest.mark.parametrize("ref", ["main", "HEAD", "v1.0", SHA[:12], OTHER])
def test_branch_short_or_different_sha_is_rejected(ref: str) -> None:
    for href in (f"{HUB}/blob/{ref}/src/widgetry/cli.py#L1", f"{LAB}/-/blob/{ref}/src/widgetry/cli.py#L1"):
        assert "indexed commit" in problem(href)


def test_without_an_indexed_commit_a_source_url_cannot_be_checked() -> None:
    res = resolve(f"{HUB}/blob/{SHA}/src/widgetry/cli.py", commit=None)
    assert res.kind == "invalid" and "without an indexed commit" in (res.problem or "")


@pytest.mark.parametrize(
    "href",
    [
        "https://code.example.test/acme/other/blob/{sha}/src/x.py#L1",  # same host, another repository
        "https://code.example.test/someone/widgetry/blob/{sha}/src/x.py#L1-L2",
        "https://elsewhere.example.test/acme/widgetry/blob/{sha}/src/x.py#L1",  # unregistered host
        "https://gitlab.example.test/group/widgetry/-/blob/{sha}/x.py#L1-2",  # a sibling namespace
        "https://example.test/docs/page.html#L10",
        "mailto:someone@example.test",
    ],
)
def test_foreign_repository_links_stay_external_and_count_zero(href: str) -> None:
    href = href.format(sha=SHA)
    assert resolve(href).kind == "external"
    page = f"# T\n\nSee [x]({href}) and [y]({href}).\n"
    assert cited_files(page, PAGE, WIKI, repo_identities=IDS, commit=SHA) == []


def test_other_pages_of_the_same_repository_are_external() -> None:
    for href in (f"{HUB}/issues/3", f"{HUB}", f"{LAB}/-/merge_requests/9", f"{HUB}/pulls"):
        assert resolve(href).kind == "external", href


@pytest.mark.parametrize(
    "href, why",
    [
        (f"{HUB}/raw/{SHA}/src/widgetry/cli.py", "unsupported source URL (raw)"),
        (f"{HUB}/blame/{SHA}/src/widgetry/cli.py#L1", "unsupported source URL (blame)"),
        (f"{HUB}/-/blob/{SHA}/src/widgetry/cli.py", "GitLab-style path on a GitHub-style host"),
        (f"{LAB}/blob/{SHA}/src/widgetry/cli.py", "ambiguous GitLab source URL"),
        (f"{HUB}/blob/{SHA}", "no commit or path"),
        (f"{HUB}/blob/{SHA}/src/widgetry/cli.py?ref=main", "unsupported query"),
        (f"{HUB}/blob/{SHA}/src/widgetry/cli.py#readme", "unsupported fragment"),
        (f"{HUB}/tree/{SHA}/src/widgetry#L1-L2", "line range on a directory"),
        (f"{HUB}/issues/3#L10", "line fragment on a page that is not a file"),
        (f"{HUB}/blob/{SHA}/docs/wiki/2-engine.md", "link wiki pages relatively"),
    ],
)
def test_ambiguous_or_unsupported_source_urls_fail(href: str, why: str) -> None:
    assert why in problem(href)


def test_url_matching_two_registered_identities_is_ambiguous() -> None:
    nested = identities(["https://gitlab.example.test/group/proj", "https://gitlab.example.test/group/proj/sub"])
    res = resolve(f"https://gitlab.example.test/group/proj/sub/-/blob/{SHA}/x.py#L1", nested)
    assert res.kind == "invalid" and "ambiguous" in (res.problem or "")


@pytest.mark.parametrize(
    "href",
    [
        "../../src/%2e%2e/%2e%2e/outside.txt",
        "../../src/widgetry%2fcli.py#L1",
        "../../src/%2E%2E/widgetry/cli.py",
        "..%2f..%2fsrc/widgetry/cli.py",
        "../../src/widgetry\\cli.py",
        f"{HUB}/blob/{SHA}/src/%2e%2e/%2e%2e/outside.txt",
        f"{HUB}/blob/{SHA}/src/widgetry%2Fcli.py",
        f"{HUB}/blob/{SHA}/src/../../outside.txt",
        f"https://code.example.test/acme%2Fwidgetry/blob/{SHA}/src/widgetry/cli.py",
    ],
)
def test_encoded_traversal_is_rejected(href: str) -> None:
    assert resolve(href).kind == "invalid", href


def test_identities_are_explicit_and_validated() -> None:
    assert identities([None, "", "not a url", "ftp://code.example.test/a/b", HUB, HUB + ".git", HUB + "/"]) == (
        RepoIdentity("code.example.test", "acme/widgetry"),
    )
    with pytest.raises(ValueError):
        RepoIdentity.parse("https://user:pw@code.example.test/acme/widgetry")
    # Without a registered identity a web URL is never a source link (no network lookup, no guessing).
    assert resolve(f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1", ()).kind == "external"


# --------------------------------------------------------------------------- HTML and code


def test_actual_html_anchors_and_images_are_found_outside_code() -> None:
    page = (
        "# T\n\n"
        '<p>See <a href="../../src/widgetry/cli.py#L1-L2">cli</a>.</p>\n\n'
        'Inline <a href="https://example.test/">web</a> and <img src="../../assets/logo.png" alt="logo">.\n\n'
        '```html\n<a href="../../src/widgetry/engine.py#L1">in a fence</a>\n```\n\n'
        'And `<a href="../../src/widgetry/engine.py#L2">in a code span</a>` too.\n'
    )
    found = [(link.href, link.image, link.html) for link in find_links(page)]
    assert found == [
        ("../../src/widgetry/cli.py#L1-L2", False, True),
        ("https://example.test/", False, True),
        ("../../assets/logo.png", True, True),
    ]
    assert cited_files(page, PAGE, WIKI) == []  # raw HTML is never evidence


def test_fenced_and_inline_code_examples_are_ignored() -> None:
    page = (
        "# T\n\n"
        f"```md\n[cli](../../src/widgetry/cli.py#L1)\n[web]({HUB}/blob/main/src/widgetry/cli.py#L1)\n```\n\n"
        f"Write `[cli]({HUB}/blob/main/x.py)` or `../../%2e%2e/x` like this.\n\n"
        "    [indented](../../src/widgetry/engine.py#L1)\n"
    )
    assert find_links(page) == []


def test_cited_files_include_pinned_web_citations() -> None:
    page = (
        "# T\n\n"
        f"[a]({HUB}/blob/{SHA}/src/widgetry/cli.py#L1-L2) [b](../../src/widgetry/cli.py#L3) "
        f"[c]({LAB}/-/tree/{SHA}/src/widgetry/store) [d]({HUB}/blob/main/src/widgetry/engine.py#L1)\n"
    )
    cited = cited_files(page, PAGE, WIKI, repo_identities=IDS, commit=SHA)
    assert cited == ["src/widgetry/cli.py", "src/widgetry/store"]


def test_generation_rewrites_pinned_web_citations_to_the_canonical_relative_link(repo: Path) -> None:
    from repowiki.generate import relink, with_sources_block
    from repowiki.git import Snapshot

    snap = Snapshot.at(repo, "HEAD")
    pinned = f"{HUB}/blob/{snap.commit}/src/widgetry/cli.py#L2-L3"
    text = f"# T\n\n[cli.py:2-3]({pinned}) [web](https://example.test/) [main]({HUB}/blob/main/src/widgetry/cli.py)\n"
    out = relink(text, PAGE, WIKI, snap, set(), repo_identities=IDS)
    assert "[cli.py:2-3](../../src/widgetry/cli.py#L2-L3)" in out
    assert "(https://example.test/)" in out and f"({HUB}/blob/main/src/widgetry/cli.py)" in out  # left for validation
    assert relink(out, PAGE, WIKI, snap, set(), repo_identities=IDS) == out  # idempotent
    block = with_sources_block(f"# T\n\n[a]({pinned})\n", PAGE, WIKI, [], repo_identities=IDS, commit=snap.commit)
    assert "- [src/widgetry/cli.py](../../src/widgetry/cli.py)" in block


# --------------------------------------------------------------------------- fix round 1


@pytest.mark.parametrize("name", ["My File.py", "a?b.py", "a#b.py", "100%.py", "ünï(1).py", "x y/z?.py"])
def test_canonical_hrefs_encode_file_names_and_round_trip(name: str) -> None:
    from urllib.parse import quote

    url = f"{HUB}/blob/{SHA}/src/{quote(name)}#L1-L2"
    target = source(url)
    assert target == SourceTarget(f"src/{name}", (1, 2))
    canonical = target.href(PAGE, WIKI)
    assert source(canonical) == target and source(canonical).href(PAGE, WIKI) == canonical
    page = f"# T\n\n[x]({canonical})\n"
    (link,) = find_links(page)  # still a parsable Markdown destination
    assert cited_files(page, PAGE, WIKI) == [f"src/{name}"]


def test_canonicalize_links_rewrites_only_accepted_web_citations() -> None:
    from repowiki.links import canonicalize_links

    text = (
        "# T\n\n"
        f"[a]({HUB}/blob/{SHA}/src/My%20File.py#L1-L2) [b]({HUB}/blob/main/src/x.py) [c](https://example.test/)\n\n"
        f"Uses [ref].\n\n[ref]: {LAB}/-/blob/{SHA}/src/y.py#L3-4\n\n"
        f"```\n[code]({HUB}/blob/{SHA}/src/z.py)\n```\n"
    )
    out = canonicalize_links(text, PAGE, WIKI, repo_identities=IDS, commit=SHA)
    assert "[a](../../src/My%20File.py#L1-L2)" in out
    assert f"[b]({HUB}/blob/main/src/x.py)" in out and "[c](https://example.test/)" in out
    assert "[ref]: ../../src/y.py#L3-L4" in out
    assert f"[code]({HUB}/blob/{SHA}/src/z.py)" in out  # code is left alone
    assert canonicalize_links(out, PAGE, WIKI, repo_identities=IDS, commit=SHA) == out
    assert canonicalize_links(text, PAGE, WIKI, repo_identities=IDS, commit=OTHER) == text  # other commit: untouched


def test_malformed_external_url_is_a_diagnostic_not_a_crash() -> None:
    for href in (
        "https://example.test:abc/docs",
        "http://[::1/x",
        f"https://code.example.test:99999/acme/widgetry/blob/{SHA}/x",
    ):
        res = resolve(href)
        assert res.kind in ("invalid", "external"), href


# --------------------------------------------------------------------------- fix round 2


@pytest.mark.parametrize(
    "form",
    [
        "[`cli.py`]({url})",  # a code-formatted label
        '[**cli** `x`]({url} "title")',
        "<{url}>",  # an autolink
        "[ref]\n\n[ref]:\n  {url}\n",  # a reference definition spread over two lines
        "[ref][r2]\n\n[r2]: <{url}>\n",
        "[`cli.py`](<{url}>)",  # an inline destination in angle brackets is not an autolink
        "[cli](  <{url}> 'title')",
        "![diagram]({url})",
    ],
)
def test_canonicalize_links_handles_every_markdown_link_form(form: str) -> None:
    from repowiki.links import canonicalize_links

    url = f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1-L2"
    text = "# T\n\n" + form.format(url=url) + "\n\nKept `" + url + "` in code.\n"
    out = canonicalize_links(text, PAGE, WIKI, repo_identities=IDS, commit=SHA)
    links = [link for link in find_links(out)]
    assert [link.href for link in links] == ["../../src/widgetry/cli.py#L1-L2"], out
    assert out.count(url) == 1 and f"`{url}`" in out  # only the code span still holds the web URL
    assert canonicalize_links(out, PAGE, WIKI, repo_identities=IDS, commit=SHA) == out


def test_angle_bracket_inline_destination_keeps_its_label() -> None:
    from repowiki.links import canonicalize_links

    url = f"{HUB}/blob/{SHA}/src/widgetry/cli.py#L1"
    out = canonicalize_links(f"# T\n\n[x](<{url}>) and <{url}>\n", PAGE, WIKI, repo_identities=IDS, commit=SHA)
    rel = "../../src/widgetry/cli.py#L1"
    assert out == f"# T\n\n[x](<{rel}>) and [src/widgetry/cli.py:1]({rel})\n"
