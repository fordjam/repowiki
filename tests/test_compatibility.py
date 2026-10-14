"""R15: effective CLI compatibility contracts (executable identity, required flags and capabilities,
effective isolation with positive controls), checked with synthetic stand-in binaries."""

from __future__ import annotations

import json
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from repowiki import compatibility as compat
from repowiki.approval import authorize
from repowiki.config import Settings
from repowiki.doctor import diagnose
from repowiki.outbound import Section, dispatch, seal
from repowiki.runners import BACKENDS, CODEX_DISABLED_FEATURES, OpencodeRunner, RunnerError

# A stand-in opencode that emulates the real CLI from the inputs repowiki actually produces, so these
# tests exercise repowiki's generated config and environment rather than a canned answer. Like opencode:
# config layers merge in order (built-in defaults with every tool on and every permission allowed; the
# global config under $XDG_CONFIG_HOME, else ~/.config; OPENCODE_CONFIG; the project's opencode.json unless
# OPENCODE_DISABLE_PROJECT_CONFIG=1; OPENCODE_CONFIG_CONTENT last), objects merge deeply and lists
# (instructions, plugins) concatenate. Plugins load unless OPENCODE_PURE=1 or --pure; `run` starts the
# configured MCP servers, runs a requested shell command when the selected agent's bash tool is on and
# allowed, echoes its instruction files, and saves its session unless OPENCODE_DB is ":memory:".
OPENCODE = r"""#!PYTHON
import json, os, re, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
env = os.environ
Path(env.get("STAND_IN_LOG", "/dev/null")).open("a").write(" ".join(args) + "\n")
home = Path(env["HOME"])
config_home = Path(env.get("XDG_CONFIG_HOME") or home / ".config")
data_home = Path(env.get("XDG_DATA_HOME") or home / ".local/share")
TOOLS = ["bash", "edit", "write", "read", "webfetch", "task"]
BUILTIN = ["build", "plan", "general", "explore"]

def load(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}

def merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        elif isinstance(v, list) and isinstance(out.get(k), list):
            out[k] = out[k] + [x for x in v if x not in out[k]]
        else:
            out[k] = v
    return out

layers = [load(config_home / "opencode/opencode.json")]
if env.get("OPENCODE_CONFIG"):
    layers.append(load(env["OPENCODE_CONFIG"]))
if env.get("OPENCODE_DISABLE_PROJECT_CONFIG") != "1":
    layers.append(load(Path.cwd() / "opencode.json"))
if env.get("OPENCODE_CONFIG_CONTENT"):
    layers.append(json.loads(env["OPENCODE_CONFIG_CONTENT"]))
cfg = {}
for layer in layers:
    cfg = merge(cfg, layer)
cfg.setdefault("instructions", [])
cfg.setdefault("plugin", [])
cfg.setdefault("mcp", {})
global_agents = config_home / "opencode/AGENTS.md"
if global_agents.exists():
    cfg["instructions"] = cfg["instructions"] + [str(global_agents)]
pure = env.get("OPENCODE_PURE") == "1" or "--pure" in args

def load_plugins():
    if pure:
        return
    for uri in cfg["plugin"]:
        text = Path(uri[7:] if uri.startswith("file://") else uri).read_text()
        m = re.search(r"writeFileSync\((\"[^\"]*\")", text)  # what loading this plugin would do
        if m:
            Path(json.loads(m.group(1))).write_text("loaded")

def tools_of(*layers):
    tools = {t: True for t in TOOLS}
    for layer in layers:
        layer = layer or {}
        if "*" in layer:
            tools = {t: bool(layer["*"]) for t in tools}
        tools.update({k: bool(v) for k, v in layer.items() if k != "*"})
    return tools

def rules_of(*layers):
    rules = [{"permission": "*", "action": "allow", "pattern": "*"}]
    for layer in layers:
        for k, v in (layer or {}).items():
            rules.append({"permission": k, "action": v, "pattern": "*"})
    return rules

def agent(name):
    own = (cfg.get("agent") or {}).get(name) or {}
    return {"tools": tools_of(cfg.get("tools"), own.get("tools")),
            "permission": rules_of(cfg.get("permission"), own.get("permission"))}

def allowed(rules, permission):
    action = "ask"
    for rule in rules:
        if rule["permission"] in ("*", permission):
            action = rule["action"]
    return action == "allow"

if args[:1] == ["--version"]:
    print("1.0.0-stand-in")
elif args[:2] == ["run", "--help"]:
    print("HELP")
elif args[:2] == ["debug", "config"]:
    load_plugins()
    print(json.dumps(cfg))
elif args[:2] == ["agent", "list"]:
    for name in BUILTIN + [a for a in (cfg.get("agent") or {}) if a not in BUILTIN]:
        print(f"{name} (primary)")
elif args[:2] == ["debug", "agent"]:
    print(json.dumps(agent(args[2])))
elif args[:1] == ["run"]:
    load_plugins()
    for server in cfg["mcp"].values():
        if server.get("enabled", True):
            subprocess.run(server["command"], check=False)
    name = args[args.index("--agent") + 1] if "--agent" in args else cfg.get("default_agent", "build")
    prompt = args[-1]
    if "-f" in args:
        prompt = Path(args[args.index("-f") + 1]).read_text()
    me = agent(name)
    m = re.search(r"TOOL-CANARY:(\S+?)\)", prompt)
    if m and me["tools"]["bash"] and allowed(me["permission"], "bash"):
        Path(m.group(1)).write_text("ran")
    said = [Path(i).read_text().strip() for i in cfg["instructions"] if Path(i).exists()]
    if env.get("OPENCODE_DB") != ":memory:":  # like opencode's database: one file, appended to
        store = data_home / "opencode/opencode.db"
        store.parent.mkdir(parents=True, exist_ok=True)
        with store.open("a") as db:
            db.write(json.dumps({"prompt": prompt}) + "\n")
    print("hello" + "".join("\n" + s for s in said))
else:
    print("unexpected", args, file=sys.stderr)
    sys.exit(2)
"""
AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
OPENCODE_HELP = "--agent <a>  --pure  --model <m>  --file <f>  --dir <d>"


def stand_in(tmp_path: Path, name: str, text: str) -> Path:
    exe = tmp_path / name
    exe.write_text(text)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def opencode(tmp_path: Path, *, help_text: str = OPENCODE_HELP, ignores: str = "") -> Path:
    """The emulating stand-in; ``ignores`` names an isolation input this build silently ignores
    (an advertised but ineffective flag or variable)."""
    text = OPENCODE.replace("PYTHON", sys.executable, 1).replace('print("HELP")', f"print({help_text!r})")
    if ignores:
        ignoring = (
            f"args = [a for a in sys.argv[1:] if a != {ignores!r}]\n"
            f"env = {{k: v for k, v in os.environ.items() if k != {ignores!r}}}\n"
        )
        text = text.replace("args = sys.argv[1:]\nenv = os.environ\n", ignoring)
    return stand_in(tmp_path, "opencode-stand-in", text)


@pytest.fixture
def send(repo: Path):
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")

    def go(runner, text: str) -> str:
        settings._approved_runners = (runner,)
        return dispatch(runner, seal(settings, "draft", [Section("template", text)]), settings)

    return go


def problems_of(runner) -> list[str]:
    found = compat.establish(runner)
    assert (found.record is None) == bool(found.problems)
    return found.problems


# --------------------------------------------------------------------------- establishing a binary


@pytest.fixture
def canary(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator opts in to the provider canary for opencode (here, the stand-in's own "provider")."""
    monkeypatch.setenv(compat.PROVIDER_CANARY_VAR, "opencode")


def test_an_effective_binary_is_established_and_recorded_privately(tmp_path: Path, canary: None) -> None:
    runner = OpencodeRunner(binary=str(opencode(tmp_path)))
    found = compat.establish(runner, now=AT)
    record = found.record
    assert found.problems == [] and record is not None and record.established_at == "2026-01-02T03:04:05+00:00"
    assert set(record.proven) == set(compat.PROPERTIES["opencode"]) and record.cli_version == "1.0.0-stand-in"
    assert record.identity == compat.identify(runner.exe())
    path = compat.save(record)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert compat.load("opencode") == record
    assert compat.require("opencode", runner.exe()) == Path(record.identity.path)


def test_text_only_behaviour_stays_unproven_without_the_provider_canary(tmp_path: Path) -> None:
    (msg,) = problems_of(OpencodeRunner(binary=str(opencode(tmp_path))))
    assert "no effective check proves text-only" in msg and f"{compat.PROVIDER_CANARY_VAR}=opencode" in msg
    assert "stays disabled" in msg  # configuration inspection alone never establishes a binary


def test_missing_required_flag_refuses(tmp_path: Path, canary: None) -> None:
    runner = OpencodeRunner(binary=str(opencode(tmp_path, help_text="--agent <a>  --model <m>  --file <f>")))
    (msg, *_) = problems_of(runner)
    assert "lacks flags" in msg and "--pure" in msg


@pytest.mark.parametrize(
    "ignored, expected",
    [
        ("OPENCODE_DISABLE_PROJECT_CONFIG", "still loads hostile instructions"),
        ("XDG_CONFIG_HOME", "still loads hostile instructions"),
        ("OPENCODE_CONFIG_CONTENT", "does not list repowiki's agent"),
        ("OPENCODE_DB", "saved a session"),
    ],
)
def test_advertised_but_ineffective_isolation_refuses(
    tmp_path: Path, canary: None, ignored: str, expected: str
) -> None:
    """Every flag is advertised, but this build ignores one isolation input: establishing fails."""
    found = " ".join(problems_of(OpencodeRunner(binary=str(opencode(tmp_path, ignores=ignored)))))
    assert expected in found and "the adapter stays disabled" in found, found


def test_weakened_generated_config_is_caught(tmp_path: Path, canary: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Negative control on repowiki itself: drop the tool denials from the generated config and the
    effective checks fail (the agents are no longer locked and the canary's tool call runs)."""
    from repowiki import runners

    real = runners.opencode_config

    def weakened(agent: str) -> dict[str, object]:
        cfg = real(agent)
        cfg.pop("tools")
        cfg.pop("permission")
        cfg["agent"] = {agent: {"mode": "primary"}}
        return cfg

    monkeypatch.setattr(runners, "opencode_config", weakened)
    found = " ".join(problems_of(OpencodeRunner(binary=str(opencode(tmp_path)))))
    assert "has tools enabled" in found and "TOOL-RAN" in found and "not text-only" in found


def test_a_positive_control_that_does_not_load_proves_nothing(tmp_path: Path, canary: None) -> None:
    exe = opencode(tmp_path)  # a build that never reads a global or project configuration
    text = exe.read_text()
    for old, new in (
        ('layers = [load(config_home / "opencode/opencode.json")]', "layers = [{}]"),
        ('layers.append(load(Path.cwd() / "opencode.json"))', "pass"),
        ("if global_agents.exists():", "if False:"),
    ):
        assert old in text
        text = text.replace(old, new)
    exe.write_text(text)
    found = " ".join(problems_of(OpencodeRunner(binary=str(exe))))
    assert "positive control did not load" in found and "positive control (no isolation) did not show" in found
    assert "stays disabled" in found


def test_a_control_whose_model_declines_the_tool_and_instructions_proves_nothing(tmp_path: Path, canary: None) -> None:
    """A model that ignores the canary's requests must not "prove" isolation: the control has to show
    the tool running and an inherited instruction surfacing before their absence means anything."""
    exe = opencode(tmp_path)
    text = exe.read_text()
    for old, new in (
        ('if m and me["tools"]["bash"]', 'if False and m and me["tools"]["bash"]'),
        ('print("hello" + "".join("\\n" + s for s in said))', 'print("hello")'),
    ):
        assert old in text, old
        text = text.replace(old, new)
    exe.write_text(text)
    (problem, *_) = [p for p in problems_of(OpencodeRunner(binary=str(exe))) if "positive control" in p]
    assert "missing: TOOL-RAN, inherited instruction" in problem


def test_a_session_written_into_an_existing_store_is_caught(tmp_path: Path, canary: None) -> None:
    """The control creates the session database; an isolated call that appends to it adds no new file
    but still changes the store, and is refused."""
    found = " ".join(problems_of(OpencodeRunner(binary=str(opencode(tmp_path, ignores="OPENCODE_DB")))))
    assert "changed the session store" in found and "stays disabled" in found


def test_hostile_canaries_load_in_the_control_and_stay_absent_under_isolation(tmp_path: Path) -> None:
    runner = OpencodeRunner(binary=str(opencode(tmp_path)))
    home = compat.hostile_home(tmp_path / "h")
    work = tmp_path / "w"
    work.mkdir()
    control, isolated = compat.environments(runner, work, home)
    code, out = compat.run_quiet([runner.exe(), "run", "--", "hi"], control, work)
    assert code == 0 and compat.CANARY in out
    assert (home / "PLUGIN-LOADED").exists() and (home / "MCP-STARTED").exists()
    for marker in ("PLUGIN-LOADED", "MCP-STARTED"):
        (home / marker).unlink()
    argv, _ = runner.command("hi", work)
    code, out = compat.run_quiet([runner.exe(), *argv[1:]], isolated, work)
    assert code == 0 and out.strip() == "hello"
    assert not (home / "PLUGIN-LOADED").exists() and not (home / "MCP-STARTED").exists()
    assert isolated["HOME"] == str(home) and not isolated["XDG_CONFIG_HOME"].startswith(str(home))


def codex(tmp_path: Path, features: tuple[str, ...]) -> Path:
    rows = "".join(f"{f} stable true\\n" for f in features)
    text = (
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '  "--version "*) echo "codex-cli 0.0.1";;\n'
        '  "exec --help") echo "--sandbox --ephemeral --ignore-user-config --ignore-rules --disable '
        '--output-last-message --skip-git-repo-check";;\n'
        f'  "features list") printf "{rows}";;\n'
        '  *) echo "unexpected $*" >&2; exit 2;;\n'
        "esac\n"
    )
    return stand_in(tmp_path, "codex-stand-in", text)


def test_unknown_required_codex_feature_refuses(tmp_path: Path) -> None:
    known = tuple(f for f in CODEX_DISABLED_FEATURES if f != "hooks")
    runner = BACKENDS["codex"](binary=str(codex(tmp_path, known)))
    found = " ".join(problems_of(runner))
    assert "required codex feature(s) unknown to this binary: hooks" in found


@pytest.mark.parametrize("backend", ["claude", "codex"])
def test_backends_without_an_effective_inspection_stay_disabled(tmp_path: Path, backend: str) -> None:
    helps = {
        "claude": "--tools --strict-mcp-config --mcp-config --setting-sources --disable-slash-commands "
        "--no-session-persistence --output-format",
    }
    if backend == "codex":
        exe = codex(tmp_path, CODEX_DISABLED_FEATURES)
    else:
        script = f'#!/bin/sh\n[ "$1" = --version ] && echo 9.9 || echo "{helps[backend]}"\n'
        exe = stand_in(tmp_path, "claude-stand-in", script)
    found = " ".join(problems_of(BACKENDS[backend](binary=str(exe))))
    assert "no effective check proves" in found and "the adapter stays disabled" in found
    assert "lacks flags" not in found  # every flag is advertised: help text alone never establishes a binary


# --------------------------------------------------------------------------- execution rechecks the binary


def test_doctor_pass_then_replaced_binary_refuses_before_any_prompt(
    send, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = opencode(tmp_path)
    monkeypatch.setenv("REPOWIKI_OPENCODE_BIN", str(exe))
    monkeypatch.setenv(compat.PROVIDER_CANARY_VAR, "opencode")
    report = diagnose("opencode")
    assert not [p for p in report.problems if "credentials" not in p], report.problems
    assert any("established" in n for n in report.notes)
    log = tmp_path / "calls"
    monkeypatch.setenv("REPOWIKI_PASS_ENV", "STAND_IN_LOG")
    monkeypatch.setenv("STAND_IN_LOG", str(log))
    runner = OpencodeRunner(binary=str(exe))
    assert send(runner, "p") == "hello" and "run --pure" in log.read_text()
    log.unlink()
    exe.write_text(exe.read_text() + "# replaced\n")  # same path, different binary
    with pytest.raises(RunnerError, match="a changed binary"):
        send(runner, "p")
    assert not log.exists()  # zero prompts
    (tmp_path / "elsewhere").mkdir()
    other = opencode(tmp_path / "elsewhere")
    with pytest.raises(RunnerError, match="a different file"):
        send(OpencodeRunner(binary=str(other)), "p")
    assert not log.exists()


@pytest.mark.parametrize("backend", sorted(BACKENDS))
@pytest.mark.parametrize("size", [10, 200_000], ids=["argv", "file"])
def test_every_prompt_transport_path_refuses_an_unestablished_binary(
    send, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, size: int
) -> None:
    log = tmp_path / "argv"
    exe = stand_in(tmp_path, "stand-in", f'#!/bin/sh\necho "$@" > "{log}"\necho hello\n')
    runner = BACKENDS[backend](model="small", binary=str(exe))
    with pytest.raises(RunnerError, match="no established compatibility profile"):
        send(runner, "x" * size)
    assert not log.exists()
    record = compat.Established(backend, compat.identify(str(exe)), "1", ("partial",), AT.isoformat())
    compat.save(record)
    if compat.PROPERTIES[backend]:
        with pytest.raises(RunnerError, match="does not prove"):
            send(runner, "x" * size)
        assert not log.exists()
    compat.save(compat.Established(backend, compat.identify(str(exe)), "1", compat.PROPERTIES[backend], "x"))
    assert send(runner, "x" * size) == "hello"
    argv0 = log.read_text()
    assert argv0  # sent only once the binary is established and unchanged


def test_insecure_or_malformed_records_fail_closed(tmp_path: Path, canary: None) -> None:
    exe = opencode(tmp_path)
    record = compat.establish(OpencodeRunner(binary=str(exe))).record
    assert record is not None
    path = compat.save(record)
    path.chmod(0o644)
    with pytest.raises(compat.CompatibilityError, match="insecure"):
        compat.require("opencode", str(exe))
    path.chmod(0o600)
    path.write_text(json.dumps({"record": 1, "backend": "opencode"}))
    with pytest.raises(compat.CompatibilityError, match="malformed"):
        compat.require("opencode", str(exe))


# --------------------------------------------------------------------------- real binaries (opt-in)
#
# Run by the operator with REPOWIKI_INTEGRATION_<BACKEND>=/absolute/path/to/binary. The binary runs
# without network access (the `offline` fixture) and with the synthetic hostile HOME; no prompt is sent.


def offline_run(prefix: list[str]):
    def run(argv: list[str], env: dict[str, str], cwd: Path) -> tuple[int, str]:
        return compat.run_quiet([*prefix, *argv], env, cwd, timeout=600)

    return run


@pytest.mark.integration("opencode")
def test_real_opencode_is_established_only_by_effective_isolation(offline: list[str]) -> None:
    """Offline, the inspection probes must pass and text-only behaviour stays unproven (that needs the
    opt-in provider canary, which cannot run offline). Prints the identity the operator records."""
    from conftest import integration_binary

    found = compat.establish(OpencodeRunner(binary=integration_binary("opencode")), run=offline_run(offline))
    (problem,) = found.problems
    assert found.record is None and "no effective check proves text-only" in problem, found.problems
    exe = compat.identify(str(integration_binary("opencode")))
    print(f"opencode {found.cli_version} inspection probes pass; sha256 {exe.sha256 if exe else '?'}")


@pytest.mark.integration("codex")
def test_real_codex_capabilities_and_its_disabled_adapter(offline: list[str]) -> None:
    from conftest import integration_binary

    found = compat.establish(BACKENDS["codex"](binary=integration_binary("codex")), run=offline_run(offline))
    text = " ".join(found.problems)
    assert "unknown to this binary" not in text and "codex features still on" not in text, text
    assert "positive control" not in text, text
    assert found.record is None and "no effective check proves config-ignored" in text  # disabled until a probe exists


@pytest.mark.integration("claude")
def test_real_claude_stays_disabled_without_an_effective_inspection(offline: list[str]) -> None:
    from conftest import integration_binary

    found = compat.establish(BACKENDS["claude"](binary=integration_binary("claude")), run=offline_run(offline))
    assert found.record is None and "no effective check proves" in " ".join(found.problems)
