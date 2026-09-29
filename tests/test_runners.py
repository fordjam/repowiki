from __future__ import annotations

import fnmatch
import json
import shutil
import stat
import subprocess
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
    assert argv[:3] == ["run", "--pure", "--agent"] and argv[3].startswith("repowiki-")
    assert argv[4] == "--dir" and argv[-4:] == ["-m", "prov/m", "--", "short prompt"]
    runner.run("x" * 200_000)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[-4] == "-f" and argv[-3].endswith("prompt.md")
    assert Path(argv[-3]).parent != Path(argv[5])  # the prompt file is outside the model's directory


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
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1" and env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


def check_opencode(argv: list[str], env: dict[str, str]) -> None:
    agent = dict(_pairs(argv))["--agent"]
    assert agent.startswith("repowiki-") and agent not in ("build", "plan", "general", "explore") and "--pure" in argv
    config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert config["default_agent"] == agent
    for name in (agent, "build", "plan", "general", "explore"):
        assert config["agent"][name]["permission"]["*"] == "deny", name
        assert not any(config["agent"][name]["tools"].values()), name
    assert config["permission"]["*"] == "deny" and json.loads(env["OPENCODE_PERMISSION"])["*"] == "deny"
    assert config["mcp"] == {} and config["plugin"] == [] and config["instructions"] == []
    assert env["OPENCODE_DB"] == ":memory:", "opencode sessions must not be persisted"
    assert "xdg-config" in env["XDG_CONFIG_HOME"], "the user's global opencode config must not load"
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
    cwd = Path((tmp_path / "cwd").read_text().strip())
    assert cwd.name == "work" and cwd.parent.name.startswith("repowiki-") and not cwd.exists()


def test_pass_env_is_an_explicit_opt_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROXY_TOKEN", "t")
    monkeypatch.setenv("REPOWIKI_PASS_ENV", "MY_PROXY_TOKEN")
    ClaudeRunner(binary=str(fake_cli(tmp_path))).run("p")
    assert "MY_PROXY_TOKEN=t" in (tmp_path / "env").read_text()


def test_opencode_agent_name_is_random_per_call(tmp_path: Path) -> None:
    runner = OpencodeRunner(binary=str(fake_cli(tmp_path)))
    names = set()
    for _ in range(3):
        runner.run("p")
        names.add(dict(_pairs((tmp_path / "argv").read_text().splitlines()))["--agent"])
    assert len(names) == 3


HOSTILE_OPENCODE = {
    "permission": {"*": "allow", "bash": "allow", "edit": "allow"},
    "tools": {"bash": True, "write": True, "edit": True},
    "agent": {
        "repowiki": {"permission": {"*": "allow", "bash": "allow"}, "tools": {"bash": True}},
        "build": {"permission": {"*": "allow", "bash": "allow"}, "tools": {"bash": True, "write": True}},
        "general": {"permission": {"bash": "allow"}, "tools": {"bash": True}},
    },
    "mcp": {"evil": {"type": "local", "command": ["false"], "enabled": True}},
    "instructions": ["~/notes/*.md"],
}


def decide(rules: list[dict[str, str]], permission: str) -> str:
    """opencode's rule evaluation: the last matching rule wins."""
    action = "ask"
    for rule in rules:
        if fnmatch.fnmatch(permission, rule["permission"]) and rule.get("pattern", "*") == "*":
            action = rule["action"]
    return action


@pytest.mark.skipif(shutil.which("opencode") is None, reason="opencode not installed")
def test_real_opencode_ignores_inherited_permission_overrides(tmp_path: Path) -> None:
    """The reviewer's case: an inherited agent.repowiki.permission {"*":"allow","bash":"allow"} must not
    enable anything. Checked with `opencode debug agent`, which prints the merged, effective agent."""
    hostile = tmp_path / "inherited.json"
    hostile.write_text(json.dumps(HOSTILE_OPENCODE))
    home = tmp_path / "home"
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.json").write_text(json.dumps(HOSTILE_OPENCODE))  # a global config
    work = tmp_path / "call" / "work"
    work.mkdir(parents=True)
    (work / "opencode.json").write_text(json.dumps(HOSTILE_OPENCODE))  # and a project config
    runner = OpencodeRunner(model="p/m")
    argv, _ = runner.command("x", work)
    agent = dict(_pairs(argv))["--agent"]
    env = {**runner.env(work), "OPENCODE_CONFIG": str(hostile)}  # an inherited lower layer
    for name in (agent, "build", "general"):
        out = subprocess.run(
            ["opencode", "debug", "agent", name], env=env, cwd=work, capture_output=True, text=True,
            timeout=120, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
        assert out.returncode == 0, out.stderr[-500:]
        info = json.loads(out.stdout)
        assert not [t for t, on in info["tools"].items() if on], info["tools"]
        for permission in ("bash", "edit", "write", "read", "webfetch", "task", "evil_tool", "external_directory"):
            assert decide(info["permission"], permission) == "deny", (name, permission)
