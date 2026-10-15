"""R2: compulsory outbound filtering. Every production model call carries a sealed envelope from the gateway."""

from __future__ import annotations

import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import adopt_page, commit, register, write
from repowiki import generate as gen
from repowiki.approval import authorize
from repowiki.config import Settings
from repowiki.fake import answer
from repowiki.git import Snapshot
from repowiki.outbound import EXCLUDED_PATH, Envelope, OutboundError, Section, dispatch, outbound_policy, seal
from repowiki.policy import REDACTED, Policy, prompt_policy, redact
from repowiki.runners import ClaudeRunner, FakeRunner, RunnerError
from repowiki.update import update

CANARY = "SYNTHETIC_OUTBOUND_CANARY_7"
SECRET_FILES = {".envbackup": f"KEY={CANARY}\n", "secrets-prod.json": f'{{"value": "{CANARY}"}}\n'}


NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the run's instant is the caller's, never the clock


def settings_for(cheap: FakeRunner, strong: FakeRunner | None = None, **kw: object) -> Settings:
    s = Settings(cheap="fake", min_pages=2, **kw)  # type: ignore[arg-type]
    s.runners = lambda: (cheap, strong or cheap)  # type: ignore[method-assign]
    return s


def run(repo: Path, cheap: FakeRunner, strong: FakeRunner | None = None, **kw: object) -> gen.Result:
    return gen.generate(repo, settings_for(cheap, strong, **kw), now=NOW, log=lambda _m: None)


def prompts(*fakes: FakeRunner) -> list[str]:
    return [t.prompt for f in fakes for t in f.trace]


def assert_absent(texts: list[str], *needles: str) -> None:
    assert texts
    for needle in needles:
        assert not [t for t in texts if needle in t], needle


@pytest.fixture
def leaky(repo: Path) -> Path:
    for rel, text in SECRET_FILES.items():
        write(repo, rel, text)
    commit(repo, "synthetic secrets")
    return repo


def authorized(repo: Path) -> Settings:
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")
    return settings


# --------------------------------------------------------------------------- missing origin / policy


def test_missing_origin_or_policy_is_refused(repo: Path) -> None:
    settings = authorized(repo)
    with pytest.raises(OutboundError, match="origin"):
        seal(settings, "draft", [Section("mystery", "text")])
    with pytest.raises(OutboundError, match="origin"):
        seal(settings, "draft", ["plain text"])  # type: ignore[list-item]
    with pytest.raises(ValueError, match="authorization"):
        seal(Settings(cheap="fake", privacy={}), "draft", [Section("template", "text")])  # never authorized
    with pytest.raises(OutboundError, match="privacy policy"):
        seal(Settings(cheap="fake"), "draft", [Section("template", "text")])  # no policy at all
    with pytest.raises(OutboundError, match="stage"):
        seal(settings, "chat", [Section("template", "text")])
    with pytest.raises(OutboundError, match="gateway"):
        Envelope("draft", "forged prompt", ("template",), "identity")


def test_a_section_naming_an_excluded_path_is_refused(leaky: Path) -> None:
    settings = authorized(leaky)
    with pytest.raises(OutboundError, match="keeps out of prompts"):
        seal(settings, "draft", [Section("source", "x", ("secrets-prod.json",))])
    with pytest.raises(OutboundError, match="keeps out of prompts"):
        seal(settings, "draft", [Section("source", "x", (".repowiki.toml",))])


# --------------------------------------------------------------------------- hard defaults, zero blob reads


@pytest.mark.parametrize("path", [
    ".envbackup", ".ENV.local", "secrets-prod.json", "config/Secrets-prod.yaml", ".secrets-old",
    "app/user_session.py", "cache/state.db", "x/model.PKL", "a.sqlite3", "m.joblib", "dump.rdb", "web.cache",
    "data/users.csv", "DATA/x.py", "lib/databases/a.sql", "sessions/s1", "run/checkpoints/c.txt", "caches/x",
    "docs/secrets-howto.rst", "keys/a.pfx", "id_ed25519", "notes/team-transcript.html",
])  # fmt: skip
def test_r2_hard_defaults(path: str) -> None:
    assert Policy().secret(path)


def test_hard_defaults_win_over_an_allowlist() -> None:
    assert Policy(allowlist=("data/*", "*")).secret("data/x.csv")


def test_excluded_files_are_never_read(leaky: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[str] = []
    real = Snapshot._open

    def spy(self: Snapshot, path: str):  # type: ignore[no-untyped-def]
        opened.append(path)
        return real(self, path)

    monkeypatch.setattr(Snapshot, "_open", spy)
    fake = FakeRunner()
    assert run(leaky, fake).problems == []
    assert opened and not set(SECRET_FILES) & set(opened)
    assert_absent(prompts(fake), CANARY, ".envbackup", "secrets-prod.json")


# --------------------------------------------------------------------------- redaction of the complete prompt


@pytest.mark.parametrize("length", [1, 11, 12])
def test_quoted_secrets_of_any_length_are_redacted(length: int) -> None:
    value = "q" * length
    for line in (f'password = "{value}"', f"api_key: '{value}'", f'"secret": "{value}"', f'TOKEN="{value}"'):
        out = redact(line)
        assert value not in out and REDACTED in out and out.count("\n") == line.count("\n")
    assert redact("pw = get_password()\nkey = load_token(path)\n") == "pw = get_password()\nkey = load_token(path)\n"


def test_short_literals_never_reach_a_model(repo: Path) -> None:
    write(repo, "src/widgetry/settings.py",
          'password = "a"\napi_key = "bbbbbbbbbbb"\ntoken = "cccccccccccc"\nuser = get_password()\n')  # fmt: skip
    commit(repo, "settings")
    plan = {"pages": [{"title": "Overview", "sources": ["src/widgetry/settings.py"]}, {"title": "More"}]}
    fake = FakeRunner(respond=lambda p: json.dumps(plan) if p.startswith("TASK: plan") else answer(p))
    run(repo, fake)
    texts = prompts(fake)
    assert_absent(texts, '"a"', "bbbbbbbbbbb", "cccccccccccc")
    assert any("get_password()" in t for t in texts)


# --------------------------------------------------------------------------- title, path and planner-reply canaries


def canary_planner(prompt: str) -> str:
    if prompt.startswith("TASK: plan"):
        return json.dumps({"pages": [
            {"title": "Overview", "summary": f'Reads .envbackup; password = "{CANARY}"', "sources": ["README.md"]},
            {"title": "Notes on secrets-prod.json", "summary": "Loads ./secrets-prod.json.",
             "sources": ["secrets-prod.json", "src/widgetry/cli.py"]},
        ]})  # fmt: skip
    return answer(prompt)


def test_title_path_and_planner_reply_canaries_are_absent(leaky: Path) -> None:
    fake = FakeRunner(respond=canary_planner)
    result = run(leaky, fake)
    assert result.problems == []
    later = [t.prompt for t in fake.trace if t.stage != "plan"]
    assert_absent(later, CANARY, ".envbackup", "secrets-prod.json")
    assert any(EXCLUDED_PATH in t for t in later)  # the planner's reply named them; the gateway replaced them
    assert_absent([t.prompt for t in fake.trace if t.stage == "plan"], CANARY, ".envbackup", "secrets-prod.json")


def test_cached_planner_reply_canaries_are_absent(leaky: Path) -> None:
    first = FakeRunner(respond=canary_planner)
    gen.generate(leaky, settings_for(first), now=NOW, log=lambda _m: None, plan_only=True)
    second = FakeRunner(respond=canary_planner)  # same responder: the saved outline is reused
    logs: list[str] = []
    gen.generate(leaky, settings_for(second), now=NOW, log=logs.append)
    assert any("reusing the saved outline" in m for m in logs)
    assert not [t for t in second.trace if t.stage == "plan"]
    assert_absent(prompts(second), CANARY, ".envbackup", "secrets-prod.json")


# --------------------------------------------------------------------------- mixed sources


def test_unapproved_readme_and_instruction_canaries_are_absent(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "README.md", f"# Widgetry\n\nAsk {CANARY} for access.\n")
    write(repo, "AGENTS.md", f"Agent note {CANARY}\n")
    write(repo, "plans/next.md", f"Plan {CANARY}\n")
    write(repo, "reports/q1.md", f"Report {CANARY}\n")
    commit(repo, "prompt-only files")
    register(repo, monkeypatch, context_classes=["source", "previous_wiki"])
    fake = FakeRunner()
    run(repo, fake)
    assert_absent(prompts(fake), CANARY, "README.md", "AGENTS.md", "plans/next.md", "reports/q1.md")


def test_prompt_only_defaults_and_their_approval() -> None:
    base = Policy()
    closed = prompt_policy(base, [], ["*"], "docs/wiki")
    for path in ("README.md", "docs/readme.rst", "CLAUDE.md", "pkg/SKILL.md", "plans/a.md", "x/Reports/b.md",
                 "docs/wiki/1-overview.md"):
        assert closed.secret(path), path
    opened = prompt_policy(base, ["readme", "instructions", "previous_wiki"], ["*", "plans/*"], "docs/wiki")
    for path in ("README.md", "CLAUDE.md", "plans/a.md", "docs/wiki/1-overview.md"):
        assert not opened.secret(path), path
    assert opened.secret("reports/b.md") and opened.secret(".env")  # not approved / hard default


def test_previous_page_manifest_and_repair_error_canaries_are_absent(leaky: Path) -> None:
    write(leaky, "pyproject.toml", '[project]\nname = "widgetry"\n\n[tool.widgetry]\npassword = "m"\n')
    commit(leaky, "manifest")
    fake = FakeRunner()
    run(leaky, fake)
    page = leaky / "docs/wiki/1-overview.md"
    page.write_text(page.read_text().replace("\n<!--", '\nSee .envbackup and secret = "zz".\n\n<!--'))
    adopt_page(page)
    commit(leaky, "hand-edited wiki")
    write(leaky, "src/widgetry/cli.py", "def main():\n    return 7\n")
    write(leaky, "README.md", "# Widgetry\n\nSee [cli](src/widgetry/cli.py).\n")
    commit(leaky, "cli")

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: page"):  # a draft that cites an excluded file: validation names it
            return answer(prompt) + "\nLeak [backup](../../.envbackup#L1) and [prod](secrets-prod.json).\n"
        if prompt.startswith("TASK: repair"):  # the repair removes the leaking paragraph
            return "\n".join(line for line in answer(prompt).splitlines() if not line.startswith("Leak "))
        return answer(prompt)

    replay = FakeRunner(respond=respond)
    update(leaky, settings_for(replay), now=NOW, log=lambda _m: None)
    stages = {t.stage for t in replay.trace}
    assert "draft" in stages and "repair" in stages
    repair = [t.prompt for t in replay.trace if t.stage == "repair"]
    assert any("secret policy" in t and EXCLUDED_PATH in t for t in repair)
    assert any("## Previous version" in t for t in prompts(replay))
    assert_absent(prompts(fake, replay), CANARY, ".envbackup", "secrets-prod.json", '"zz"', 'password = "m"')


# --------------------------------------------------------------------------- traces, retries, alternate callers


def test_every_production_stage_is_traced_and_sealed(repo: Path) -> None:
    def respond(prompt: str) -> str:
        broken = "\nBroken [x](src/widgetry/nowhere.py#L1).\n"
        if prompt.startswith("TASK: page"):
            return answer(prompt) + broken
        if prompt.startswith("TASK: repair"):
            return answer(prompt).replace(broken.strip(), "")  # a repair removes the block (unlinking would fail)
        return answer(prompt)

    cheap, strong = FakeRunner(respond=respond), FakeRunner()
    run(repo, cheap, strong)
    entries = cheap.trace + strong.trace
    assert {t.stage for t in entries} == {"plan", "draft", "revise", "repair"}
    assert all(t.sealed and t.origins for t in entries)
    assert [t.prompt for t in cheap.trace] == cheap.calls and [t.prompt for t in strong.trace] == strong.calls
    assert {"template", "path", "readme"} <= {o for t in entries if t.stage == "plan" for o in t.origins}
    assert {"source", "planner_reply", "outline"} <= {o for t in entries if t.stage == "draft" for o in t.origins}
    assert "draft" in {o for t in entries if t.stage == "revise" for o in t.origins}
    assert "validation" in {o for t in entries if t.stage == "repair" for o in t.origins}


def test_retries_go_through_the_gateway(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gen, "_sleep", lambda _s: None)
    failures = iter([True, True])

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: page") and next(failures, False):
            raise RunnerError("transient")
        return answer(prompt)

    fake = FakeRunner(respond=respond)
    run(repo, fake, retries=2)
    drafts = [t for t in fake.trace if t.stage == "draft"]
    assert len(drafts) >= 3 and drafts[0].prompt == drafts[1].prompt == drafts[2].prompt
    assert all(t.sealed for t in fake.trace)


def fake_cli(tmp_path: Path) -> Path:
    exe = tmp_path / "fakecli"
    exe.write_text(f'#!/bin/sh\ntouch "{tmp_path}/ran"\necho hello\n')
    exe.chmod(0o755)
    return exe


def test_alternate_production_callers_cannot_bypass_the_gateway(repo: Path, tmp_path: Path) -> None:
    settings = authorized(repo)
    runner = ClaudeRunner(binary=str(fake_cli(tmp_path)))
    envelope = seal(settings, "draft", [Section("template", "TASK: page\n")])
    with pytest.raises(OutboundError, match="unsealed"):
        runner.run("TASK: page\nraw string")  # type: ignore[arg-type]
    with pytest.raises(OutboundError, match="not released"):
        runner.run(envelope)  # a sealed envelope, but not sent by the gateway
    with pytest.raises(OutboundError, match="sealed prompt envelopes"):
        gen.call(runner, "raw string", settings, lambda _m: None)  # type: ignore[arg-type]
    with pytest.raises(OutboundError, match="sealed prompt envelopes"):
        dispatch(runner, "raw string", settings)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unapproved runner"):
        dispatch(runner, envelope, settings)  # the gateway sends only to runners Settings.runners() built
    assert not (tmp_path / "ran").exists()
    other = Settings(cheap="fake", exclude=["*.md"])  # a different (narrower) authorization
    authorize(repo, other, "HEAD", "generate")
    with pytest.raises(OutboundError, match="different authorization"):
        dispatch(FakeRunner(), envelope, other)


def test_outbound_policy_is_used_for_context(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(repo, "plans/roadmap.md", f"{CANARY}\n")
    commit(repo, "plans")
    register(repo, monkeypatch)
    settings = authorized(repo)
    policy = outbound_policy(settings)
    assert policy.secret("plans/roadmap.md") and not settings.context_policy.secret("plans/roadmap.md")
    envelope = seal(settings, "draft", [Section("template", "see plans/roadmap.md and ./plans/roadmap.md.")])
    assert envelope.prompt == f"see {EXCLUDED_PATH} and ./{EXCLUDED_PATH}."


# --------------------------------------------------------------------------- round 2: reviewer's variants


def test_literals_containing_the_other_quote_are_redacted(repo: Path) -> None:
    lines = ['password = "don\'tshare"', "token = 'abc\"def'", 'secret = "a\\"b-tail"']
    for line in lines:
        assert redact(line).endswith(("\"[redacted by repowiki]\"", "'[redacted by repowiki]'")), line
    write(repo, "src/widgetry/creds.py", "\n".join(lines) + "\n")
    commit(repo, "creds")
    plan = {"pages": [{"title": "Overview", "sources": ["src/widgetry/creds.py"]}, {"title": "More"}]}
    fake = FakeRunner(respond=lambda p: json.dumps(plan) if p.startswith("TASK: plan") else answer(p))
    run(repo, fake)
    assert any("creds.py" in t for t in prompts(fake))
    assert_absent(prompts(fake), "don't", "tshare", 'abc"def', "b-tail")


VARIANT_FILES = ["secrets team.json", "secrets-équipe.json", "secrets-prod.json"]
VARIANT_REFS = (
    "# reads SECRETS-prod.json, ./secrets team.json, secrets%20team.json,\n"
    "# SECRETS-ÉQUIPE.JSON and secrets-équipe.json (decomposed)\n"
    "def load():\n    return 1\n"
)


def test_excluded_name_variants_never_reach_a_model(repo: Path) -> None:
    for name in VARIANT_FILES:
        write(repo, name, f"{CANARY}\n")
    write(repo, "src/widgetry/refs.py", VARIANT_REFS)
    commit(repo, "variants")

    def respond(prompt: str) -> str:
        if prompt.startswith("TASK: plan"):
            return json.dumps({"pages": [
                {"title": "Overview", "sources": ["src/widgetry/refs.py"]},
                {"title": "Secrets Team.json notes", "summary": "Uses SECRETS-Équipe.json.", "sources": []},
            ]})  # fmt: skip
        return answer(prompt)

    fake = FakeRunner(respond=respond)
    assert run(repo, fake).problems == []
    folded = [unicodedata.normalize("NFC", t).lower() for t in prompts(fake)]
    assert any("refs.py" in t for t in folded)
    assert_absent(folded, CANARY.lower(), "secrets team.json", "secrets%20team.json", "secrets-équipe.json",
                  "secrets-prod.json")
    assert any(EXCLUDED_PATH.lower() in t for t in folded)


def test_removing_the_gateway_filter_is_detected(leaky: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The canary tests have teeth: with redaction and name scrubbing switched off they would fail."""
    from repowiki import outbound

    monkeypatch.setattr(outbound, "redact", lambda text: text)
    monkeypatch.setattr(outbound, "scrub", lambda text, _excluded: text)
    fake = FakeRunner(respond=canary_planner)
    run(leaky, fake)
    later = [t.prompt for t in fake.trace if t.stage != "plan"]
    assert any("secrets-prod.json" in t for t in later) and any(CANARY in t for t in later)
