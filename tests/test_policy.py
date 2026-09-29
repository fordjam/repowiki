"""Blocker (c): one secret policy for model context, citations, validation and site export."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import generate as gen
from repowiki.config import Settings, load_settings
from repowiki.fake import answer
from repowiki.git import Snapshot
from repowiki.links import cited_files
from repowiki.policy import Policy, is_secret
from repowiki.runners import FakeRunner
from repowiki.site import RepoEntry, build_site
from repowiki.survey import excerpts, survey
from repowiki.validate import validate

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
    return gen.generate(repo, settings, log=lambda _msg: None)


@pytest.mark.parametrize("path", SECRETS + ["src/.env.local", "home/.ssh/config", "x/id_rsa.pub", "a/.pypirc"])
def test_secret_patterns(path: str) -> None:
    assert is_secret(path)


@pytest.mark.parametrize("path", ["src/id_utils.py", "src/environment.py", "README.md", "docs/secrets-howto.rst"])
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
    assert CANARY not in excerpts(Snapshot.at(leaky), SECRETS, 60_000)
    fake = FakeRunner()
    run(leaky, fake)
    assert fake.calls and not any(CANARY in c or ".npmrc" in c for c in fake.calls)


def test_citations_to_secrets_are_removed_and_rejected(leaky: Path) -> None:
    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return answer(prompt)
        return "# P\n\nOK [cli](src/widgetry/cli.py#L1-L3). Leak [secret](../../.env#L1) and [npm](.npmrc#L1).\n"

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


def test_site_never_exports_or_links_secrets(leaky: Path, tmp_path: Path) -> None:
    run(leaky, FakeRunner())
    page = leaky / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "\nSee [config](../../.env#L1) and [key](../../deploy/id_ed25519).\n")
    out = tmp_path / "site"
    build_site([RepoEntry("W", leaky)], out, log=lambda _m: None)
    assert not list(out.rglob("*.env*")) and not list(out.rglob("*id_ed25519*"))
    for f in out.rglob("*.html"):
        assert CANARY not in f.read_text()
    html = (out / "w/1-overview.html").read_text()
    assert "_source/.env" not in html and "config" in html


def test_index_json_is_unchanged_by_the_policy(leaky: Path) -> None:
    run(leaky, FakeRunner())
    index = json.loads((leaky / "docs/wiki/index.json").read_text())
    assert set(index) == {"repo", "indexed_commit", "indexed_at", "pages"}
