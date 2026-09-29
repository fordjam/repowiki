"""Improvements: --preview shows exactly what goes to which provider; budgets and estimates stop runs early."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import generate as gen
from repowiki.cli import main
from repowiki.config import Settings
from repowiki.generate import BudgetError, Meter
from repowiki.runners import FakeRunner
from repowiki.update import update


def settings_for(cheap: FakeRunner, strong: FakeRunner | None = None, **kw: object) -> Settings:
    s = Settings(**{"cheap": "fake", "min_pages": 2, **kw})  # type: ignore[arg-type]
    s.runners = lambda: (cheap, strong or cheap)  # type: ignore[method-assign]
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
