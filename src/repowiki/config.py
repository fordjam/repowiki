"""Settings: defaults < ``.repowiki.toml`` in the repo < command-line flags."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .policy import Policy
from .runners import Runner, make_runner, parse_spec
from .safety import check_wiki_dir

CONFIG_FILE = ".repowiki.toml"
DEFAULT_MODEL = "claude"


def privacy_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("privacy must be a table")
    unknown = set(value) - {"visibility", "allowlist", "context_classes", "prompt_exclude"}
    if unknown:
        raise ValueError("unknown privacy setting(s): " + ", ".join(sorted(unknown)))
    result = {"visibility": "private", "allowlist": [], "context_classes": [], "prompt_exclude": []}
    result.update(value)
    if result["visibility"] not in ("private", "public", "unknown"):
        raise ValueError("invalid privacy visibility")
    if result["visibility"] == "unknown":
        result["visibility"] = "private"
    for key in ("allowlist", "context_classes", "prompt_exclude"):
        items = result[key]
        if not isinstance(items, list) or not all(isinstance(p, str) and p for p in items):
            raise ValueError(f"privacy.{key} must be a list of nonempty strings")
    if set(result["context_classes"]) - {"source", "readme", "instructions", "previous_wiki"}:
        raise ValueError("unknown context class")
    for pattern in result["allowlist"] + result["prompt_exclude"]:
        if pattern.startswith("/") or "\\" in pattern or any(p in ("", ".", "..") for p in pattern.split("/")):
            raise ValueError("privacy paths must be repository-relative glob patterns")
    return result


@dataclass
class Settings:
    privacy: dict[str, Any] | None = None
    policy_commit: str | None = None
    wiki_dir: str = "docs/wiki"
    repo: str | None = None  # "owner/name" written to index.json; detected when unset
    cheap: str | None = None  # planning, drafting, repairs
    strong: str | None = None  # final page pass
    min_pages: int = 4
    max_pages: int = 12
    context_chars: int = 60_000  # source excerpt budget per page prompt
    timeout: float = 900
    jobs: int = 1
    exclude: list[str] = field(default_factory=list)  # extra secret-policy globs
    min_citations: int = 1  # line citations every page must contain
    retries: int = 2  # extra attempts for a model call that times out or exits non-zero
    backoff: float = 5.0  # seconds before the first retry; doubles each time
    checkpoints: bool = True  # save finished pages in .git/repowiki and reuse them on a rerun
    resume: bool = True  # reuse existing checkpoints (False: start fresh but still save new ones)
    budget_tokens: int | None = None  # stop before prompts for one run exceed about this many tokens
    allow_remote: bool = False  # let ollama use cloud models or a non-local OLLAMA_HOST

    def __post_init__(self) -> None:
        if self.privacy is not None:
            self.privacy = privacy_config(self.privacy)
        self.wiki_dir = check_wiki_dir(self.wiki_dir)
        for spec in (self.cheap, self.strong):
            if spec:
                parse_spec(spec)
        if not 1 <= self.min_pages <= self.max_pages:
            raise ValueError("need 1 <= min_pages <= max_pages")
        if self.budget_tokens is not None and self.budget_tokens <= 0:
            raise ValueError("budget_tokens must be positive")
        if self.min_citations < 0 or self.retries < 0 or self.backoff < 0:
            raise ValueError("min_citations, retries and backoff cannot be negative")
        if not isinstance(self.exclude, list) or not all(isinstance(x, str) for x in self.exclude):
            raise ValueError("exclude must be a list of glob strings")

    @property
    def policy(self) -> Policy:
        return Policy(tuple(self.exclude))

    @property
    def context_policy(self) -> Policy:
        p = self.privacy or privacy_config({})
        denied = list(p["prompt_exclude"]) + [".repowiki.toml"]
        if "readme" not in p["context_classes"]:
            denied += ["README", "README.*"]
        if "instructions" not in p["context_classes"]:
            denied += ["AGENTS.md", "CLAUDE.md", "GEMINI.md", "SKILL.md"]
        if "previous_wiki" not in p["context_classes"]:
            denied += [self.wiki_dir + "/*"]
        allow = tuple(p["allowlist"]) if "source" in p["context_classes"] else ()
        return Policy(tuple(self.exclude + denied), allow)

    @property
    def cheap_spec(self) -> str:
        return self.cheap or self.strong or DEFAULT_MODEL

    @property
    def strong_spec(self) -> str:
        return self.strong or self.cheap or DEFAULT_MODEL

    @property
    def two_pass(self) -> bool:
        """A separate final pass only runs when the strong model differs from the cheap one."""
        return self.strong_spec != self.cheap_spec

    def runners(self) -> tuple[Runner, Runner]:
        """Both runners, checked before any model call (raises RunnerError for a refused setup)."""
        from .approval import recheck

        recheck(self)
        cheap = make_runner(self.cheap_spec, timeout=self.timeout, allow_remote=self.allow_remote)
        strong = cheap
        if self.two_pass:
            strong = make_runner(self.strong_spec, timeout=self.timeout, allow_remote=self.allow_remote)
        self._approved_runners = (cheap, strong)
        return cheap, strong


def load_settings(repo_root: Path, overrides: dict[str, Any] | None = None, path: Path | None = None) -> Settings:
    data: dict[str, Any] = {}
    # Refuse a symlinked configuration before following it: its target may be any file.
    if (repo_root / CONFIG_FILE).is_symlink() or (path is not None and path.is_symlink()):
        raise ValueError("privacy configuration must be a regular file, not a symlink")
    if path is not None and path.resolve() != (repo_root / CONFIG_FILE).resolve():
        raise ValueError("configuration must be the repository privacy configuration")
    cfg = path or repo_root / CONFIG_FILE
    if cfg.is_file():
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
        if "policy_commit" in raw:
            raise ValueError("policy_commit must be selected by the operator, outside repository configuration")
        models = raw.pop("models", {})
        if not isinstance(models, dict):
            raise ValueError("models must be a table")
        unknown_models = sorted(set(models) - {"model", "cheap", "strong"})
        if unknown_models:
            raise ValueError(f"unknown model setting(s): {', '.join(unknown_models)}")
        if "model" in models:
            data["cheap"] = data["strong"] = models["model"]
        data.update({k: v for k, v in models.items() if k in ("cheap", "strong")})
        data.update(raw)
    elif path is not None:
        raise FileNotFoundError(path)
    for key, value in (overrides or {}).items():
        if value is not None:
            data[key] = value
    known = {f.name for f in fields(Settings)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
    return Settings(**data)
