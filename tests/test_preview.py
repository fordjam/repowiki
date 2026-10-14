"""Improvements: --preview shows exactly what goes to which provider; budgets and estimates stop runs early."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import adopt_page, commit, write
from repowiki import generate as gen
from repowiki.cli import main
from repowiki.config import Settings
from repowiki.generate import BudgetError, Meter
from repowiki.runners import FakeRunner
from repowiki.update import update


def settings_for(cheap: FakeRunner, strong: FakeRunner | None = None, **kw: object) -> Settings:
    s = Settings(**{"cheap": "fake", "min_pages": 2, **kw})  # type: ignore[arg-type]
    s.runners = lambda: (cheap, strong or cheap)  # type: ignore[method-assign]
    # R3: --preview and --dry-run describe runners instead of building them; describe the same fakes.
    s.describe_runners = lambda: (cheap, strong or cheap)  # type: ignore[method-assign]
    return s


def test_preview_sends_nothing_and_lists_the_plan_payload(repo: Path) -> None:
    write(repo, ".env", "TOKEN=SYNTHETIC\n")
    commit(repo, "env")
    fake = FakeRunner()
    text = gen.preview(repo, settings_for(fake))
    assert fake.calls == [] and not (repo / "docs/wiki").exists()
    assert "Nothing has been sent" in text and "plan -> fake" in text
    assert re.search(r"README\.md\s+\d+ bytes\s+text, first", text)
    assert "(file list)" in text and "no file contents" in text
    assert "outline is not known yet" in text
    assert "Withheld by the secret policy (never sent): 1 file(s)\n  .env" in text and "SYNTHETIC" not in text


def test_preview_matches_what_the_run_actually_sends(repo: Path) -> None:
    cheap, strong = FakeRunner("cheap"), FakeRunner("strong")
    settings = settings_for(cheap, strong)
    gen.generate(repo, settings, log=lambda _m: None, plan_only=True)
    assert len(cheap.calls) == 1  # --plan-only makes exactly one call
    text = gen.preview(repo, settings)
    assert "plan: reused from a checkpoint" in text
    previewed = set(re.findall(r"^\s+(\S+)\s+[\d,]+ bytes\s+lines 1-(\d+) of (\d+)$", text, re.M))
    assert re.search(r"final pass \S+ -> fake:strong", text) and "only if the page fails validation" in text

    cheap.calls.clear()
    gen.generate(repo, settings, log=lambda _m: None)
    sent = {
        (m.group("path"), m.group("shown"), m.group("total"))
        for call in cheap.calls
        for m in re.finditer(r"^=== (?P<path>.+?) \(lines 1-(?P<shown>\d+) of (?P<total>\d+)\) ===$", call, re.M)
    }
    assert previewed == sent and sent


def test_budget_stops_before_any_page_is_drafted(repo: Path) -> None:
    fake = FakeRunner()
    with pytest.raises(gen.GenerateError, match="exceeds the budget"):
        gen.generate(repo, settings_for(fake, budget_tokens=500), log=lambda _m: None)
    assert [c.split("\n", 1)[0] for c in fake.calls] == ["TASK: plan"]
    assert not (repo / "docs/wiki").exists()
    fake.calls.clear()
    logged: list[str] = []
    result = gen.generate(repo, settings_for(fake, budget_tokens=200_000), log=logged.append)
    assert result.problems == [] and not any(c.startswith("TASK: plan") for c in fake.calls)
    assert any("estimate for the pages:" in m and "prompt tokens" in m for m in logged)


def test_meter_is_a_hard_limit() -> None:
    meter = Meter(budget_tokens=10)
    meter.charge("x" * 40)
    with pytest.raises(BudgetError, match="token budget reached"):
        meter.charge("y")
    assert meter.calls == 1 and meter.tokens == 10


def test_cli_preview_and_budget(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2", "--preview"]) == 0
    assert "Nothing has been sent" in capsys.readouterr().out
    assert not (repo / "docs/wiki").exists()
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2", "--budget", "100"]) == 2
    assert "budget" in capsys.readouterr().err


def test_update_dry_run_shows_what_would_be_sent(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 3\n")
    commit(repo, "cli")
    fake.calls.clear()
    logged: list[str] = []
    update(repo, settings_for(fake), log=logged.append, dry_run=True)
    out = "\n".join(logged)
    assert fake.calls == []
    assert "src/widgetry/cli.py" in out and "lines 1-2 of 2" in out and "previous version of the page" in out


# --------------------------------------------------------------------------- R3: exact, inert, staged preview

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

from conftest import register  # noqa: E402
from repowiki import runners, safety  # noqa: E402
from repowiki.approval import authorize, receipt_path  # noqa: E402
from repowiki.generate import StaleEnvelopeError  # noqa: E402
from repowiki.git import Snapshot  # noqa: E402
from repowiki.outbound import outbound_policy  # noqa: E402
from repowiki.survey import survey  # noqa: E402

DIGEST_RE = re.compile(r"^Preview digest: ([0-9a-f]{64})$", re.M)
STAGE_RE = re.compile(
    r"^  (?P<step>plan|draft)(?: (?P<page>\S+))? -> \S+: (?P<bytes>[\d,]+) bytes, sha256 (?P<sha>[0-9a-f]{64})", re.M
)


def digest_of(text: str) -> str:
    m = DIGEST_RE.search(text)
    assert m, text
    return m.group(1)


def stages_of(text: str) -> dict[tuple[str, str | None], tuple[int, str]]:
    return {(m["step"], m["page"]): (int(m["bytes"].replace(",", "")), m["sha"]) for m in STAGE_RE.finditer(text)}


def tree_state(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under the clone, .git included: size, mtime and content."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            out[str(path.relative_to(root))] = (info.st_size, info.st_mtime_ns, path.read_bytes())
    return out


def test_r3_unknown_outline_previews_the_exact_plan_envelope(repo: Path) -> None:
    fake = FakeRunner()
    settings = settings_for(fake)
    text = gen.preview(repo, settings)
    stages = stages_of(text)
    assert list(stages) == [("plan", None)] and "outline is not known yet" in text
    assert "  draft (every page) -> fake: deferred, size unknown" in text
    assert "deferred: the outline, and so every page's prompt, is not known until the plan call answers" in text
    check = Settings(cheap="fake", min_pages=2)
    authorize(repo, check, "HEAD", "generate")
    snap = Snapshot.at(repo)
    envelope = gen.plan_request(snap, survey(snap, check.wiki_dir, policy=outbound_policy(check)), check, "widgetry")
    data = envelope.prompt.encode()
    assert stages[("plan", None)] == (len(data), hashlib.sha256(data).hexdigest())
    assert "      origins:\n        template\n        readme: README.md\n        path: " in text
    assert "src/widgetry/cli.py" in text.split("        path: ", 1)[1].split("\n", 1)[0]  # complete manifest
    gen.generate(repo, settings, log=lambda _m: None, expect_preview=digest_of(text))
    sent = fake.trace[0]
    assert sent.stage == "plan" and hashlib.sha256(sent.prompt.encode()).hexdigest() == stages[("plan", None)][1]


def test_r3_known_outline_previews_exact_drafts_and_defers_the_rest(repo: Path) -> None:
    cheap, strong = FakeRunner("cheap"), FakeRunner("strong")
    settings = settings_for(cheap, strong)
    gen.generate(repo, settings, log=lambda _m: None, plan_only=True)
    text = gen.preview(repo, settings)
    drafts = {page: v for (step, page), v in stages_of(text).items() if step == "draft"}
    assert drafts and "plan: reused from a checkpoint" in text
    assert "final pass" in text and "deferred: its prompt contains the draft reply" in text
    assert "deferred: built only if the finished page fails validation" in text
    cheap.trace.clear()
    gen.generate(repo, settings, log=lambda _m: None, expect_preview=digest_of(text))
    sent = {t.prompt for t in cheap.trace if t.stage == "draft"}
    assert {hashlib.sha256(p.encode()).hexdigest() for p in sent} == {sha for _, sha in drafts.values()}
    assert {len(p.encode()) for p in sent} == {n for n, _ in drafts.values()}


def test_r3_preview_counts_utf8_bytes(repo: Path) -> None:
    readme = "# Widgetry\n\nCaf\u00e9 widgets.\n"
    write(repo, "README.md", readme)
    commit(repo, "accent")
    text = gen.preview(repo, settings_for(FakeRunner()))
    size, _ = stages_of(text)[("plan", None)]
    check = Settings(cheap="fake", min_pages=2)
    authorize(repo, check, "HEAD", "generate")
    snap = Snapshot.at(repo)
    surveyed = survey(snap, check.wiki_dir, policy=outbound_policy(check))
    prompt = gen.plan_request(snap, surveyed, check, "widgetry").prompt
    assert prompt.count("é") == 1 and size == len(prompt) + 1  # é is two UTF-8 bytes
    assert len(readme.encode()) == len(readme) + 1
    assert re.search(rf"README\.md\s+{len(readme.encode())} bytes\s+text, first {len(readme)} characters", text)


def test_r3_repeated_preview_is_identical_and_writes_nothing(repo: Path) -> None:
    gen.generate(repo, settings_for(FakeRunner()), log=lambda _m: None, plan_only=True)
    before = tree_state(repo)
    first = gen.preview(repo, settings_for(FakeRunner()))
    second = gen.preview(repo, settings_for(FakeRunner()))
    assert first == second and tree_state(repo) == before


def test_r3_ollama_preview_makes_no_provider_calls(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = "ollama:qwen3:8b"
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["models"].append(spec)
    receipt["endpoints"][spec] = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    path.write_text(json.dumps(receipt))
    contacted: list[str] = []
    monkeypatch.setattr(runners, "_ollama_show", lambda base, model, timeout=10.0: contacted.append(model) or {})
    monkeypatch.setattr(runners.OllamaRunner, "run", lambda self, envelope: contacted.append("run") or "")
    text = gen.preview(repo, Settings(cheap=spec, min_pages=2))
    assert "plan -> ollama:qwen3:8b:" in text and contacted == []
    runners.make_runner(spec)  # verification (what a real run does) does contact the server
    assert contacted == [spec.split(":", 1)[1]]


def test_r3_changed_inputs_reject_the_preview(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRunner()
    settings = settings_for(fake)
    gen.generate(repo, settings, log=lambda _m: None, plan_only=True)
    fake.trace.clear()
    digest = digest_of(gen.preview(repo, settings))

    write(repo, "src/widgetry/engine.py", "def build(args):\n    return 0\n")  # source
    commit(repo, "source")
    with pytest.raises(StaleEnvelopeError, match="nothing was sent"):
        gen.generate(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)

    assert fake.trace == []
    gen.generate(repo, settings_for(fake), log=lambda _m: None, plan_only=True)  # outline for this commit
    fake.trace.clear()
    digest = digest_of(gen.preview(repo, settings_for(fake)))  # checkpoint bytes
    plan_files = list((repo / ".git/repowiki/checkpoints").glob("plan-*"))
    assert plan_files
    for f in plan_files:
        f.write_text(json.dumps({"pages": [{"title": "Overview"}, {"title": "Other"}]}))
    with pytest.raises(StaleEnvelopeError):
        gen.generate(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)

    digest = digest_of(gen.preview(repo, settings_for(fake)))  # configuration
    register(repo, monkeypatch, context_classes=["source", "previous_wiki"])
    with pytest.raises(StaleEnvelopeError):
        gen.generate(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)
    assert fake.trace == []  # every rejection happened before anything was sent


def test_r3_cli_preview_digest_binds_the_run(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    args = ["generate", str(repo), "--model", "fake", "--min-pages", "2"]
    assert main([*args, "--expect-preview", "0" * 64]) == 2
    assert "differ from the preview" in capsys.readouterr().err and not (repo / "docs/wiki").exists()
    assert main([*args, "--preview"]) == 0
    digest = digest_of(capsys.readouterr().out)
    assert main([*args, "--expect-preview", digest]) == 0
    assert (repo / "docs/wiki/index.json").exists()


def test_r3_update_dry_run_binds_and_rejects_changed_previous_pages(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 3\n")
    commit(repo, "cli")
    logged: list[str] = []
    update(repo, settings_for(fake), log=logged.append, dry_run=True)
    digest = digest_of("\n".join(logged))
    stale = [p for p in (repo / "docs/wiki").glob("*.md") if "cli.py" in p.read_text()]
    stale[0].write_text(stale[0].read_text().replace("\n<!--", "\nA hand-made line.\n\n<!--"))
    fake.trace.clear()
    with pytest.raises(StaleEnvelopeError, match="nothing was sent"):
        update(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)
    assert fake.trace == []
    adopt_page(stale[0])
    logged.clear()
    update(repo, settings_for(fake), log=logged.append, dry_run=True)
    digest = digest_of("\n".join(logged))
    expected = stages_of("\n".join(logged))
    update(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)
    sent = {hashlib.sha256(t.prompt.encode()).hexdigest() for t in fake.trace if t.stage == "draft"}
    assert sent == {sha for (step, _), (_, sha) in expected.items() if step == "draft"}


def test_r3_update_dry_run_never_recovers(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 4\n")
    commit(repo, "cli")
    calls = [0]
    real = safety.os.replace

    def replace(src: object, dst: object) -> None:  # the publish "dies" on its third rename in the wiki
        if "docs/wiki" in str(dst):
            calls[0] += 1
            if calls[0] == 3:
                raise OSError("killed")
        real(src, dst)  # type: ignore[arg-type]

    with monkeypatch.context() as m:
        m.setattr(safety.os, "replace", replace)
        m.setattr(safety, "_rollback", lambda wiki, journal: None)
        with pytest.raises(OSError):
            update(repo, settings_for(fake), log=lambda _m: None)
    before = tree_state(repo)
    from repowiki.update import pending_publish

    assert pending_publish(repo / "docs/wiki")  # an interrupted publish awaits recovery
    with pytest.raises(gen.GenerateError, match="interrupted publish is pending"):
        update(repo, settings_for(fake), log=lambda _m: None, dry_run=True)
    assert tree_state(repo) == before  # zero recovery mutations


def page_checkpoints(repo: Path) -> list[Path]:
    return sorted((repo / ".git/repowiki/checkpoints").glob("page-*"))


@pytest.mark.parametrize("change", ["still valid", "invalid"])
def test_r3_changed_page_checkpoint_bytes_reject_the_preview(repo: Path, change: str) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    text = gen.preview(repo, settings_for(fake))
    assert "draft 1-overview.md: reused from a checkpoint, nothing sent (checkpoint sha256 " in text
    digest = digest_of(text)
    target = page_checkpoints(repo)[0]
    if change == "still valid":
        target.write_text(target.read_text().replace("\n<!--", "\nEdited prose.\n\n<!--"))
    else:
        target.write_text("# Broken\n")
    before = {p.name: p.read_text() for p in (repo / "docs/wiki").iterdir()}
    fake.trace.clear()
    with pytest.raises(StaleEnvelopeError, match="nothing was sent"):
        gen.generate(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest)
    assert fake.trace == [] and {p.name: p.read_text() for p in (repo / "docs/wiki").iterdir()} == before


def test_r3_invalid_page_checkpoint_is_previewed_as_a_call(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    for path in page_checkpoints(repo):
        path.write_text("# Broken\n")  # fails validation: the run would draft every page again
    text = gen.preview(repo, settings_for(fake))
    drafts = {page: sha for (step, page), (_, sha) in stages_of(text).items() if step == "draft"}
    assert drafts and "draft 1-overview.md: reused" not in text
    fake.trace.clear()
    gen.generate(repo, settings_for(fake), log=lambda _m: None, expect_preview=digest_of(text))
    sent = {hashlib.sha256(t.prompt.encode()).hexdigest() for t in fake.trace if t.stage == "draft"}
    assert sent == set(drafts.values())


def test_r3_cli_update_dry_run_digest_binds_the_update(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2"]) == 0
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 5\n")
    commit(repo, "cli")
    capsys.readouterr()
    args = ["update", str(repo), "--model", "fake"]
    assert main([*args, "--expect-preview", "0" * 64]) == 2
    assert "differ from the dry run" in capsys.readouterr().err
    assert main([*args, "--dry-run"]) == 0
    digest = digest_of(capsys.readouterr().out)
    assert main([*args, "--expect-preview", digest]) == 0
    assert "re-wrote" in capsys.readouterr().out


# --------------------------------------------------------------------------- effort presets and honest estimates

from repowiki.config import load_settings  # noqa: E402

PRICE_RE = re.compile(r"[$€£¥]|\b(USD|EUR|GBP|cents?|dollars?)\b|\bcosts?\b|\bACUs?\b", re.I)


def set_limits(repo: Path, **limits: object) -> None:
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["limits"].update(limits)
    path.write_text(json.dumps(receipt))


def approve_model(repo: Path, spec: str) -> None:
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["models"].append(spec)
    receipt["endpoints"][spec] = "builtin"
    path.write_text(json.dumps(receipt))


@pytest.mark.parametrize("effort, limit, ceiling", [("standard", "budget_tokens", 100_000),
                                                    ("deep", "max_pages", 20),
                                                    ("deep", "context_chars", 100_000)])  # fmt: skip
def test_preset_cannot_widen_the_approval_receipt(repo: Path, effort: str, limit: str, ceiling: int) -> None:
    approve_model(repo, "fake:strong")
    set_limits(repo, **{limit: ceiling})
    settings = load_settings(repo, {"effort": effort, "min_pages": 2, "strong": "fake:strong"})
    assert getattr(settings, limit) > ceiling
    with pytest.raises(ValueError, match="exceeds approved limit: " + limit):
        gen.preview(repo, settings)
    assert not (repo / "docs/wiki").exists()


def test_explicit_settings_tighten_the_approved_limits(repo: Path) -> None:
    set_limits(repo, budget_tokens=100_000, max_pages=10)
    settings = load_settings(repo, {"effort": "standard", "min_pages": 2, "budget_tokens": 90_000, "max_pages": 9})
    text = gen.preview(repo, settings)
    assert "  run cap: 90,000 estimated input tokens (checked before every call)" in text
    assert "  pages: 2 to 9" in text and settings.budget_tokens == 90_000


def test_missing_approved_strong_model_fails_deep(repo: Path) -> None:
    with pytest.raises(ValueError, match="deep preset needs an approved strong model"):
        load_settings(repo, {"effort": "deep", "min_pages": 2})  # the policy names one model only
    settings = load_settings(repo, {"effort": "deep", "min_pages": 2, "strong": "fake:strong"})
    with pytest.raises(ValueError, match="unapproved model"):
        gen.preview(repo, settings)  # configured, but not in the approval receipt
    approve_model(repo, "fake:strong")
    assert "final pass fake:strong" in gen.preview(repo, settings)


def test_preview_reports_effective_settings_and_labelled_estimates(repo: Path) -> None:
    settings = load_settings(repo, {"effort": "draft", "min_pages": 2})
    text = gen.preview(repo, settings)
    assert "Effective settings:\n  effort: draft (presets v1; sets limits only, not a quality guarantee)\n" in text
    assert "  pages: 2 to 12\n  source context: at most 32,768 characters and 32,768 UTF-8 bytes per page " \
        "prompt\n" in text
    assert "  run cap: 50,000 estimated input tokens (checked before every call)\n" in text
    assert "  models: drafting fake; final pass none (one pass)\n" in text
    plan_bytes, _ = stages_of(text)[("plan", None)]
    m = re.search(r"^  ready stages: 1 call\(s\), ([\d,]+) UTF-8 bytes \(exact\), ~([\d,]+) estimated input tokens$",
                  text, re.M)  # fmt: skip
    assert m and int(m[1].replace(",", "")) == plan_bytes
    check = load_settings(repo, {"effort": "draft", "min_pages": 2})
    authorize(repo, check, "HEAD", "generate")
    snap = Snapshot.at(repo)
    prompt = gen.plan_request(snap, survey(snap, check.wiki_dir, policy=outbound_policy(check)), check, "widgetry")
    assert int(m[2].replace(",", "")) == (len(prompt.prompt) + 3) // 4  # characters / 4, labelled as an estimate
    assert "number and size are unknown until the outline exists" in text
    assert "  bounded by the run cap: 50,000 estimated input tokens for the whole run" in text
    assert "tokens are estimates (about 4 characters per token), not a provider's count; no price is estimated" in text
    assert not PRICE_RE.search(text), PRICE_RE.search(text)


def test_deferred_estimates_are_labelled_guesses_bounded_only_by_the_run_cap(repo: Path) -> None:
    cheap, strong = FakeRunner("cheap"), FakeRunner("strong")
    settings = settings_for(cheap, strong, budget_tokens=200_000)
    gen.generate(repo, settings, log=lambda _m: None, plan_only=True)
    text = gen.preview(repo, settings)
    pages = len([1 for (step, _p) in stages_of(text) if step == "draft"])
    sizes = [n for (step, _p), (n, _s) in stages_of(text).items() if step == "draft"]
    assert pages >= 2
    assert re.search(rf"^  ready stages: {pages} call\(s\), {sum(sizes):,} UTF-8 bytes \(exact\), ", text, re.M)
    assert re.search(rf"^  deferred stages: {pages} call\(s\), ~[\d,]+ estimated input tokens, plus up to {pages} "
                     r"repair call\(s\) only if needed \(a guess assuming a typical 8,000-character draft, not a "
                     r"bound\)$", text, re.M)  # fmt: skip
    assert "  bounded by the run cap: 200,000 estimated input tokens for the whole run" in text
    assert not PRICE_RE.search(text)


def test_estimate_without_a_run_cap_says_deferred_stages_are_unbounded() -> None:
    from repowiki.estimate import Estimate, PlannedCall

    est = Estimate([PlannedCall("final pass", "fake", "a.md", [], 9_000, deferred="needs the draft")])
    lines = est.totals(None)
    assert "  no run cap is set, so the deferred stages are not bounded" in lines
    assert lines[0] == "  ready stages: 0 call(s), 0 UTF-8 bytes (exact), ~0 estimated input tokens"
    assert not any(PRICE_RE.search(line) for line in est.render([]).splitlines())


def test_update_dry_run_reports_effective_settings(repo: Path) -> None:
    fake = FakeRunner()
    gen.generate(repo, settings_for(fake), log=lambda _m: None)
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 3\n")
    commit(repo, "cli")
    settings = load_settings(repo, {"effort": "draft", "min_pages": 2})
    settings.describe_runners = lambda: (fake, fake)  # type: ignore[method-assign]
    logged: list[str] = []
    update(repo, settings, log=logged.append, dry_run=True)
    out = "\n".join(logged)
    assert "Effective settings:\n  effort: draft (presets v1" in out
    assert "  run cap: 50,000 estimated input tokens" in out and "UTF-8 bytes (exact)" in out
    assert not PRICE_RE.search(out)


@pytest.mark.parametrize("effort, limit", [("draft", 32 * 1024), ("standard", 64 * 1024), ("deep", 128 * 1024)])
def test_preset_context_ceiling_is_utf8_bytes_for_multibyte_source(repo: Path, effort: str, limit: int) -> None:
    from repowiki.prompts import PlannedPage

    line = "# " + "字" * 40 + "\n"  # 42 characters, 122 UTF-8 bytes
    write(repo, "src/widgetry/wide.py", line * 3_000)
    commit(repo, "wide")
    approve_model(repo, "fake:strong")
    settings = load_settings(repo, {"effort": effort, "min_pages": 2, "strong": "fake:strong"})
    authorize(repo, settings, "HEAD", "preview")
    page = PlannedPage("1-wide.md", "Wide", None, "", ["src/widgetry/wide.py", "src/widgetry/cli.py"])
    ctx = gen.Context(repo, Snapshot.at(repo), settings, "widgetry", FakeRunner(), FakeRunner(), [page],
                      lambda _m: None)  # fmt: skip
    ex = gen.page_excerpts(ctx, page)
    assert ex.size <= limit and len(ex.text) < limit // 2  # the KiB ceiling binds in bytes, not characters
    assert ex.size > limit - 1_000  # the ceiling is used, not undershot
    shown, total = ex.shown["src/widgetry/wide.py"]
    assert 0 < shown < total and ex.text.count("字") * 3 < limit
    envelope, _ = gen.page_request(ctx, page)
    assert ex.text in envelope.prompt  # the bounded excerpts are what the page prompt carries


def test_context_bytes_refuses_when_headers_alone_exceed_it(repo: Path) -> None:
    from repowiki.prompts import PlannedPage

    settings = load_settings(repo, {"min_pages": 2, "context_bytes": 10})
    page = PlannedPage("1-x.md", "X", None, "", ["src/widgetry/cli.py"])
    ctx = gen.Context(repo, Snapshot.at(repo), settings, "widgetry", FakeRunner(), FakeRunner(), [page],
                      lambda _m: None)  # fmt: skip
    with pytest.raises(gen.GenerateError, match="headers alone .* over the context_bytes ceiling of 10"):
        gen.page_excerpts(ctx, page)


def test_standard_final_pass_runs_on_the_single_approved_model(repo: Path) -> None:
    settings = load_settings(repo, {"effort": "standard", "min_pages": 2})
    gen.generate(repo, settings, log=lambda _m: None, plan_only=True)
    text = gen.preview(repo, settings)
    assert re.search(r"^  final pass \S+ -> fake: deferred", text, re.M)
    assert "  models: drafting fake; final pass fake (the drafting model)\n" in text
