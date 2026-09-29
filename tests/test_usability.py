"""Usability: config carried into `site`, extensionless files, `doctor`, local-only Ollama."""

from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki.cli import main
from repowiki.config import Settings
from repowiki.doctor import diagnose, doctor
from repowiki.generate import generate
from repowiki.runners import FakeRunner, RunnerError, is_cloud_model, make_runner, ollama_host_is_local
from repowiki.site import RepoEntry, build_site


def test_site_reads_wiki_dir_from_the_repo_config(repo: Path, tmp_path: Path) -> None:
    write(repo, ".repowiki.toml", 'wiki_dir = "handbook"\n')
    commit(repo, "config")
    assert main(["generate", str(repo), "--model", "fake", "--min-pages", "2"]) == 0
    assert (repo / "handbook/index.json").is_file()
    out = tmp_path / "site"
    assert main(["site", str(repo), "-o", str(out)]) == 0
    assert (out / "widgetry/1-overview.html").is_file()


def test_extensionless_files_link_as_files_and_directories_as_trees(repo: Path, tmp_path: Path) -> None:
    write(repo, "Makefile", "all:\n\techo hi\n")
    write(repo, "Dockerfile", "FROM scratch\n")
    commit(repo, "build files")
    s = Settings(cheap="fake", min_pages=2)
    fake = FakeRunner()
    s.runners = lambda: (fake, fake)  # type: ignore[method-assign]
    generate(repo, s, log=lambda _m: None)
    page = repo / "docs/wiki/1-overview.md"
    page.write_text(
        page.read_text().replace(
            "\n<!--", "\nSee [Makefile](../../Makefile), [Dockerfile](../../Dockerfile) and [src](../../src).\n\n<!--"
        )
    )
    out = tmp_path / "site"
    build_site([RepoEntry("W", repo, url="https://github.com/example/w")], out, log=lambda _m: None)
    html = (out / "w/1-overview.html").read_text()

    def href(label: str) -> str:
        m = re.search(rf'href="([^"]+)"[^>]*>{label}</a>', html)
        assert m, label
        return m.group(1)

    assert re.fullmatch(r"https://github.com/example/w/blob/[0-9a-f]{40}/Makefile", href("Makefile"))
    assert "/blob/" in href("Dockerfile")
    assert re.fullmatch(r"https://github.com/example/w/tree/[0-9a-f]{40}/src", href("src"))


# --------------------------------------------------------------------------- doctor

STAND_IN = """#!/bin/sh
case "$1 $2" in
  "--version "*) echo "9.9.9 (Claude Code)";;
  "--help "*) echo "{help}";;
  "auth status") echo '{{"loggedIn": {signed_in}}}';;
  *) echo "unexpected $*" >&2; exit 2;;
esac
"""
CLAUDE_HELP = (
    "  --tools <tools...>  --strict-mcp-config  --mcp-config <c>  --setting-sources <s>  "
    "--disable-slash-commands  --no-session-persistence  --output-format <f>"
)


def stand_in(tmp_path: Path, help_text: str = CLAUDE_HELP, signed_in: str = "true") -> Path:
    exe = tmp_path / "claude-stand-in"
    exe.write_text(STAND_IN.format(help=help_text, signed_in=signed_in))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_doctor_checks_version_flags_and_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(stand_in(tmp_path)))
    report = diagnose("claude")
    assert report.ok and report.version == "9.9.9 (Claude Code)" and report.path == str(tmp_path / "claude-stand-in")


def test_doctor_flags_old_cli_and_signed_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_help = CLAUDE_HELP.replace("--tools <tools...>", "").replace("--setting-sources <s>", "")
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(stand_in(tmp_path, old_help, "false")))
    report = diagnose("claude")
    assert not report.ok
    text = " ".join(report.problems)
    assert "--tools" in text and "--setting-sources" in text and "claude auth login" in text


def test_doctor_cli_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(stand_in(tmp_path)))
    assert main(["doctor", "--model", "claude:haiku"]) == 0
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(tmp_path / "missing"))
    assert main(["doctor", "--model", "claude"]) == 1
    assert "not found" in capsys.readouterr().out
    assert [r.backend for r in doctor([])] == ["claude", "opencode", "codex", "ollama"]


def test_backends_honours_binary_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("REPOWIKI_CODEX_BIN", str(stand_in(tmp_path)))
    assert main(["backends"]) == 0
    assert f"codex     found at {tmp_path / 'claude-stand-in'}" in capsys.readouterr().out


# --------------------------------------------------------------------------- ollama stays local


@pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "deepseek-v3.1:cloud", "x-cloud"])
def test_ollama_cloud_models_are_refused(model: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert is_cloud_model(model)
    with pytest.raises(RunnerError, match="cloud"):
        make_runner(f"ollama:{model}")
    make_runner(f"ollama:{model}", allow_remote=True)


@pytest.mark.parametrize("host", ["gpu.example.com:11434", "http://10.0.0.5:11434", "https://ollama.com"])
def test_ollama_remote_hosts_are_refused(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", host)
    assert not ollama_host_is_local(host)
    with pytest.raises(RunnerError, match="OLLAMA_HOST"):
        make_runner("ollama:qwen3:8b")
    make_runner("ollama:qwen3:8b", allow_remote=True)


@pytest.mark.parametrize("host", ["", "127.0.0.1:11434", "http://localhost:11434", "0.0.0.0", "[::1]:11434"])
def test_local_ollama_is_fine(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", host)
    assert ollama_host_is_local(host) and not is_cloud_model("qwen3:8b")
    make_runner("ollama:qwen3:8b")


def test_generate_refuses_cloud_ollama_before_any_call(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "ollama:gpt-oss:120b-cloud"]) == 2
    assert "cloud model" in capsys.readouterr().err
    assert not (repo / "docs/wiki").exists()
