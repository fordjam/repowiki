from __future__ import annotations

import stat
from pathlib import Path

import pytest

from repowiki.runners import (
    ClaudeRunner,
    CodexRunner,
    FakeRunner,
    OllamaRunner,
    OpencodeRunner,
    RunnerError,
    make_runner,
    parse_spec,
)

# A stand-in CLI: records argv and stdin, then prints a canned answer.
FAKE_CLI = """#!/bin/sh
printf '%s\\n' "$@" > "{log}/argv"
cat > "{log}/stdin"
{extra}
echo "{answer}"
"""


def fake_cli(tmp_path: Path, answer: str = "hello", extra: str = "") -> Path:
    exe = tmp_path / "fakecli"
    exe.write_text(FAKE_CLI.format(log=tmp_path, answer=answer, extra=extra))
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_parse_spec() -> None:
    assert parse_spec("claude") == ("claude", None)
    assert parse_spec("claude:haiku") == ("claude", "haiku")
    assert parse_spec("opencode:provider/model-x") == ("opencode", "provider/model-x")
    assert parse_spec("ollama:qwen3:8b") == ("ollama", "qwen3:8b")
    with pytest.raises(ValueError):
        parse_spec("nope:model")


def test_make_runner_types() -> None:
    assert isinstance(make_runner("claude:sonnet"), ClaudeRunner)
    assert isinstance(make_runner("codex"), CodexRunner)
    assert isinstance(make_runner("fake"), FakeRunner)
    assert make_runner("claude:sonnet").label == "claude:sonnet"


def test_claude_command_uses_stdin(tmp_path: Path) -> None:
    runner = ClaudeRunner(model="haiku", binary=str(fake_cli(tmp_path)))
    assert runner.run("the prompt") == "hello"
    assert (tmp_path / "argv").read_text().split() == ["-p", "--output-format", "text", "--model", "haiku"]
    assert (tmp_path / "stdin").read_text() == "the prompt"


def test_opencode_passes_message_or_file(tmp_path: Path) -> None:
    runner = OpencodeRunner(model="prov/m", binary=str(fake_cli(tmp_path)))
    runner.run("short prompt")
    assert (tmp_path / "argv").read_text().splitlines() == ["run", "-m", "prov/m", "short prompt"]
    runner.run("x" * 200_000)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:4] == ["run", "-m", "prov/m", "-f"] and argv[4].endswith("prompt.md")


def test_codex_reads_last_message_file(tmp_path: Path) -> None:
    # The fake writes the file named after -o, like `codex exec -o FILE` does.
    extra = 'while [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then echo "final answer" > "$2"; fi; shift; done'
    runner = CodexRunner(model="m1", binary=str(fake_cli(tmp_path, answer="log noise", extra=extra)))
    assert runner.run("p") == "final answer"
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:3] == ["exec", "--sandbox", "read-only"] and argv[-1] == "-" and "m1" in argv


def test_ollama_needs_model_and_strips_thinking(tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        OllamaRunner(binary=str(fake_cli(tmp_path))).run("p")
    runner = OllamaRunner(model="small", binary=str(fake_cli(tmp_path, answer="<think>hmm</think>answer")))
    assert runner.run("p") == "answer"


def test_errors_are_runner_errors(tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="not found"):
        ClaudeRunner(binary=str(tmp_path / "missing")).run("p")
    with pytest.raises(RunnerError, match="exit 3"):
        ClaudeRunner(binary=str(fake_cli(tmp_path, extra="echo boom >&2; exit 3"))).run("p")
    with pytest.raises(RunnerError, match="timed out"):
        ClaudeRunner(binary=str(fake_cli(tmp_path, extra="sleep 5")), timeout=0.5).run("p")
    with pytest.raises(RunnerError, match="empty"):
        ClaudeRunner(binary=str(fake_cli(tmp_path, answer=""))).run("p")


def test_binary_override_from_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(fake_cli(tmp_path, answer="via env")))
    assert make_runner("claude").run("p") == "via env"


def test_fake_runner_records_calls() -> None:
    fake = FakeRunner(respond=lambda p: p.upper())
    assert fake.run("abc") == "ABC"
    assert fake.calls == ["abc"]
