from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from repowiki.runners import (
    BACKENDS,
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
env > "{log}/env"
pwd > "{log}/cwd"
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
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:3] == ["-p", "--output-format", "text"] and argv[-2:] == ["--model", "haiku"]
    assert (tmp_path / "stdin").read_text() == "the prompt"


def test_opencode_passes_message_or_file(tmp_path: Path) -> None:
    runner = OpencodeRunner(model="prov/m", binary=str(fake_cli(tmp_path)))
    runner.run("short prompt")
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv == ["run", "--pure", "--agent", "repowiki", "-m", "prov/m", "--", "short prompt"]
    runner.run("x" * 200_000)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[6] == "-f" and argv[7].endswith("prompt.md")


def test_opencode_strips_its_banner(tmp_path: Path) -> None:
    runner = OpencodeRunner(binary=str(fake_cli(tmp_path, answer="> repowiki · some-model\n\n# Page")))
    assert runner.run("p") == "# Page"


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


# --------------------------------------------------------------------------- isolation contract
#
# Every registered backend must appear here. Adding a backend without an
# isolation contract, or dropping a flag from one, fails this test.

FORBIDDEN = {
    "--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "bypassPermissions",
    "acceptEdits", "--dangerously-bypass-approvals-and-sandbox", "workspace-write", "danger-full-access",
    "--full-auto", "--yolo", "--auto", "--continue", "--resume", "--share", "--add-dir", "--allowedTools",
}  # fmt: skip


def _pairs(argv: list[str]) -> set[tuple[str, str]]:
    return set(zip(argv, argv[1:], strict=False))


def check_claude(argv: list[str], env: dict[str, str]) -> None:
    pairs = _pairs(argv)
    assert ("--tools", "") in pairs, "claude must run with every tool disabled"
    assert ("--setting-sources", "") in pairs and "--strict-mcp-config" in argv
    assert ("--mcp-config", '{"mcpServers":{}}') in pairs
    assert "--no-session-persistence" in argv and "--disable-slash-commands" in argv


def check_opencode(argv: list[str], env: dict[str, str]) -> None:
    assert ("--agent", "repowiki") in _pairs(argv) and "--pure" in argv
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    agent = config["agent"]["repowiki"]
    assert agent["tools"] == {"*": False} and agent["permission"] == {"*": "deny"}
    assert config["permission"] == {"*": "deny"} and json.loads(env["OPENCODE_PERMISSION"]) == {"*": "deny"}
    assert env["OPENCODE_DB"] == ":memory:", "opencode sessions must not be persisted"
    assert env["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1" and env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"


def check_codex(argv: list[str], env: dict[str, str]) -> None:
    pairs = _pairs(argv)
    assert ("--sandbox", "read-only") in pairs and "--ephemeral" in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    for feature in ("shell_tool", "unified_exec", "apps", "plugins", "hooks", "browser_use", "computer_use"):
        assert ("--disable", feature) in pairs, feature
    assert ("-c", 'web_search="disabled"') in pairs


def check_ollama(argv: list[str], env: dict[str, str]) -> None:
    assert argv[:2] == ["run", "small"]  # `ollama run` has no tool use


CONTRACTS = {"claude": check_claude, "opencode": check_opencode, "codex": check_codex, "ollama": check_ollama}


def test_every_backend_has_an_isolation_contract() -> None:
    assert set(CONTRACTS) == set(BACKENDS)


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_backends_run_without_write_or_shell_capability(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghx-not-for-models")
    monkeypatch.setenv("SOME_SERVICE_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENCODE_CONFIG", "/tmp/permissive.json")
    runner = BACKENDS[backend](model="small", binary=str(fake_cli(tmp_path)))
    assert runner.run("p") == "hello"
    argv = (tmp_path / "argv").read_text().splitlines()
    env = dict(line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines() if "=" in line)
    CONTRACTS[backend](argv, env)
    assert not FORBIDDEN & set(argv), FORBIDDEN & set(argv)
    assert not any(v in ("default", "Bash", "Edit", "Write") for k, v in _pairs(argv) if k == "--tools")
    # a minimal environment: no inherited secrets, no user-chosen config overrides
    assert "GITHUB_TOKEN" not in env and "SOME_SERVICE_PASSWORD" not in env and "OPENCODE_CONFIG" not in env
    assert env["HOME"] and env["TERM"] == "dumb"
    # runs in an empty throwaway directory, never the repository
    assert Path((tmp_path / "cwd").read_text().strip()).name.startswith("repowiki-")


def test_pass_env_is_an_explicit_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROXY_TOKEN", "t")
    monkeypatch.setenv("REPOWIKI_PASS_ENV", "MY_PROXY_TOKEN")
    ClaudeRunner(binary=str(fake_cli(tmp_path))).run("p")
    assert "MY_PROXY_TOKEN=t" in (tmp_path / "env").read_text()
