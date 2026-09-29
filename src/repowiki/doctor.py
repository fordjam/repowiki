"""`repowiki doctor`: is each backend CLI installed, recent enough for repowiki's isolation flags, and signed in?

Nothing here sends a prompt. Each check runs the CLI's own version, help and
auth-status commands with the same minimal environment a real call gets.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .runners import (
    BACKENDS,
    CODEX_DISABLED_FEATURES,
    CLIRunner,
    OllamaRunner,
    RunnerError,
    binary_for,
    parse_spec,
)

# Flags repowiki passes for isolation; a CLI whose help lacks one is too old (or too new) to trust.
REQUIRED_FLAGS: dict[str, tuple[list[str], tuple[str, ...]]] = {
    "claude": (
        ["--help"],
        ("--tools", "--strict-mcp-config", "--mcp-config", "--setting-sources", "--disable-slash-commands",
         "--no-session-persistence", "--output-format"),
    ),
    "codex": (
        ["exec", "--help"],
        ("--sandbox", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--disable", "--output-last-message",
         "--skip-git-repo-check"),
    ),
    "opencode": (["run", "--help"], ("--agent", "--pure", "--model", "--file")),
    "ollama": (["run", "--help"], ("--nowordwrap",)),
}  # fmt: skip
AUTH_COMMANDS: dict[str, list[str]] = {
    "claude": ["auth", "status"],
    "codex": ["login", "status"],
    "opencode": ["providers", "list"],
    "ollama": ["list"],
}


@dataclass
class Report:
    backend: str
    binary: str
    path: str | None = None
    version: str | None = None
    problems: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def installed(self) -> bool:
        return self.path is not None

    @property
    def ok(self) -> bool:
        return self.installed and not self.problems


def _run(argv: list[str], env: dict[str, str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env, cwd=cwd, stdin=subprocess.DEVNULL
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def diagnose(backend: str, model: str | None = None, *, allow_remote: bool = False, timeout: float = 60) -> Report:
    runner: CLIRunner = BACKENDS[backend](model=model, binary=binary_for(backend))
    report = Report(backend, runner.exe(), shutil.which(runner.exe()))
    if not report.installed:
        report.problems.append(f"`{runner.exe()}` not found on PATH (or set REPOWIKI_{backend.upper()}_BIN)")
        return report
    with tempfile.TemporaryDirectory(prefix="repowiki-doctor-") as tmp:
        cwd = Path(tmp) / "work"
        cwd.mkdir()
        env = runner.env(cwd)
        exe = report.path or runner.exe()
        code, out = _run([exe, "--version"], env, cwd, timeout)
        if code == 0 and out.strip():
            report.version = out.strip().splitlines()[0][:80]
        else:
            report.problems.append(f"`{runner.exe()} --version` failed: {out.strip()[-200:]}")
        args, flags = REQUIRED_FLAGS[backend]
        code, out = _run([exe, *args], env, cwd, timeout)
        missing = [f for f in flags if not re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", out)]
        if code != 0 or missing:
            report.problems.append(
                f"this version lacks flags repowiki needs for isolation: {', '.join(missing) or '(help failed)'}; "
                "upgrade the CLI"
            )
        if backend == "codex":
            _, feats = _run([exe, "features", "list"], env, cwd, timeout)
            unknown = [f for f in CODEX_DISABLED_FEATURES if not re.search(rf"^{f}\s", feats, re.M)]
            if unknown:
                report.notes.append(f"features not known to this codex (skipped by it or an error): {unknown}")
        _auth(report, [exe, *AUTH_COMMANDS[backend]], env, cwd, timeout)
    if isinstance(runner, OllamaRunner):
        runner.allow_remote = allow_remote
        _ollama(report, runner)
    return report


def _auth(report: Report, argv: list[str], env: dict[str, str], cwd: Path, timeout: float) -> None:
    code, out = _run(argv, env, cwd, timeout)
    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", out)
    if report.backend == "claude":
        try:
            signed_in = bool(json.loads(text).get("loggedIn"))
        except (ValueError, AttributeError):
            signed_in = code == 0 and "logged in" in text.lower()
        if not signed_in:
            report.problems.append("not signed in: run `claude auth login` (or set ANTHROPIC_API_KEY)")
        return
    if report.backend == "opencode":
        m = re.search(r"(\d+)\s+credentials?", text)
        env_keys = [k for k in os.environ if k.endswith("_API_KEY")]
        if code != 0 or (m and int(m.group(1)) == 0 and not env_keys):
            report.problems.append("no provider credentials: run `opencode auth login`")
        return
    if report.backend == "ollama":
        if code != 0:
            report.problems.append("the Ollama server is not reachable: start it with `ollama serve`")
        else:
            report.notes.append("local models: " + ", ".join(ln.split()[0] for ln in text.splitlines()[1:] if ln))
        return
    if code != 0:
        report.problems.append(f"not signed in: `{' '.join(argv[1:])}` said: {text.strip()[-200:]}")


def _ollama(report: Report, runner: OllamaRunner) -> None:
    """The same loopback, cloud-tag and /api/show checks a real run makes."""
    if not runner.model:
        report.notes.append("pass --model ollama:MODEL to check that a model is local")
        return
    try:
        runner.check()
    except RunnerError as exc:
        report.problems.append(str(exc))
    else:
        report.notes.append(f"{runner.model} is served locally by {runner.host}")


def doctor(specs: list[str], *, allow_remote: bool = False) -> list[Report]:
    """Reports for the backends named in `specs` (all CLI backends when empty)."""
    wanted: list[tuple[str, str | None]] = []
    for spec in specs or list(BACKENDS):
        backend, model = parse_spec(spec)
        if backend != "fake" and (backend, model) not in wanted:
            wanted.append((backend, model))
    reports = []
    for backend, model in wanted:
        try:
            reports.append(diagnose(backend, model, allow_remote=allow_remote))
        except RunnerError as exc:
            reports.append(Report(backend, binary_for(backend), problems=[str(exc)]))
    return reports
