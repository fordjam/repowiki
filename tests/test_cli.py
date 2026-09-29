from __future__ import annotations

from pathlib import Path

import pytest

from conftest import commit, write
from repowiki.cli import main


def test_end_to_end_with_fake_backend(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2", "--plan-only"]) == 0
    assert not (repo / "docs/wiki").exists()
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2", "--name", "example/widgetry"]) == 0
    assert main(["validate", str(repo)]) == 0
    commit(repo, "wiki")
    write(repo, "src/widgetry/cli.py", "def main():\n    return 1\n")
    commit(repo, "cli")
    assert main(["update", str(repo), "--model", "fake", "--dry-run"]) == 0
    assert "stale:" in capsys.readouterr().out
    assert main(["update", str(repo), "--model", "fake"]) == 0
    out = tmp_path / "site"
    assert main(["site", str(repo), "-o", str(out), "--title", "Demo"]) == 0
    assert "Demo" in (out / "index.html").read_text()


def test_validate_reports_failure(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate", str(repo)]) == 1
    assert "not found" in capsys.readouterr().out


def test_errors_exit_2(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "nosuch:model"]) == 2
    assert "unknown backend" in capsys.readouterr().err


def test_backends_lists_adapters(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backends"]) == 0
    out = capsys.readouterr().out
    for name in ("claude", "opencode", "codex", "ollama", "fake"):
        assert name in out
