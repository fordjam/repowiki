"""Model backends.

A runner turns a prompt into text. Adapters shell out to coding-assistant CLIs
you are already signed in to (subscription first, no API keys), plus a
deterministic fake for tests and offline dry runs.

A model is chosen with a spec string ``backend[:model]``, e.g. ``claude:haiku``,
``opencode:openrouter/some-model``, ``codex:gpt-5-mini``, ``ollama:qwen3:8b`` or
``fake``. Only the first ``:`` separates backend from model.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class RunnerError(RuntimeError):
    """The backend could not produce an answer."""


class Runner(Protocol):
    @property
    def label(self) -> str: ...

    def run(self, prompt: str) -> str: ...


@dataclass
class CLIRunner:
    """Base for adapters that run one CLI process per prompt."""

    model: str | None = None
    binary: str | None = None
    timeout: float = 900
    cwd: Path | None = None
    extra_args: list[str] = field(default_factory=list)

    backend = "cli"
    default_binary = ""

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

    def output(self, stdout: str, workdir: Path) -> str:
        return stdout

    def run(self, prompt: str) -> str:
        if not self.available():
            raise RunnerError(f"{self.label}: `{self.exe()}` not found on PATH")
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
                    cwd=self.cwd or workdir,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                raise RunnerError(f"{self.label}: timed out after {self.timeout:.0f}s") from exc
            except OSError as exc:
                raise RunnerError(f"{self.label}: {exc}") from exc
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "").strip()[-600:]
                raise RunnerError(f"{self.label}: exit {proc.returncode}: {tail}")
            text = self.output(proc.stdout, workdir).strip()
        if not text:
            raise RunnerError(f"{self.label}: empty response")
        return text


class ClaudeRunner(CLIRunner):
    """Claude Code CLI: ``claude -p [--model M]``, prompt on stdin."""

    backend = "claude"
    default_binary = "claude"

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [self.exe(), "-p", "--output-format", "text"]
        if self.model:
            argv += ["--model", self.model]
        return argv + self.extra_args, prompt


# Linux caps a single argv string at 128 KiB; longer prompts go in an attached file.
ARG_LIMIT = 100_000


class OpencodeRunner(CLIRunner):
    """opencode: ``opencode run [-m provider/model] MESSAGE``."""

    backend = "opencode"
    default_binary = "opencode"

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [self.exe(), "run"]
        if self.model:
            argv += ["-m", self.model]
        argv += self.extra_args
        if len(prompt.encode()) < ARG_LIMIT:
            return argv + [prompt], None
        path = workdir / "prompt.md"
        path.write_text(prompt, encoding="utf-8")
        return argv + ["-f", str(path), "--", "Follow the instructions in the attached prompt.md exactly."], None

    def output(self, stdout: str, workdir: Path) -> str:
        return strip_ansi(stdout)


class CodexRunner(CLIRunner):
    """Codex CLI: ``codex exec [-m M] -`` in a read-only sandbox; reads the last message file."""

    backend = "codex"
    default_binary = "codex"

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        argv = [
            self.exe(), "exec", "--sandbox", "read-only", "--skip-git-repo-check",
            "--ephemeral", "--color", "never", "-o", str(workdir / "last.md"),
        ]  # fmt: skip
        if self.model:
            argv += ["-m", self.model]
        return argv + self.extra_args + ["-"], prompt

    def output(self, stdout: str, workdir: Path) -> str:
        last = workdir / "last.md"
        return last.read_text(encoding="utf-8") if last.exists() else stdout


class OllamaRunner(CLIRunner):
    """Ollama: ``ollama run MODEL``, prompt on stdin. Local and free; needs a model name."""

    backend = "ollama"
    default_binary = "ollama"

    def command(self, prompt: str, workdir: Path) -> tuple[list[str], str | None]:
        if not self.model:
            raise RunnerError("ollama needs a model, e.g. ollama:qwen3:8b")
        return [self.exe(), "run", self.model, "--nowordwrap", *self.extra_args], prompt

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


def make_runner(spec: str, *, timeout: float = 900, binaries: dict[str, str] | None = None) -> Runner:
    backend, model = parse_spec(spec)
    if backend == "fake":
        return FakeRunner(model)
    cls = BACKENDS[backend]
    binary = (binaries or {}).get(backend) or os.environ.get(f"REPOWIKI_{backend.upper()}_BIN")
    return cls(model=model, binary=binary, timeout=timeout)
