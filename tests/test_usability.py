"""Usability: config carried into `site`, extensionless files, `doctor`, local-only Ollama."""

from __future__ import annotations

import re
import stat
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path

import pytest

from conftest import commit, write
from repowiki import runners
from repowiki.cli import main
from repowiki.config import Settings
from repowiki.doctor import diagnose, doctor
from repowiki.generate import generate
from repowiki.runners import (
    FakeRunner,
    OllamaRunner,
    RunnerError,
    is_cloud_model,
    make_runner,
    ollama_endpoint,
    ollama_host_is_local,
)
from repowiki.site import RepoEntry, build_site

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # build_site needs a fixed instant, never the clock


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
    build_site([RepoEntry("W", repo, url="https://github.com/example/w")], out, log=lambda _m: None, now=NOW)
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


OLLAMA_STAND_IN = """#!/bin/sh
case "$1 $2" in
  "--version "*) echo "ollama version is 0.0.0";;
  "run --help") echo "  --nowordwrap  Don't wrap words";;
  "list "*) printf 'NAME ID SIZE MODIFIED\\nsmall:latest 0 1 GB now\\n';;
  *) echo "unexpected $*" >&2; exit 2;;
esac
"""


def stand_in(tmp_path: Path, help_text: str = CLAUDE_HELP, signed_in: str = "true") -> Path:
    exe = tmp_path / "claude-stand-in"
    exe.write_text(STAND_IN.format(help=help_text, signed_in=signed_in))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_doctor_checks_version_flags_and_sign_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(stand_in(tmp_path)))
    report = diagnose("claude")
    assert report.version == "9.9.9 (Claude Code)" and report.path == str(tmp_path / "claude-stand-in")
    # Every flag advertised and signed in, yet not usable: help text never establishes a binary (R15).
    (problem,) = report.problems
    assert "no effective check proves" in problem and "stays disabled" in problem and not report.ok


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
    ollama = tmp_path / "ollama-stand-in"
    ollama.write_text(OLLAMA_STAND_IN)
    ollama.chmod(ollama.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("REPOWIKI_OLLAMA_BIN", str(ollama))
    assert main(["doctor", "--model", "ollama:small"]) == 0  # an established backend
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(stand_in(tmp_path)))
    assert main(["doctor", "--model", "claude:haiku"]) == 1  # advertised flags only: not established (R15)
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(tmp_path / "missing"))
    assert main(["doctor", "--model", "claude"]) == 1
    assert "not found" in capsys.readouterr().out
    assert [r.backend for r in doctor([])] == ["claude", "opencode", "codex", "ollama"]


def test_default_doctor_cannot_discover_installed_clis() -> None:
    """The default suite hides every installed backend CLI: doctor finds none and runs nothing."""
    reports = doctor([])
    assert [r.backend for r in reports] == ["claude", "opencode", "codex", "ollama"]
    for report in reports:
        assert not report.installed and report.version is None, report
        assert "not found" in report.problems[0]


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


@pytest.mark.parametrize(
    "host",
    [
        "http://127.remote.example:11434",  # the reviewer's case: a DNS name that merely starts with 127.
        "127.0.0.1.example.com",
        "gpu.example.com:11434",
        "http://10.0.0.5:11434",
        "https://ollama.com",
        "http://user:pw@127.0.0.1:11434",
        "http://127.0.0.1:11434/proxy",
        "http://127.0.0.1:11434?x=1",
        "ftp://127.0.0.1",
    ],
)
def test_ollama_non_loopback_hosts_are_refused(host: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", host)
    assert not ollama_host_is_local(host)
    with pytest.raises(RunnerError, match="OLLAMA_HOST"):
        make_runner("ollama:qwen3:8b")
    make_runner("ollama:qwen3:8b", allow_remote=True)


@pytest.mark.parametrize(
    "host, endpoint",
    [
        ("", "http://127.0.0.1:11434"),
        ("127.0.0.1:11434", "http://127.0.0.1:11434"),
        ("127.8.9.10", "http://127.8.9.10:11434"),
        ("http://localhost:11434", "http://localhost:11434"),
        ("0.0.0.0", "http://0.0.0.0:11434"),
        ("[::1]:11434", "http://[::1]:11434"),
    ],
)
def test_local_ollama_is_fine(host: str, endpoint: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OLLAMA_HOST", host)
    assert ollama_endpoint(host) == endpoint and ollama_host_is_local(host)
    runner = make_runner("ollama:qwen3:8b")
    assert isinstance(runner, OllamaRunner) and runner.host == endpoint
    assert runner.env(tmp_path)["OLLAMA_HOST"] == endpoint  # the verified server is the one used


def test_ollama_model_must_be_installed_and_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    asked: list[tuple[str, str]] = []

    def remote(base: str, model: str, timeout: float = 10.0) -> dict[str, object]:
        asked.append((base, model))
        return {"remote_host": "https://ollama.com:443", "remote_model": "big"}

    monkeypatch.setattr(runners, "_ollama_show", remote)
    with pytest.raises(RunnerError, match="runs remotely"):
        make_runner("ollama:sneaky")
    assert asked == [("http://127.0.0.1:11434", "sneaky")]

    def missing(base: str, model: str, timeout: float = 10.0) -> dict[str, object]:
        raise urllib.error.HTTPError(f"{base}/api/show", 404, "not found", Message(), None)

    monkeypatch.setattr(runners, "_ollama_show", missing)
    with pytest.raises(RunnerError, match="ollama pull"):
        make_runner("ollama:absent")

    def down(base: str, model: str, timeout: float = 10.0) -> dict[str, object]:
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(runners, "_ollama_show", down)
    with pytest.raises(RunnerError, match="could not reach"):
        make_runner("ollama:qwen3:8b")


def test_generate_refuses_cloud_ollama_before_any_call(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["generate", str(repo), "--model", "ollama:gpt-oss:120b-cloud"]) == 2
    assert "cloud model" in capsys.readouterr().err
    assert not (repo / "docs/wiki").exists()
