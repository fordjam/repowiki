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


@dataclass
class Settings:
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

    def __post_init__(self) -> None:
        self.wiki_dir = check_wiki_dir(self.wiki_dir)
        for spec in (self.cheap, self.strong):
            if spec:
                parse_spec(spec)
        if not 1 <= self.min_pages <= self.max_pages:
            raise ValueError("need 1 <= min_pages <= max_pages")
        if not isinstance(self.exclude, list) or not all(isinstance(x, str) for x in self.exclude):
            raise ValueError("exclude must be a list of glob strings")

    @property
    def policy(self) -> Policy:
        return Policy(tuple(self.exclude))

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
        cheap = make_runner(self.cheap_spec, timeout=self.timeout)
        strong = cheap if not self.two_pass else make_runner(self.strong_spec, timeout=self.timeout)
        return cheap, strong


def load_settings(repo_root: Path, overrides: dict[str, Any] | None = None, path: Path | None = None) -> Settings:
    data: dict[str, Any] = {}
    cfg = path or repo_root / CONFIG_FILE
    if cfg.is_file():
        raw = tomllib.loads(cfg.read_text(encoding="utf-8"))
        models = raw.pop("models", {}) or {}
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
