"""Model backends.

A runner turns a prompt into text. Adapters shell out to coding-assistant CLIs
you are already signed in to (subscription first, no API keys), plus a
deterministic fake for tests and offline dry runs.

Every CLI call is isolated: tools are switched off, sessions are not saved,
user customisations (MCP servers, hooks, skills, project config) are not
loaded where the CLI allows it, the process runs in an empty temporary
directory, and it gets a minimal environment instead of yours. The prompt
itself is the only repository content a backend receives.

A model is chosen with a spec string ``backend[:model]``, e.g. ``claude:haiku``,
``opencode:openrouter/some-model``, ``codex:gpt-5-mini``, ``ollama:qwen3:8b`` or
``fake``. Only the first ``:`` separates backend from model.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol


class RunnerError(RuntimeError):
    """The backend could not produce an answer."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient  # worth retrying (timeouts, non-zero exits)


class Runner(Protocol):
    @property
    def label(self) -> str: ...

    def run(self, prompt: str) -> str: ...


# --------------------------------------------------------------------------- environment

# Passed to every backend: enough to find the binary, its config/auth under
# HOME or XDG dirs, TLS roots and proxies. Nothing else from your shell.
BASE_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy",
    "SYSTEMROOT", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
)  # fmt: skip
PASS_ENV_VAR = "REPOWIKI_PASS_ENV"  # comma-separated extra variable names you choose to pass


def minimal_env(names: tuple[str, ...] = (), prefixes: tuple[str, ...] = ()) -> dict[str, str]:
    """A child environment built from an allow-list, never a copy of ``os.environ``."""
    extra = tuple(n.strip() for n in os.environ.get(PASS_ENV_VAR, "").split(",") if n.strip())
    wanted = set(BASE_ENV) | set(names) | set(extra)
    env = {k: v for k, v in os.environ.items() if k in wanted or (prefixes and k.startswith(prefixes))}
    env.update({"TERM": "dumb", "NO_COLOR": "1"})
    return env


# --------------------------------------------------------------------------- CLI adapters


@dataclass
class CLIRunner:
    """Base for adapters that run one isolated CLI process per prompt."""

    model: str | None = None
    binary: str | None = None
    timeout: float = 900

    backend: ClassVar[str] = "cli"
    default_binary: ClassVar[str] = ""
    env_names: ClassVar[tuple[str, ...]] = ()
    env_prefixes: ClassVar[tuple[str, ...]] = ()

    @property
    def label(self) -> str:
        return f"{self.backend}:{self.model}" if self.model else self.backend

    def exe(self) -> str:
        return self.binary or self.default_binary

    def available(self) -> bool:
        return shutil.which(self.exe()) is not None

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        """argv and stdin for one prompt."""
        raise NotImplementedError

    def env(self, workdir: Path) -> dict[str, str]:
        return minimal_env(self.env_names, self.env_prefixes)

    def check(self) -> None:
        """Refuse configurations that would break a privacy promise (runs before any call)."""

    def output(self, stdout: str, workdir: Path) -> str:
        return stdout

    def run(self, prompt: str) -> str:
        self.check()
        if not self.available():
            raise RunnerError(f"{self.label}: `{self.exe()}` not found on PATH", transient=False)
        with tempfile.TemporaryDirectory(prefix="repowiki-") as tmp:
            workdir = Path(tmp)
            argv, stdin = self.command(prompt, workdir)
            try:
                proc = subprocess.run(
                    argv,
                    input=stdin,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=workdir,
                    env=self.env(workdir),
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError(f"{self.label}: timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise RunnerError(f"{self.label}: {exc}", transient=False) from exc
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-600:]
                raise RunnerError(f"{self.label}: exit {proc.returncode}: {tail}")
            text = self.output(proc.stdout, workdir).strip()
        if not text:
            raise RunnerError(f"{self.label}: empty response")
        return text


class ClaudeRunner(CLIRunner):
    """Claude Code CLI in print mode with every tool, MCP server and setting source switched off."""

    backend = "claude"
    default_binary = "claude"
    env_names = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
    env_prefixes = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "AWS_", "GOOGLE_", "CLOUD_ML_", "VERTEX_")
    ISOLATION: ClassVar[tuple[str, ...]] = (
        "--tools", "",                      # no built-in tools at all
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',  # no MCP servers
        "--setting-sources", "",            # ignore user, project and local settings (hooks, plugins)
        "--disable-slash-commands",         # no skills
        "--no-session-persistence",         # nothing saved to disk
    )  # fmt: skip

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [self.exe(), "-p", "--output-format", "text", *self.ISOLATION]
        if self.model:
            argv += ["--model", self.model]
        return argv, prompt


# Linux caps a single argv string at 128 KiB; longer prompts go in an attached file.
ARG_LIMIT = 100_000
OPENCODE_AGENT = "repowiki"


def opencode_config() -> dict[str, object]:
    """Inline opencode config: a primary agent with every tool off and every permission denied."""
    return {
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
        "permission": {"*": "deny"},
        "agent": {
            OPENCODE_AGENT: {
                "mode": "primary",
                "description": "repowiki: text in, text out, no tools",
                "tools": {"*": False},
                "permission": {"*": "deny"},
            }
        },
    }


class OpencodeRunner(CLIRunner):
    """opencode ``run`` with a generated tool-free agent, no plugins and an in-memory session store."""

    backend = "opencode"
    default_binary = "opencode"
    env_names = ("OPENCODE_API_KEY",)

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [self.exe(), "run", "--pure", "--agent", OPENCODE_AGENT]
        if self.model:
            argv += ["-m", self.model]
        if len(prompt.encode()) < ARG_LIMIT:
            return argv + ["--", prompt], None
        path = workdir / "prompt.md"
        path.write_text(prompt, encoding="utf-8")
        return argv + ["-f", str(path), "--", "Follow the instructions in the attached prompt.md exactly."], None

    def env(self, workdir: Path) -> dict[str, str]:
        env = super().env(workdir)
        provider = (self.model or "").split("/", 1)[0]
        key = re.sub(r"[^A-Z0-9]", "_", provider.upper()) + "_API_KEY"
        if provider and key in os.environ:
            env[key] = os.environ[key]
        env.update(
            {
                "OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config()),
                "OPENCODE_PERMISSION": json.dumps({"*": "deny"}),
                "OPENCODE_DB": ":memory:",  # sessions are never written to disk
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                "OPENCODE_DISABLE_AUTOUPDATE": "1",
                "OPENCODE_DISABLE_SHARE": "1",
                "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
            }
        )
        return env

    def output(self, stdout: str, workdir: Path) -> str:
        text = strip_ansi(stdout)
        # `opencode run` prints a "> agent · model" banner line before the answer.
        return re.sub(r"\A\s*> \S+ · [^\n]*\n", "", text)


# Codex features that let the model act (run commands, use apps, browse); all off for every call.
CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "browser_use", "computer_use",
    "multi_agent", "image_generation", "view_image",
)  # fmt: skip


class CodexRunner(CLIRunner):
    """Codex ``exec``: read-only sandbox, ephemeral session, no user config, action features off."""

    backend = "codex"
    default_binary = "codex"
    env_names = ("CODEX_HOME", "OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL")

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [
            self.exe(), "exec", "--sandbox", "read-only", "--ephemeral", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "--color", "never", "-C", str(workdir),
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
        ]  # fmt: skip
        for feature in CODEX_DISABLED_FEATURES:
            argv += ["--disable", feature]
        argv += ["-o", str(workdir / "last.md")]
        if self.model:
            argv += ["-m", self.model]
        return argv + ["-"], prompt

    def output(self, stdout: str, workdir: Path) -> str:
        last = workdir / "last.md"
        return last.read_text(encoding="utf-8") if last.exists() else stdout


class OllamaRunner(CLIRunner):
    """Ollama: ``ollama run MODEL``, prompt on stdin. No tools; needs a model name."""

    backend = "ollama"
    default_binary = "ollama"
    env_names = ("OLLAMA_HOST",)

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        if not self.model:
            raise RunnerError("ollama needs a model, e.g. ollama:qwen3:8b", transient=False)
        return [self.exe(), "run", self.model, "--nowordwrap"], prompt

    def output(self, stdout: str, workdir: Path) -> str:
        # Reasoning models print a <think> block before the answer.
        return re.sub(r"<think>.*?</think>", "", strip_ansi(stdout), flags=re.S)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


# --------------------------------------------------------------------------- fake


@dataclass
class FakeRunner:
    """Deterministic offline backend.

    With no ``respond`` callable it answers repowiki's own prompts from the
    information inside them (file lists and excerpts), producing a small but
    valid wiki. Tests pass ``respond`` to script specific answers.
    """

    model: str | None = None
    respond: Callable[[str], str] | None = None
    calls: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"fake:{self.model}" if self.model else "fake"

    def available(self) -> bool:
        return True

    def run(self, prompt: str) -> str:
        self.calls.append(prompt)
        if self.respond is not None:
            return self.respond(prompt)
        from . import fake  # local import: only needed for offline runs

        return fake.answer(prompt)


# --------------------------------------------------------------------------- registry

BACKENDS: dict[str, type[CLIRunner]] = {
    "claude": ClaudeRunner,
    "opencode": OpencodeRunner,
    "codex": CodexRunner,
    "ollama": OllamaRunner,
}


def parse_spec(spec: str) -> tuple[str, str | None]:
    backend, _, model = spec.strip().partition(":")
    backend = backend.strip().lower()
    if backend not in BACKENDS and backend != "fake":
        known = ", ".join([*BACKENDS, "fake"])
        raise ValueError(f"unknown backend {backend!r} in {spec!r} (known: {known})")
    return backend, (model.strip() or None)


def binary_for(backend: str, binaries: dict[str, str] | None = None) -> str:
    """Executable for a backend: explicit override, then ``REPOWIKI_<NAME>_BIN``, then the default name."""
    env = os.environ.get(f"REPOWIKI_{backend.upper()}_BIN")
    return (binaries or {}).get(backend) or env or BACKENDS[backend].default_binary


def make_runner(spec: str, *, timeout: float = 900, binaries: dict[str, str] | None = None) -> Runner:
    backend, model = parse_spec(spec)
    if backend == "fake":
        return FakeRunner(model)
    return BACKENDS[backend](model=model, binary=binary_for(backend, binaries), timeout=timeout)
