"""Blocker (e): only a wiki that passes validation is ever written, and failures leave the old one intact."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import generate as gen
from repowiki.config import Settings
from repowiki.fake import answer
from repowiki.links import find_links, unlink
from repowiki.runners import FakeRunner, RunnerError
from repowiki.update import update
from repowiki.validate import validate


def settings_for(fake: FakeRunner) -> Settings:
    s = Settings(cheap="fake", min_pages=2)
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return s


def run(repo: Path, fake: FakeRunner) -> gen.Result:
    return gen.generate(repo, settings_for(fake), log=lambda _m: None)


def snapshot(repo: Path) -> dict[str, str]:
    return {p.name: p.read_text() for p in sorted((repo / "docs/wiki").iterdir())}


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
