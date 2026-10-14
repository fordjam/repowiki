"""Model backends.

A runner turns a prompt into text. Adapters shell out to coding-assistant CLIs
you are already signed in to (subscription first, no API keys), plus a
deterministic fake for tests and offline dry runs.

Every CLI call is isolated: tools are switched off, sessions are not saved,
user customisations (MCP servers, hooks, skills, project config) are not
loaded where the CLI allows it, the process runs in an empty temporary
directory, and it gets a minimal environment instead of yours. The prompt
itself is the only repository content a backend receives, and it arrives only
as a sealed envelope released by the outbound gateway (``outbound.dispatch``):
a CLI runner refuses a plain string.

A model is chosen with a spec string ``backend[:model]``, e.g. ``claude:haiku``,
``opencode:openrouter/some-model``, ``codex:gpt-5-mini``, ``ollama:qwen3:8b`` or
``fake``. Only the first ``:`` separates backend from model.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, Protocol
from urllib.parse import urlsplit

from .outbound import Envelope, accept, released


class RunnerError(RuntimeError):
    """The backend could not produce an answer."""

    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient  # worth retrying (timeouts, non-zero exits)


class Runner(Protocol):
    @property
    def label(self) -> str: ...

    def run(self, envelope: Envelope) -> str: ...


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

    def run(self, envelope: Envelope) -> str:
        prompt = accept(self, envelope)  # refuses strings and envelopes not released by the gateway
        self.check()
        if not self.available():
            raise RunnerError(f"{self.label}: `{self.exe()}` not found on PATH", transient=False)
        from .compatibility import CompatibilityError, require

        try:  # the exact binary `repowiki doctor` established, unchanged (rw-P24)
            verified = require(self.backend, self.exe())
        except CompatibilityError as exc:
            raise RunnerError(f"{self.label}: {exc}", transient=False) from None
        with tempfile.TemporaryDirectory(prefix="repowiki-") as tmp:
            # The process runs in an empty `work` directory; its parent is private to repowiki
            # (generated config, session stores) and never the model's working directory.
            workdir = Path(tmp) / "work"
            workdir.mkdir()
            argv, stdin = self.command(prompt, workdir)
            argv[0] = str(verified)  # run the file that was verified, not a fresh PATH lookup
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

    def env(self, workdir: Path) -> dict[str, str]:
        env = super().env(workdir)
        env.update(
            {
                "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # no CLAUDE.md files from your home directory
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",  # no auto-memory in or out
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            }
        )
        return env

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [self.exe(), "-p", "--output-format", "text", *self.ISOLATION]
        if self.model:
            argv += ["--model", self.model]
        return argv, prompt


# Linux caps a single argv string at 128 KiB; longer prompts go in an attached file.
ARG_LIMIT = 100_000

# Every OpenCode permission, denied; every built-in tool, disabled.
OPENCODE_TOOLS = (
    "bash", "edit", "write", "read", "grep", "glob", "list", "patch", "multiedit", "webfetch", "websearch",
    "codesearch", "task", "todowrite", "todoread", "skill", "lsp", "question",
)  # fmt: skip
OPENCODE_DENY: dict[str, str] = {
    "*": "deny",
    **{t: "deny" for t in OPENCODE_TOOLS},
    "external_directory": "deny",
    "doom_loop": "deny",
}
OPENCODE_BUILTIN_AGENTS = ("build", "plan", "general", "explore")


def opencode_agent_name(workdir: Path) -> str:
    """A private agent name per call, derived from the random temp directory: no config layer can name it."""
    return "repowiki-" + hashlib.sha256(str(workdir.resolve()).encode()).hexdigest()[:16]


def opencode_config(agent: str) -> dict[str, object]:
    """The config each opencode call runs with.

    opencode merges its config layers and applies an agent's own permissions
    after the global ones, so global denials alone can be undone by an inherited
    ``agent.<name>.permission``. repowiki therefore runs a private agent whose
    name is random per call, with every permission denied and every tool off,
    passed through ``OPENCODE_CONFIG_CONTENT`` (the highest layer a user can
    set). The built-in agents are locked the same way in case one is selected.
    """
    tools_off: dict[str, bool] = {"*": False, **{t: False for t in OPENCODE_TOOLS}}
    locked: dict[str, object] = {"permission": dict(OPENCODE_DENY), "tools": dict(tools_off)}
    return {
        "$schema": "https://opencode.ai/config.json",
        "default_agent": agent,
        "agent": {
            agent: {"mode": "primary", "description": "repowiki: text in, text out, no tools", **locked},
            **{name: dict(locked) for name in OPENCODE_BUILTIN_AGENTS},
        },
        "permission": dict(OPENCODE_DENY),
        "tools": dict(tools_off),
        "mcp": {},
        "plugin": [],
        "instructions": [],
        "share": "disabled",
        "autoupdate": False,
        "snapshot": False,
    }


class OpencodeRunner(CLIRunner):
    """opencode ``run`` as a private, locked agent, with your global and project config not loaded."""

    backend = "opencode"
    default_binary = "opencode"
    env_names = ("OPENCODE_API_KEY",)

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        agent = opencode_agent_name(workdir)
        argv = [self.exe(), "run", "--pure", "--agent", agent, "--dir", str(workdir)]
        if self.model:
            argv += ["-m", self.model]
        if len(prompt.encode()) < ARG_LIMIT:
            return argv + ["--", prompt], None
        path = workdir.parent / "prompt.md"  # outside the model's working directory
        path.write_text(prompt, encoding="utf-8")
        return argv + ["-f", str(path), "--", "Follow the instructions in the attached prompt.md exactly."], None

    def env(self, workdir: Path) -> dict[str, str]:
        env = super().env(workdir)
        provider = (self.model or "").split("/", 1)[0]
        key = re.sub(r"[^A-Z0-9]", "_", provider.upper()) + "_API_KEY"
        if provider and key in os.environ:
            env[key] = os.environ[key]
        private = workdir.parent
        (private / "xdg-config").mkdir(exist_ok=True)
        env.update(
            {
                "OPENCODE_CONFIG_CONTENT": json.dumps(opencode_config(opencode_agent_name(workdir))),
                "OPENCODE_PERMISSION": json.dumps(OPENCODE_DENY),
                # An empty config home: your global opencode.json (agents, MCP servers, plugins,
                # instructions) is not loaded. Sign-ins live in the data dir and still work.
                "XDG_CONFIG_HOME": str(private / "xdg-config"),
                "OPENCODE_DB": ":memory:",  # sessions are never written to disk
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_PURE": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
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


def ollama_endpoint(raw: str) -> str | None:
    """Resolve an OLLAMA_HOST value to one canonical ``scheme://host:port`` URL, with Ollama's own
    defaults (no scheme: http on 11434; ``http://`` without a port: 80; ``https://``: 443).

    The same string is used for the locality check, the ``/api/show`` check and the ``ollama``
    subprocess, so the server that is verified is the server that is used. Values Ollama would
    read in ways repowiki does not reproduce (a path, query, credentials, another scheme) give None.
    """
    value = raw.strip()
    if not value:
        return "http://127.0.0.1:11434"
    scheme, sep, rest = value.partition("://")
    if not sep:
        scheme, rest, default_port = "http", value, 11434
    elif scheme == "http":
        default_port = 80
    elif scheme == "https":
        default_port = 443
    else:
        return None
    parsed = urlsplit(f"{scheme}://{rest}")
    try:
        hostname = parsed.hostname
        port = parsed.port if parsed.port is not None else default_port
    except ValueError:
        return None
    if not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return None
    if parsed.path not in ("", "/"):
        return None
    literal = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{literal}:{port}"


def is_loopback(endpoint: str) -> bool:
    """True only for a loopback IP literal (127.0.0.0/8, ::1), the unspecified address, or the name
    ``localhost`` resolving only to loopback addresses. No other name is resolved or trusted, so
    ``127.remote.example`` is refused."""
    hostname = urlsplit(endpoint).hostname or ""
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        if hostname.lower() != "localhost":
            return False
        try:
            infos = socket.getaddrinfo("localhost", None)
        except OSError:
            return False
        addresses = {ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos}
        return bool(addresses) and all(a.is_loopback for a in addresses)
    return ip.is_loopback or ip.is_unspecified


def ollama_host_is_local(host: str) -> bool:
    endpoint = ollama_endpoint(host)
    return endpoint is not None and is_loopback(endpoint)


def is_cloud_model(model: str) -> bool:
    """Ollama cloud models are tagged ``cloud``, ``*-cloud`` or ``cloud-*`` (e.g. ``gpt-oss:120b-cloud``)."""
    name, _, tag = model.lower().partition(":")
    return tag == "cloud" or tag.endswith("-cloud") or tag.startswith("cloud-") or name.endswith("-cloud")


# The reviewed /api/show metadata profiles a model must match before any prompt is sent. This is a
# deliberately narrow contract about what the local server reports, not proof of the model's identity.
# Recorded against a synthetic fixture shaped like Ollama 0.12's report for a pulled GGUF model; other
# formats are refused until a profile for them is reviewed and added here.
OLLAMA_FORMATS = ("gguf",)
OLLAMA_DETAIL_STRINGS = ("format", "family", "parameter_size", "quantization_level")
OLLAMA_REMOTE_FIELDS = ("remote_host", "remote_model")


class OllamaReportError(ValueError):
    """The server's /api/show report does not match an accepted metadata profile."""


def ollama_profile_problem(report: object) -> str | None:
    """Why an ``/api/show`` report fails the accepted local-model profile, or None when it matches.

    Accepted: a ``details`` object whose ``format`` (one of ``OLLAMA_FORMATS``), ``family``,
    ``parameter_size`` and ``quantization_level`` are non-empty strings and whose ``families`` is a
    non-empty list of non-empty strings; a non-empty ``model_info`` object; and ``remote_host`` /
    ``remote_model`` absent or empty strings. Anything else (``{}``, wrong types, an unknown format,
    a remote field) is refused."""
    if not isinstance(report, dict) or not report:
        return "the report is empty or not a JSON object"
    for key in OLLAMA_REMOTE_FIELDS:
        value = report.get(key, "")
        if not isinstance(value, str):
            return f'"{key}" is not a string'
        if value:
            return f"the model runs remotely ({key}={value!r})"
    details = report.get("details")
    if not isinstance(details, dict):
        return '"details" is missing or not an object'
    for key in OLLAMA_DETAIL_STRINGS:
        value = details.get(key)
        if not isinstance(value, str) or not value.strip():
            return f'"details.{key}" is missing or not a non-empty string'
    if details["format"] not in OLLAMA_FORMATS:
        return f'unsupported model format {details["format"]!r} (accepted: {", ".join(OLLAMA_FORMATS)})'
    families = details.get("families")
    if not isinstance(families, list) or not families or not all(isinstance(f, str) and f.strip() for f in families):
        return '"details.families" is missing or not a non-empty list of strings'
    info = report.get("model_info")
    if not isinstance(info, dict) or not info:
        return '"model_info" is missing or not a non-empty object'
    return None


def _ollama_request(base_url: str, model: str, timeout: float) -> object:
    """POST /api/show on the already-validated loopback server, bypassing any proxy: the raw JSON."""
    request = urllib.request.Request(
        f"{base_url}/api/show",
        data=json.dumps({"model": model}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _ollama_show(base_url: str, model: str, timeout: float = 10.0) -> dict[str, object]:
    """The model's ``/api/show`` report, only if it matches an accepted profile (``ollama_profile_problem``);
    otherwise ``OllamaReportError``. Nothing is cached: every call asks the server again."""
    report = _ollama_request(base_url, model, timeout)
    problem = ollama_profile_problem(report)
    if problem:
        raise OllamaReportError(problem)
    assert isinstance(report, dict)
    return report


@dataclass
class OllamaRunner(CLIRunner):
    """Ollama: ``ollama run MODEL``, prompt on stdin. No tools; a verified local model on a loopback server."""

    allow_remote: bool = False
    host: str | None = None  # the canonical OLLAMA_HOST, selected by the first check()

    backend: ClassVar[str] = "ollama"
    default_binary: ClassVar[str] = "ollama"

    def check(self) -> None:
        """Refuse anything that would leave this machine (unless allowed). Runs before every prompt:
        nothing is cached, so a server whose report changes is refused on the next call.

        Checks: the host is a loopback address (and the same endpoint the first check selected); the
        model has no cloud tag; and the server's ``/api/show`` report for this model matches an
        accepted local profile (``ollama_profile_problem``), with no ``remote_host``/``remote_model``.
        """
        if not self.model:
            raise RunnerError("ollama needs a model, e.g. ollama:qwen3:8b", transient=False)
        raw = os.environ.get("OLLAMA_HOST", "")
        endpoint = ollama_endpoint(raw)
        selected = endpoint if endpoint is not None else raw
        if self.host is not None and selected != self.host:
            raise RunnerError(
                f"OLLAMA_HOST changed from {self.host!r} to {raw!r} after ollama:{self.model} was verified; "
                "start a new run to use another server",
                transient=False,
            )
        problems = []
        if endpoint is None:
            problems.append(f"OLLAMA_HOST={raw!r} is not a plain scheme://host:port address")
        elif not is_loopback(endpoint):
            problems.append(f"OLLAMA_HOST={raw!r} is not a loopback address")
        if is_cloud_model(self.model):
            problems.append(f"ollama:{self.model} is an Ollama cloud model (it runs on Ollama's servers)")
        if problems and not self.allow_remote:
            raise RunnerError(
                "prompts would leave this machine: " + "; ".join(problems) + ". Use a local model and a "
                "loopback OLLAMA_HOST, or pass --allow-remote.",
                transient=False,
            )
        if endpoint is None:  # only with --allow-remote: pass the value through untouched
            self.host = raw
            return
        if not self.allow_remote:
            try:
                info = _ollama_show(endpoint, self.model)
            except OllamaReportError as exc:
                raise RunnerError(
                    f"refusing ollama:{self.model} at {endpoint}: its /api/show report does not match an accepted "
                    f"local model profile: {exc}",
                    transient=False,
                ) from None
            except urllib.error.HTTPError as exc:
                hint = f" (run `ollama pull {self.model}`)" if exc.code == 404 else ""
                raise RunnerError(
                    f"could not verify ollama:{self.model} at {endpoint}: HTTP {exc.code}{hint}", transient=False
                ) from None
            except (OSError, ValueError) as exc:
                raise RunnerError(
                    f"could not reach the local Ollama server at {endpoint} to verify ollama:{self.model}: {exc}",
                    transient=False,
                ) from None
            remote = info.get("remote_host") or info.get("remote_model")
            if remote:
                raise RunnerError(
                    f"ollama reports that {self.model} runs remotely ({remote}); pass --allow-remote to allow it",
                    transient=False,
                )
        self.host = endpoint

    def env(self, workdir: Path) -> dict[str, str]:
        env = super().env(workdir)
        if self.host:
            env["OLLAMA_HOST"] = self.host  # exactly the endpoint that was verified
        return env

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        # `run` verified the endpoint and model immediately before this (``check``, never cached).
        if self.host is None:
            raise RunnerError(f"{self.label}: not verified", transient=False)
        assert self.model
        return [self.exe(), "run", self.model, "--nowordwrap"], prompt

    def output(self, stdout: str, workdir: Path) -> str:
        # Reasoning models print a <think> block before the answer.
        return re.sub(r"<think>.*?</think>", "", strip_ansi(stdout), flags=re.S)


_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


# --------------------------------------------------------------------------- fake


@dataclass(frozen=True)
class TraceEntry:
    """One fake-backend call: the stage, the origins it carried and the exact prompt text."""

    stage: str | None  # None: a plain string, not sent through the outbound gateway
    origins: tuple[str, ...]
    prompt: str
    sealed: bool


@dataclass
class FakeRunner:
    """Deterministic offline backend.

    With no ``respond`` callable it answers repowiki's own prompts from the
    information inside them (file lists and excerpts), producing a small but
    valid wiki. Tests pass ``respond`` to script specific answers.

    ``trace`` records every call completely. Production stages arrive as
    envelopes released by the outbound gateway (``sealed``); a plain string is
    accepted only for direct use of the fake and is recorded as unsealed.
    """

    model: str | None = None
    respond: Callable[[str], str] | None = None
    calls: list[str] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"fake:{self.model}" if self.model else "fake"

    @property
    def cache_id(self) -> str:
        """Checkpoint identity: scripted fakes never share checkpoints with each other."""
        return self.label if self.respond is None else f"{self.label}:{id(self.respond)}"

    def available(self) -> bool:
        return True

    def run(self, envelope: Envelope | str) -> str:
        if isinstance(envelope, Envelope):
            prompt = accept(self, envelope)
            self.trace.append(TraceEntry(envelope.stage, envelope.origins, prompt, released(self, envelope)))
        else:
            prompt = envelope
            self.trace.append(TraceEntry(None, (), prompt, False))
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


def make_runner(
    spec: str,
    *,
    timeout: float = 900,
    binaries: dict[str, str] | None = None,
    allow_remote: bool = False,
    verify: bool = True,
) -> Runner:
    """A runner for `spec`, checked up front (e.g. Ollama must be local unless `allow_remote`).

    With ``verify=False`` the runner is only described: nothing is checked, discovered or contacted
    (no ``ollama`` /api/show request), which is what ``--preview`` and ``--dry-run`` use. Verification
    still runs before any prompt is sent, because ``run`` checks again.
    """
    backend, model = parse_spec(spec)
    if backend == "fake":
        return FakeRunner(model)
    runner = BACKENDS[backend](model=model, binary=binary_for(backend, binaries), timeout=timeout)
    if isinstance(runner, OllamaRunner):
        runner.allow_remote = allow_remote
    if verify:
        runner.check()
    return runner
