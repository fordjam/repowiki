from __future__ import annotations

import fnmatch
import json
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from repowiki import compatibility
from repowiki.approval import authorize
from repowiki.config import Settings
from repowiki.outbound import Section, dispatch, seal
from repowiki.runners import (
    BACKENDS,
    OPENCODE_BUILTIN_AGENTS,
    ClaudeRunner,
    CLIRunner,
    CodexRunner,
    FakeRunner,
    OllamaRunner,
    OpencodeRunner,
    RunnerError,
    make_runner,
    minimal_env,
    parse_spec,
)
from repowiki.runners import _ollama_show as real_ollama_show

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


def establish_stand_in(runner) -> None:
    """Record the stand-in binary as established, as `repowiki doctor` would for a real one (R15):
    these tests are about what a call sends; test_compatibility covers establishing a binary."""
    identity = compatibility.identify(runner.exe()) if isinstance(runner, CLIRunner) else None
    if identity is not None:
        props = compatibility.PROPERTIES[runner.backend]
        at = "2026-01-01T00:00:00+00:00"
        compatibility.save(compatibility.Established(runner.backend, identity, "stand-in", props, at))


@pytest.fixture
def send(repo: Path):
    """Production path for an adapter: a sealed envelope through the outbound gateway (R2).

    CLI runners refuse plain strings; the synthetic clone supplies the authorization.
    """
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")

    def go(runner, text: str) -> str:
        settings._approved_runners = (runner,)  # as Settings.runners() records the runners it built
        establish_stand_in(runner)
        return dispatch(runner, seal(settings, "draft", [Section("template", text)]), settings)

    return go


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


def test_claude_command_uses_stdin(send, tmp_path: Path) -> None:
    runner = ClaudeRunner(model="haiku", binary=str(fake_cli(tmp_path)))
    assert send(runner, "the prompt") == "hello"
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:3] == ["-p", "--output-format", "text"] and argv[-2:] == ["--model", "haiku"]
    assert (tmp_path / "stdin").read_text() == "the prompt"


def test_opencode_passes_message_or_file(send, tmp_path: Path) -> None:
    runner = OpencodeRunner(model="prov/m", binary=str(fake_cli(tmp_path)))
    send(runner, "short prompt")
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:3] == ["run", "--pure", "--agent"] and argv[3].startswith("repowiki-")
    assert argv[4] == "--dir" and argv[-4:] == ["-m", "prov/m", "--", "short prompt"]
    send(runner, "x" * 200_000)
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[-4] == "-f" and argv[-3].endswith("prompt.md")
    assert Path(argv[-3]).parent != Path(argv[5])  # the prompt file is outside the model's directory


def test_opencode_strips_its_banner(send, tmp_path: Path) -> None:
    runner = OpencodeRunner(binary=str(fake_cli(tmp_path, answer="> repowiki · some-model\n\n# Page")))
    assert send(runner, "p") == "# Page"


def test_codex_reads_last_message_file(send, tmp_path: Path) -> None:
    # The fake writes the file named after -o, like `codex exec -o FILE` does.
    extra = 'while [ "$#" -gt 0 ]; do if [ "$1" = "-o" ]; then echo "final answer" > "$2"; fi; shift; done'
    runner = CodexRunner(model="m1", binary=str(fake_cli(tmp_path, answer="log noise", extra=extra)))
    assert send(runner, "p") == "final answer"
    argv = (tmp_path / "argv").read_text().splitlines()
    assert argv[:3] == ["exec", "--sandbox", "read-only"] and argv[-1] == "-" and "m1" in argv


def test_ollama_needs_model_and_strips_thinking(send, tmp_path: Path) -> None:
    with pytest.raises(RunnerError):
        send(OllamaRunner(binary=str(fake_cli(tmp_path))), "p")
    runner = OllamaRunner(model="small", binary=str(fake_cli(tmp_path, answer="<think>hmm</think>answer")))
    assert send(runner, "p") == "answer"


def test_errors_are_runner_errors(send, tmp_path: Path) -> None:
    with pytest.raises(RunnerError, match="not found"):
        send(ClaudeRunner(binary=str(tmp_path / "missing")), "p")
    with pytest.raises(RunnerError, match="exit 3"):
        send(ClaudeRunner(binary=str(fake_cli(tmp_path, extra="echo boom >&2; exit 3"))), "p")
    with pytest.raises(RunnerError, match="timed out"):
        send(ClaudeRunner(binary=str(fake_cli(tmp_path, extra="sleep 5")), timeout=0.5), "p")
    with pytest.raises(RunnerError, match="empty"):
        send(ClaudeRunner(binary=str(fake_cli(tmp_path, answer=""))), "p")


def test_binary_override_from_environment(send, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("REPOWIKI_CLAUDE_BIN", str(fake_cli(tmp_path, answer="via env")))
    assert send(make_runner("claude"), "p") == "via env"


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
    send, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghx-not-for-models")
    monkeypatch.setenv("SOME_SERVICE_PASSWORD", "hunter2")
    monkeypatch.setenv("OPENCODE_CONFIG", "/tmp/permissive.json")
    runner = BACKENDS[backend](model="small", binary=str(fake_cli(tmp_path)))
    assert send(runner, "p") == "hello"
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


def test_pass_env_is_an_explicit_opt_in(send, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_PROXY_TOKEN", "t")
    monkeypatch.setenv("REPOWIKI_PASS_ENV", "MY_PROXY_TOKEN")
    send(ClaudeRunner(binary=str(fake_cli(tmp_path))), "p")
    assert "MY_PROXY_TOKEN=t" in (tmp_path / "env").read_text()


def test_opencode_agent_name_is_random_per_call(send, tmp_path: Path) -> None:
    runner = OpencodeRunner(binary=str(fake_cli(tmp_path)))
    names = set()
    for _ in range(3):
        send(runner, "p")
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


# --------------------------------------------------------------------------- hostile inherited HOME
#
# The isolation claims are checked with a synthetic hostile HOME *installed* (HOME and the XDG
# directories point at it) before the production environment is derived. Its canaries: permissive
# permissions and agents, an instruction file, a plugin that leaves a marker when loaded, MCP servers.

CANARY = "REPOWIKI-CANARY"


def hostile_home(tmp_path: Path) -> Path:
    home = tmp_path / "hostile-home"
    marker = home / "PLUGIN-LOADED"
    plugin = home / "canary-plugin.js"
    instructions = home / f"{CANARY}-instructions.md"
    config = {
        **HOSTILE_OPENCODE,
        "instructions": [str(instructions)],
        "plugin": [plugin.as_uri()],
    }
    files = {
        ".config/opencode/opencode.json": json.dumps(config),
        ".config/opencode/AGENTS.md": f"{CANARY}: global opencode instructions\n",
        ".claude/settings.json": json.dumps({"permissions": {"allow": ["Bash", "Edit", "Write"]}}),
        ".claude/CLAUDE.md": f"{CANARY}: global claude instructions\n",
        ".codex/config.toml": 'sandbox_mode = "danger-full-access"\napproval_policy = "never"\n',
        ".codex/AGENTS.md": f"{CANARY}: global codex instructions\n",
        instructions.name: f"{CANARY}: listed instruction file\n",
        plugin.name: (
            'import { writeFileSync } from "node:fs";\n'
            f"writeFileSync({json.dumps(str(marker))}, {json.dumps(CANARY)});\n"
            "export const Canary = async () => ({});\n"
        ),
    }
    for rel, text in files.items():
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text(text)
    return home


def install_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """The hostile HOME as the inherited environment: HOME and the XDG config, data and cache
    directories inside it (XDG_STATE_HOME stays the synthetic operator state holding the receipt)."""
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(var, str(home / sub))


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_hostile_home_is_installed_before_the_production_environment_is_derived(
    send, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = hostile_home(tmp_path)
    install_home(monkeypatch, home)
    runner = BACKENDS[backend](model="small", binary=str(fake_cli(tmp_path)))
    assert send(runner, "p") == "hello"
    argv = (tmp_path / "argv").read_text().splitlines()
    env = dict(line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines() if "=" in line)
    assert env["HOME"] == str(home), "the hostile HOME must be the one the backend inherits"
    CONTRACTS[backend](argv, env)
    if backend == "opencode":  # its config home is private, empty and outside the hostile HOME
        private = Path(env["XDG_CONFIG_HOME"])
        assert home not in private.parents and private.parent.name.startswith("repowiki-")
        assert CANARY not in env["OPENCODE_CONFIG_CONTENT"]
    assert not (home / "PLUGIN-LOADED").exists()


@pytest.mark.integration("opencode")
def test_real_opencode_ignores_inherited_permission_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline: list[str]
) -> None:
    """With a hostile HOME installed, opencode without repowiki's isolation loads the canaries (the
    positive control); with it, the global and project layers add no instruction, plugin or MCP
    server, and every agent opencode lists (built-in and generated) has every tool off and every
    permission denied, even under an inherited permissive layer. Checked with ``opencode debug``,
    which prints the merged, effective config. The binary runs without network access (``offline``)
    and without inherited credentials."""
    from conftest import CREDENTIAL_RE, integration_binary

    binary = integration_binary("opencode")
    assert binary
    home = hostile_home(tmp_path)
    install_home(monkeypatch, home)
    marker = home / "PLUGIN-LOADED"
    inherited = tmp_path / "inherited.json"
    inherited.write_text(json.dumps(HOSTILE_OPENCODE))
    work = tmp_path / "call" / "work"
    work.mkdir(parents=True)
    (work / "opencode.json").write_text(json.dumps(HOSTILE_OPENCODE))  # and a project config

    def debug(env: dict[str, str], *args: str) -> str:
        out = subprocess.run(
            [*offline, binary, "debug", *args], env=env, cwd=work, capture_output=True, text=True,
            timeout=600, stdin=subprocess.DEVNULL, check=False,
        )  # fmt: skip
        assert out.returncode == 0, out.stderr[-500:]
        return out.stdout

    # Positive control: the production base environment, derived with the hostile HOME, no isolation.
    base = minimal_env(OpencodeRunner.env_names)
    assert not [k for k in base if CREDENTIAL_RE.search(k)], "no inherited credential reaches the binary"
    plain = {**base, "OPENCODE_CONFIG": str(inherited)}
    control = json.loads(debug(plain, "config"))
    assert any(CANARY in str(i) for i in control["instructions"]), "the instruction canary must load without isolation"
    assert any("canary-plugin" in str(p) for p in control["plugin"]) and marker.exists(), "the plugin canary must load"
    assert control["permission"]["*"] == "allow" and control["agent"]["build"]["tools"]["bash"] is True
    marker.unlink()

    runner = OpencodeRunner(model="p/m", binary=binary)
    argv, _ = runner.command("x", work)
    generated = dict(_pairs(argv))["--agent"]
    production = runner.env(work)  # what repowiki actually runs with
    raw = debug(production, "config")
    isolated = json.loads(raw)
    assert CANARY not in raw and not marker.exists()
    assert isolated["instructions"] == [] and isolated["plugin"] == [] and isolated["mcp"] == {}
    assert isolated["permission"]["*"] == "deny"
    # Permissions and tools stay locked even under an inherited OPENCODE_CONFIG layer, which repowiki
    # never passes itself (only an explicit REPOWIKI_PASS_ENV opt-in could).
    env = {**production, "OPENCODE_CONFIG": str(inherited)}
    listed = subprocess.run(
        [*offline, binary, "agent", "list"], env=production, cwd=work, capture_output=True, text=True,
        timeout=600, stdin=subprocess.DEVNULL, check=False,
    )  # fmt: skip
    assert listed.returncode == 0, listed.stderr[-500:]
    # Every agent opencode itself provides, plus the generated one (an agent the inherited layer
    # defines under another name is never selected: the generated agent is the default and --agent).
    agents = re.findall(r"^(\S+) \((?:primary|subagent|all)\)$", listed.stdout, re.M)
    assert generated in agents and set(OPENCODE_BUILTIN_AGENTS) <= set(agents), agents
    for layers in (production, env):
        for name in agents:
            info = json.loads(debug(layers, "agent", name))
            assert not [t for t, on in info["tools"].items() if on], (name, info["tools"])
            for permission in ("bash", "edit", "write", "read", "webfetch", "task", "evil_tool", "external_directory"):
                assert decide(info["permission"], permission) == "deny", (name, permission)
    assert not marker.exists()


# --------------------------------------------------------------------------- prompt delivery and state

# Records a prompt passed as a file (`-f PATH`) before the call's private directory is removed.
COPY_PROMPT_FILE = 'while [ "$#" -gt 0 ]; do if [ "$1" = "-f" ]; then cp "$2" "{log}/promptfile"; fi; shift; done'


def _call(tmp_path: Path) -> tuple[list[str], dict[str, str], str]:
    argv = (tmp_path / "argv").read_text().splitlines()
    env = dict(line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines() if "=" in line)
    private = str(Path((tmp_path / "cwd").read_text().strip()).parent)
    agents = [v for k, v in _pairs(argv) if k == "--agent"]

    def norm(value: str) -> str:
        for form in sorted({private, str(Path(private).resolve()), private.removeprefix("/private")}, key=len)[::-1]:
            value = value.replace(form, "<call>")
        return value.replace(agents[0], "<agent>") if agents else value

    delivered = (tmp_path / "promptfile").read_text() if (tmp_path / "promptfile").exists() else ""
    delivered = delivered or (tmp_path / "stdin").read_text() or argv[-1]
    cut = min([i for i, a in enumerate(argv) if a in ("-f", "--")] or [len(argv)])
    return [norm(a) for a in argv[:cut]], {k: norm(v) for k, v in env.items()}, delivered


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_small_and_file_backed_prompts_behave_identically(
    send, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_home(monkeypatch, hostile_home(tmp_path))
    copier = COPY_PROMPT_FILE.format(log=tmp_path)
    runner = BACKENDS[backend](model="small", binary=str(fake_cli(tmp_path, extra=copier)))
    small, large = "short prompt", "x" * 200_000 + " end"
    results = []
    for prompt in (small, large):
        (tmp_path / "promptfile").unlink(missing_ok=True)
        assert send(runner, prompt) == "hello"
        argv, env, delivered = _call(tmp_path)
        raw = dict(line.split("=", 1) for line in (tmp_path / "env").read_text().splitlines() if "=" in line)
        CONTRACTS[backend]((tmp_path / "argv").read_text().splitlines(), raw)
        assert delivered == prompt  # the whole prompt arrives, whichever way it is passed
        results.append((argv, env))
    assert results[0] == results[1]  # same command, same isolation, same environment
    if backend == "opencode":
        assert (tmp_path / "promptfile").exists(), "a large opencode prompt is passed as a file"


@pytest.mark.parametrize("backend", sorted(BACKENDS))
def test_repeat_runs_inherit_no_state(send, backend: str, tmp_path: Path) -> None:
    extra = (
        f'ls -A "$PWD" > "{tmp_path}/work-before"; touch "$PWD/leftover"; '
        f'if [ -n "$OPENCODE_CONFIG_CONTENT" ]; then ls -A "$XDG_CONFIG_HOME" > "{tmp_path}/config-before"; '
        'touch "$XDG_CONFIG_HOME/leftover"; fi'
    )
    runner = BACKENDS[backend](model="small", binary=str(fake_cli(tmp_path, extra=extra)))
    seen = []
    for _ in range(2):
        assert send(runner, "p") == "hello"
        cwd = Path((tmp_path / "cwd").read_text().strip())
        assert (tmp_path / "work-before").read_text() == ""  # an empty working directory every time
        if backend == "opencode":
            assert (tmp_path / "config-before").read_text() == ""
        assert not cwd.parent.exists(), "the call's private directory is removed afterwards"
        seen.append(cwd)
    assert seen[0] != seen[1]


# --------------------------------------------------------------------------- hermetic default suite


def test_default_suite_cannot_discover_installed_backend_clis(tmp_path: Path) -> None:
    for name, cls in BACKENDS.items():
        runner = cls(model="small")
        assert shutil.which(runner.exe()) is None, name
        assert not runner.available(), name
    assert shutil.which("git") and shutil.which("sh")  # everything else still resolves


def test_default_suite_blocks_the_network_but_not_loopback() -> None:
    import socket

    from conftest import NetworkBlocked

    with pytest.raises(NetworkBlocked):
        socket.create_connection(("192.0.2.1", 80), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.getaddrinfo("example.com", 443)
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.create_connection(server.getsockname(), timeout=5):
            pass


def test_absent_integration_binary_skips_only_integration(
    pytester: pytest.Pytester, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(
        test_mixed="""
        import pytest

        @pytest.mark.integration("opencode")
        def test_real_binary():
            pass

        def test_default():
            pass
        """
    )
    monkeypatch.delenv("REPOWIKI_INTEGRATION_OPENCODE", raising=False)
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1, skipped=1)
    monkeypatch.setenv("REPOWIKI_INTEGRATION_OPENCODE", "opencode")  # a bare name is never looked up
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1, skipped=1)
    monkeypatch.setenv("REPOWIKI_INTEGRATION_OPENCODE", str(fake_cli(tmp_path)))
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=2)


def test_r3_described_runners_are_inert_and_cannot_send(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repowiki import runners as runners_mod

    contacted: list[str] = []
    monkeypatch.setattr(runners_mod, "_ollama_show", lambda base, model, timeout=10.0: contacted.append(model) or {})
    described = make_runner("ollama:small", verify=False)
    assert isinstance(described, OllamaRunner) and described.host is None and contacted == []
    make_runner("ollama:small")
    assert contacted == ["small"]  # verification is what contacts the server
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")
    inert = ClaudeRunner(binary=str(fake_cli(tmp_path)))
    with pytest.raises(ValueError, match="unapproved runner"):
        dispatch(inert, seal(settings, "draft", [Section("template", "p")]), settings)
    assert not (tmp_path / "argv").exists()


def test_inherited_credentials_never_reach_tests_or_backends(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(
        test_env="""
        import os

        from repowiki.runners import BACKENDS, minimal_env

        def test_clean():
            leaked = [k for k in os.environ if "CANARY" in os.environ[k]]
            assert leaked == []
            for cls in BACKENDS.values():
                env = minimal_env(cls.env_names, cls.env_prefixes)
                assert not [k for k, v in env.items() if "CANARY" in v], cls
        """
    )
    for name in ("OPENCODE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN",
                 "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "SOME_SERVICE_PASSWORD", "CODEX_HOME"):  # fmt: skip
        monkeypatch.setenv(name, f"CANARY-{name}")
    monkeypatch.setenv("REPOWIKI_PASS_ENV", "SOME_SERVICE_PASSWORD")
    pytester.runpytest_subprocess("-p", "no:cacheprovider").assert_outcomes(passed=1)


def test_offline_wrapper_denies_child_processes_the_network() -> None:
    """The subprocess canary integration tests rely on: refused, not merely slow or proxied."""
    from conftest import NETWORK_CANARY, network_denied, offline_prefix, wrapper_runs

    prefix = offline_prefix()
    if prefix is None or not wrapper_runs(prefix):
        # No wrapper here (another platform, or a parent sandbox that forbids nesting): integration skips too.
        pytest.skip("cannot start a network-denied child process here; integration tests skip too")
    assert network_denied(prefix)  # the wrapper starts children: they must be refused the network
    import sys

    probe = subprocess.run([*prefix, sys.executable, "-I", "-c", NETWORK_CANARY], capture_output=True, text=True)
    assert probe.stdout.split() == ["denied", "denied"]


def test_offline_wrapper_capability_check_skips_rather_than_fails() -> None:
    """A wrapper that cannot start a child is reported as unavailable, never as a network-denial failure."""
    import sys

    from conftest import network_denied, wrapper_runs

    broken = [sys.executable, "-c", "import sys; sys.exit(71)", "--"]  # like sandbox_apply: Operation not permitted
    assert not wrapper_runs(broken) and not network_denied(broken)


# --------------------------------------------------------------------------- R12: strict Ollama locality reports
#
# A synthetic /api/show report shaped like the one Ollama 0.12 returns for a pulled GGUF model. The
# hermetic suite stubs `_ollama_show`; these tests put the real one back and stub only the HTTP request.

GOOD_SHOW: dict[str, object] = {
    "modelfile": "# synthetic",
    "template": "{{ .Prompt }}",
    "details": {
        "parent_model": "",
        "format": "gguf",
        "family": "synthllama",
        "families": ["synthllama"],
        "parameter_size": "8.0B",
        "quantization_level": "Q4_K_M",
    },
    "model_info": {"general.architecture": "synthllama", "general.parameter_count": 8000000000},
    "capabilities": ["completion"],
}


def with_details(**changes: object) -> dict[str, object]:
    details = dict(GOOD_SHOW["details"])  # type: ignore[arg-type]
    for key, value in changes.items():
        if value is ...:
            details.pop(key)
        else:
            details[key] = value
    return {**GOOD_SHOW, "details": details}


@pytest.fixture
def ollama_server(monkeypatch: pytest.MonkeyPatch):
    """The real verification over a scripted /api/show: `reports[model]` is what the server answers next."""
    from repowiki import runners as runners_mod

    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.setattr(runners_mod, "_ollama_show", real_ollama_show)
    asked: list[tuple[str, str]] = []
    reports: dict[str, object] = {}

    def request(base: str, model: str, timeout: float) -> object:
        asked.append((base, model))
        return reports[model]

    monkeypatch.setattr(runners_mod, "_ollama_request", request)
    return asked, reports


def test_r12_accepted_profile_is_reverified_before_every_prompt(send, ollama_server, tmp_path: Path) -> None:
    asked, reports = ollama_server
    reports["small"] = GOOD_SHOW
    runner = OllamaRunner(model="small", binary=str(fake_cli(tmp_path)))
    assert send(runner, "one") == "hello" and send(runner, "two") == "hello"
    assert asked == [("http://127.0.0.1:11434", "small")] * 2  # once per prompt, never cached


@pytest.mark.parametrize("report", [{}, [], "local", None], ids=["empty", "list", "string", "null"])
def test_r12_empty_or_non_object_report_refuses(send, ollama_server, tmp_path: Path, report: object) -> None:
    _, reports = ollama_server
    reports["small"] = report
    with pytest.raises(RunnerError, match="does not match an accepted local model profile"):
        send(OllamaRunner(model="small", binary=str(fake_cli(tmp_path))), "p")
    assert not (tmp_path / "argv").exists()  # zero prompts


MALFORMED = {
    "details-missing": {k: v for k, v in GOOD_SHOW.items() if k != "details"},
    "details-not-object": {**GOOD_SHOW, "details": ["gguf"]},
    "format-missing": with_details(format=...),
    "format-empty": with_details(format=""),
    "format-number": with_details(format=1),
    "format-unknown": with_details(format="safetensors"),
    "family-missing": with_details(family=...),
    "family-blank": with_details(family="  "),
    "parameter-size-number": with_details(parameter_size=8),
    "quantization-null": with_details(quantization_level=None),
    "families-empty": with_details(families=[]),
    "families-string": with_details(families="synthllama"),
    "families-blank-item": with_details(families=["synthllama", ""]),
    "families-number-item": with_details(families=[1]),
    "model-info-missing": {k: v for k, v in GOOD_SHOW.items() if k != "model_info"},
    "model-info-empty": {**GOOD_SHOW, "model_info": {}},
    "model-info-list": {**GOOD_SHOW, "model_info": ["general.architecture"]},
    "remote-host-set": {**GOOD_SHOW, "remote_host": "https://ollama.com:443"},
    "remote-model-set": {**GOOD_SHOW, "remote_model": "big"},
    "remote-host-not-string": {**GOOD_SHOW, "remote_host": 0},
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_r12_malformed_report_refuses(send, ollama_server, tmp_path: Path, name: str) -> None:
    _, reports = ollama_server
    reports["small"] = MALFORMED[name]
    with pytest.raises(RunnerError, match="does not match an accepted local model profile"):
        send(OllamaRunner(model="small", binary=str(fake_cli(tmp_path))), "p")
    assert not (tmp_path / "argv").exists()  # zero prompts


def test_r12_empty_remote_fields_are_accepted(send, ollama_server, tmp_path: Path) -> None:
    _, reports = ollama_server
    reports["small"] = {**GOOD_SHOW, "remote_host": "", "remote_model": ""}
    assert send(OllamaRunner(model="small", binary=str(fake_cli(tmp_path))), "p") == "hello"


def test_r12_report_that_turns_remote_refuses_the_next_call(send, ollama_server, tmp_path: Path) -> None:
    _, reports = ollama_server
    reports["small"] = GOOD_SHOW
    runner = OllamaRunner(model="small", binary=str(fake_cli(tmp_path)))
    assert send(runner, "first") == "hello"
    (tmp_path / "argv").unlink()
    reports["small"] = {**GOOD_SHOW, "remote_host": "https://ollama.com:443", "remote_model": "small"}
    with pytest.raises(RunnerError, match="runs remotely"):
        send(runner, "second")
    assert not (tmp_path / "argv").exists()  # the second prompt was never sent


def test_r12_two_models_are_verified_separately(send, ollama_server, tmp_path: Path) -> None:
    asked, reports = ollama_server
    reports["small"] = GOOD_SHOW
    reports["other"] = {}
    good = OllamaRunner(model="small", binary=str(fake_cli(tmp_path)))
    bad = OllamaRunner(model="other", binary=str(fake_cli(tmp_path)))
    assert send(good, "p") == "hello"
    (tmp_path / "argv").unlink()
    with pytest.raises(RunnerError, match="ollama:other"):
        send(bad, "p")
    assert not (tmp_path / "argv").exists()
    assert [model for _, model in asked] == ["small", "other"]


@pytest.mark.parametrize(
    "host, needle",
    [("10.1.2.3", "changed"), ("http://127.0.0.1:9999", "changed"), ("ftp://127.0.0.1", "changed")],
)
def test_r12_endpoint_is_reverified_on_every_call(
    send, ollama_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str, needle: str
) -> None:
    asked, reports = ollama_server
    reports["small"] = GOOD_SHOW
    runner = OllamaRunner(model="small", binary=str(fake_cli(tmp_path)))
    assert send(runner, "p") == "hello"
    (tmp_path / "argv").unlink()
    monkeypatch.setenv("OLLAMA_HOST", host)
    with pytest.raises(RunnerError, match=needle):
        send(runner, "p")
    assert not (tmp_path / "argv").exists() and len(asked) == 1


@pytest.mark.parametrize("host", ["10.1.2.3", "127.remote.example", "https://ollama.com"])
def test_r12_invalid_loopback_identity_refuses_before_the_server_is_asked(
    send, ollama_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    asked, reports = ollama_server
    reports["small"] = GOOD_SHOW
    monkeypatch.setenv("OLLAMA_HOST", host)
    with pytest.raises(RunnerError, match="not a loopback address"):
        send(OllamaRunner(model="small", binary=str(fake_cli(tmp_path))), "p")
    with pytest.raises(RunnerError, match="cloud model"):
        monkeypatch.delenv("OLLAMA_HOST")
        send(OllamaRunner(model="gpt-oss:120b-cloud", binary=str(fake_cli(tmp_path))), "p")
    assert asked == [] and not (tmp_path / "argv").exists()


def test_r12_preview_stays_inert(ollama_server) -> None:
    asked, reports = ollama_server
    reports["small"] = {}
    described = make_runner("ollama:small", verify=False)
    assert isinstance(described, OllamaRunner) and described.host is None and asked == []
    with pytest.raises(RunnerError, match="not verified"):
        described.command("p", Path("."))  # an unverified runner cannot build a command
    with pytest.raises(RunnerError, match="does not match"):
        make_runner("ollama:small")
    assert asked == [("http://127.0.0.1:11434", "small")]


@pytest.mark.integration("ollama")
def test_real_ollama_report_matches_the_accepted_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The approved local integration for the profile: a real local server's /api/show for a model the
    operator has pulled (named in REPOWIKI_INTEGRATION_OLLAMA_MODEL) is accepted. Until it passes on
    the operator's Ollama version, the adapter stays unproven against that version."""
    import os

    from conftest import integration_binary
    from repowiki import runners as runners_mod

    binary = integration_binary("ollama")
    model = os.environ.get("REPOWIKI_INTEGRATION_OLLAMA_MODEL", "")
    if not model:
        pytest.skip("integration: set REPOWIKI_INTEGRATION_OLLAMA_MODEL to a locally pulled model")
    monkeypatch.setattr(runners_mod, "_ollama_show", real_ollama_show)
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=60, check=False)
    runner = make_runner(f"ollama:{model}", binaries={"ollama": str(binary)})
    assert isinstance(runner, OllamaRunner) and runner.host, version.stdout
