"""Blocker (c): one secret policy for model context, citations, validation and site export."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import adopt_page, commit, write
from repowiki import generate as gen
from repowiki.config import Settings, load_settings
from repowiki.fake import answer
from repowiki.generate import GenerateError
from repowiki.git import Snapshot
from repowiki.links import cited_files
from repowiki.policy import Policy, is_secret
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, SiteError, build_site
from repowiki.survey import excerpts, survey
from repowiki.update import update
from repowiki.validate import validate

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock

CANARY = "SYNTHETIC_SECRET_VALUE_42"
SECRETS = [
    ".env",
    ".env.production",
    "config/secrets.json",
    ".npmrc",
    "deploy/id_ed25519",
    "keys/server.pem",
    "aws/credentials",
    ".claude/projects/session.jsonl",
    "notes/agent-transcript.md",
    "transcripts/2026-01-01.txt",
]


@pytest.fixture
def leaky(repo: Path) -> Path:
    for rel in SECRETS:
        write(repo, rel, f"TOKEN={CANARY}\n")
    commit(repo, "oops")
    return repo


def run(repo: Path, fake: FakeRunner, **kw: object) -> gen.Result:
    settings = Settings(cheap="fake", min_pages=2, **kw)  # type: ignore[arg-type]
    settings.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    return gen.generate(repo, settings, now=NOW, log=lambda _msg: None)


@pytest.mark.parametrize("path", SECRETS + ["src/.env.local", "home/.ssh/config", "x/id_rsa.pub", "a/.pypirc"])
def test_secret_patterns(path: str) -> None:
    assert is_secret(path)


# R2 decided "secrets*" as a hard basename default, so docs/secrets-howto.rst moved to test_r2_hard_defaults.
@pytest.mark.parametrize("path", ["src/id_utils.py", "src/environment.py", "README.md", "docs/secret-sharing.rst"])
def test_ordinary_files_are_not_secret(path: str) -> None:
    assert not is_secret(path)


def test_extra_patterns_from_settings(tmp_path: Path) -> None:
    (tmp_path / ".repowiki.toml").write_text('exclude = ["internal/*", "*.sql"]\n')
    policy = load_settings(tmp_path).policy
    assert policy.secret("internal/plan.md") and policy.secret("db/dump.sql") and not policy.secret("src/a.py")
    assert Policy().secret("keys/a.pem")


def test_secrets_never_reach_a_model(leaky: Path) -> None:
    s = survey(Snapshot.at(leaky), "docs/wiki")
    assert not set(SECRETS) & set(s.files)
    assert CANARY not in excerpts(Snapshot.at(leaky), SECRETS, 60_000).text
    fake = FakeRunner()
    run(leaky, fake)
    assert fake.calls and not any(CANARY in c or ".npmrc" in c for c in fake.calls)


def test_citations_to_secrets_are_removed_and_rejected(leaky: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        ok = "# P\n\nOK [cli](src/widgetry/cli.py#L1-L3).\n"
        if prompt.startswith("TASK: repair"):
            return ok  # the repair removes the paragraph that cites secrets
        return ok + "\nLeak [secret](../../.env#L1) and [npm](.npmrc#L1).\n"

    result = run(leaky, FakeRunner(respond=respond))
    assert result.problems == []
    for page in result.written:
        text = (leaky / "docs/wiki" / page).read_text()
        assert not [f for f in cited_files(text, page, "docs/wiki") if is_secret(f)]
        assert "(../../.env" not in text and ".npmrc#" not in text
    assert any("removed" in w for w in result.warnings)


def test_validation_rejects_links_to_secrets(leaky: Path) -> None:
    run(leaky, FakeRunner())
    page = leaky / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "\nSee [config](../../.env#L1).\n")
    msgs = [p.message for p in validate(leaky) if p.level == "error"]
    assert any("secret policy" in m for m in msgs)


def _site_files(out: Path) -> dict[str, bytes]:
    return {str(f.relative_to(out)): f.read_bytes() for f in sorted(out.rglob("*")) if f.is_file()}


def _assert_no_secret_exported_or_linked(out: Path) -> None:
    files = _site_files(out)
    assert not [name for name in files if is_secret(name.removesuffix(".html"))]
    for name, data in files.items():
        text = data.decode("utf-8", "replace")
        assert CANARY not in text, name
        assert "_source/.env" not in text and "id_ed25519" not in text, name


def test_site_never_exports_or_links_secrets(leaky: Path, tmp_path: Path) -> None:
    """Privacy-first: a wiki that links secret files is never rendered, linked or exported.

    Validation is mandatory before rendering (R6), so such a wiki fails the whole build
    instead of being rendered with the link text kept; the last good site stays byte-for-byte,
    and even with source export on, no secret file or its contents reaches any output.
    """
    run(leaky, FakeRunner())
    entry = RepoEntry("W", leaky, export_sources=True)
    out = tmp_path / "site"
    build_site([entry], out, log=lambda _m: None, now=NOW)
    good = _site_files(out)
    assert "w/1-overview.html" in good and any(name.startswith("w/_source/") for name in good)
    _assert_no_secret_exported_or_linked(out)

    page = leaky / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "\nSee [config](../../.env#L1) and [key](../../deploy/id_ed25519).\n")
    with pytest.raises(SiteError, match="secret policy"):
        build_site([entry], out, log=lambda _m: None, now=NOW)
    assert _site_files(out) == good  # zero good-output replacements
    _assert_no_secret_exported_or_linked(out)
    assert not list(tmp_path.glob(".site-*"))  # no partial build left beside it
    fresh = tmp_path / "fresh"
    with pytest.raises(SiteError, match="secret policy"):
        build_site([entry], fresh, log=lambda _m: None, now=NOW)
    assert not fresh.exists()


def test_index_json_is_unchanged_by_the_policy(leaky: Path) -> None:
    run(leaky, FakeRunner())
    index = json.loads((leaky / "docs/wiki/index.json").read_text())
    assert set(index) == {"repo", "indexed_commit", "indexed_at", "pages"}


# --------------------------------------------------------------------------- wiki pages as inputs (round 2)

TOKEN = "gh" + "p_" + "Q" * 36  # built at runtime: a credential-shaped canary


def with_wiki(repo: Path) -> None:
    run(repo, FakeRunner())
    commit(repo, "wiki")


def add_transcript_page(repo: Path) -> None:
    """The reviewer's case: a secret-pattern page listed in a valid index."""
    wiki = repo / "docs/wiki"
    (wiki / "agent-transcript.md").write_text(f"# Transcript\n\nuser: my key is {CANARY}\n")
    index = json.loads((wiki / "index.json").read_text())
    index["pages"].append({"file": "agent-transcript.md", "title": "Transcript", "parent": None})
    (wiki / "index.json").write_text(json.dumps(index))


def test_site_refuses_a_secret_pattern_page(repo: Path, tmp_path: Path) -> None:
    with_wiki(repo)
    add_transcript_page(repo)
    with pytest.raises(SiteError, match="secret policy"):
        build_site([RepoEntry("W", repo)], tmp_path / "site", log=lambda _m: None, now=NOW)
    assert not (tmp_path / "site").exists()


def test_update_never_sends_a_secret_pattern_page(repo: Path) -> None:
    with_wiki(repo)
    add_transcript_page(repo)
    commit(repo, "transcript")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 5\n")
    commit(repo, "cli")
    fake = FakeRunner()
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    with pytest.raises(GenerateError, match="secret policy"):
        update(repo, settings, now=NOW, log=lambda _m: None)
    assert not any(CANARY in c for c in fake.calls)


def test_validate_flags_a_secret_pattern_page(repo: Path) -> None:
    with_wiki(repo)
    add_transcript_page(repo)
    assert any("secret policy" in p.message and p.level == "error" for p in validate(repo))


def test_planned_titles_never_produce_secret_page_names(repo: Path) -> None:
    plan = {"pages": [{"title": "Overview"}, {"title": "Agent transcript"}, {"title": "Secrets"}]}

    def respond(prompt: str) -> str:
        return json.dumps(plan) if prompt.startswith("TASK: plan") else answer(prompt)

    result = run(repo, FakeRunner(respond=respond))
    assert [p.file for p in result.pages] == ["1-overview.md", "2-page.md", "3-secrets.md"]
    assert result.problems == []


def test_credential_strings_are_redacted_in_prompts_pages_and_site(repo: Path, tmp_path: Path) -> None:
    write(repo, "src/widgetry/settings.py", f'API_TOKEN = "{TOKEN}"\nDEBUG = True\n')
    commit(repo, "settings")
    snap = Snapshot.at(repo)
    ex = excerpts(snap, ["src/widgetry/settings.py"], 60_000)
    assert (
        TOKEN not in ex.text and "[redacted by repowiki]" in ex.text and ex.shown["src/widgetry/settings.py"] == (2, 2)
    )

    with_wiki(repo)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text().replace("\n<!--", f"\nExample header: Bearer {TOKEN}\n\n<!--"))
    adopt_page(page)
    commit(repo, "hand edit")
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo)], out, log=lambda _m: None, now=NOW)
    assert all(TOKEN not in f.read_text() for f in out.rglob("*.html"))

    write(repo, "src/widgetry/cli.py", "def main():\n    return 6\n")
    commit(repo, "cli")
    fake = FakeRunner()
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    update(repo, settings, now=NOW, log=lambda _m: None)
    assert fake.calls and not any(TOKEN in c for c in fake.calls)
