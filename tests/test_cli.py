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


def test_r6_site_validated_ref_main_builds_only_a_valid_committed_wiki(
    repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The exact gate command: `repowiki site --validated --ref main`."""
    out = tmp_path / "site"
    assert main(["site", str(repo), "-o", str(out), "--validated", "--ref", "main"]) == 2  # no wiki at main
    assert "failed validation" in capsys.readouterr().err and not out.exists()
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2"]) == 0
    commit(repo, "wiki")
    assert main(["site", str(repo), "-o", str(out), "--validated", "--ref", "main"]) == 0
    good = (out / "widgetry/1-overview.html").read_text()
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(page.read_text() + "\n[bad](../../src/widgetry/cli.py#L999)\n")
    assert main(["site", str(repo), "-o", str(out), "--validated"]) == 2  # the working tree is invalid
    commit(repo, "bad range")
    assert main(["site", str(repo), "-o", str(out), "--validated", "--ref", "main"]) == 2
    assert "cites lines 999-999" in capsys.readouterr().err
    assert (out / "widgetry/1-overview.html").read_text() == good  # the last good site is untouched


def test_effort_flag_previews_effective_settings(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--min-pages", "2", "--effort", "draft", "--preview"]) == 0
    out = capsys.readouterr().out
    assert "  effort: draft (presets v1; sets limits only, not a quality guarantee)" in out
    assert "  run cap: 50,000 estimated input tokens (checked before every call)" in out
    assert main(["generate", str(repo), "--min-pages", "2", "--effort", "draft", "--max-pages", "6", "--preview"]) == 0
    assert "  pages: 2 to 6" in capsys.readouterr().out
    assert not (repo / "docs/wiki").exists()


@pytest.mark.parametrize("argv, message", [
    (["--effort", "turbo"], "unknown effort preset 'turbo'"),
    (["--effort", "draft", "--max-pages", "20"], "can only tighten"),
    (["--effort", "draft", "--budget", "60000"], "can only tighten"),
    (["--effort", "deep"], "deep preset needs an approved strong model"),
])  # fmt: skip
def test_effort_flag_refusals_exit_2(repo: Path, capsys: pytest.CaptureFixture[str], argv: list[str],
                                     message: str) -> None:  # fmt: skip
    assert main(["generate", str(repo), "--min-pages", "2", *argv, "--preview"]) == 2
    assert message in capsys.readouterr().err
    if "--max-pages" not in argv:  # update has no page-count flag
        assert main(["update", str(repo), *argv, "--dry-run"]) == 2
        assert message in capsys.readouterr().err
