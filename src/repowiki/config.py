"""Settings: defaults < ``.repowiki.toml`` in the repo < command-line flags."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

from .policy import Policy
from .runners import Runner, make_runner, parse_spec
from .safety import check_wiki_dir
from .steering import Steering, load_steering

CONFIG_FILE = ".repowiki.toml"
DEFAULT_MODEL = "claude"

PRESETS_VERSION = 1
KIB = 1024


@dataclass(frozen=True)
class Preset:
    """A named set of ceilings (``effort``). It sets limits only; it does not certify page quality."""

    name: str
    version: int
    max_pages: int  # page ceiling
    context_bytes: int  # source excerpts per page prompt, in UTF-8 bytes (the KiB ceiling)
    budget_tokens: int  # run cap, in estimated input tokens (characters / 4)
    final_pass: bool  # True: an approved final pass always runs (on the strong model, else the cheap one)
    needs_strong: bool  # True: refuse without a separate strong model for the final pass

    @property
    def context_chars(self) -> int:
        """The character budget matching the byte ceiling: a character is at least one UTF-8 byte."""
        return self.context_bytes


PRESETS: dict[str, Preset] = {
    p.name: p
    for p in (
        Preset("draft", PRESETS_VERSION, 12, 32 * KIB, 50_000, final_pass=False, needs_strong=False),
        Preset("standard", PRESETS_VERSION, 20, 64 * KIB, 150_000, final_pass=True, needs_strong=False),
        Preset("deep", PRESETS_VERSION, 30, 128 * KIB, 400_000, final_pass=True, needs_strong=True),
    )
}
PRESET_LIMITS = ("max_pages", "context_chars", "context_bytes", "budget_tokens")


def preset(effort: str) -> Preset:
    """The preset named ``draft``, ``standard`` or ``deep``, optionally pinned as ``name@version``."""
    name, pinned, version = effort.partition("@") if isinstance(effort, str) else ("", "", "")
    found = PRESETS.get(name)
    if found is None or (pinned and version != str(found.version)):
        known = ", ".join(f"{n} (or {n}@{p.version})" for n, p in PRESETS.items())
        raise ValueError(f"unknown effort preset {effort!r}; known presets: {known}")
    return found


def resolve_effort(data: dict[str, Any]) -> dict[str, Any]:
    """Apply ``data["effort"]`` before explicit settings: a preset fills each limit left unset, and an
    explicit value may only tighten it. Returns a new dict; ``data`` is unchanged."""
    data = dict(data)
    if data.get("effort") is None:
        data.pop("effort", None)
        return data
    chosen = preset(data["effort"])
    for name in PRESET_LIMITS:
        ceiling = getattr(chosen, name)
        value = data.get(name)
        if value is None:
            data[name] = ceiling
        elif isinstance(value, bool) or not isinstance(value, int) or value > ceiling:
            raise ValueError(
                f"{name} = {value!r} is above the {chosen.name} preset's ceiling of {ceiling:,}; "
                "explicit settings can only tighten a preset (choose a larger preset or a smaller value)"
            )
    if not chosen.final_pass:  # one pass: the final-pass model is the drafting model
        single = data.get("cheap") or data.get("strong") or DEFAULT_MODEL
        data["cheap"] = data["strong"] = single
    return data


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
    wiki_ref: str | None = None  # operator-selected committed previous wiki (update defaults to main)
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
    context_bytes: int | None = None  # UTF-8 byte ceiling on a page prompt's source excerpts
    allow_remote: bool = False  # let ollama use cloud models or a non-local OLLAMA_HOST
    effort: str | None = None  # draft | standard | deep (see PRESETS); applied by resolve_effort

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
        if self.context_bytes is not None and (
            isinstance(self.context_bytes, bool) or not isinstance(self.context_bytes, int) or self.context_bytes <= 0
        ):
            raise ValueError("context_bytes must be a positive integer")
        if self.min_citations < 0 or self.retries < 0 or self.backoff < 0:
            raise ValueError("min_citations, retries and backoff cannot be negative")
        if not isinstance(self.exclude, list) or not all(isinstance(x, str) for x in self.exclude):
            raise ValueError("exclude must be a list of glob strings")
        if self.effort is not None:
            self._check_preset(preset(self.effort))

    def _check_preset(self, chosen: Preset) -> None:
        """The preset's ceilings and pass rule hold, however these settings were built."""
        for name in PRESET_LIMITS:
            value = getattr(self, name)
            if value is None or value > getattr(chosen, name):
                raise ValueError(f"{name} is not within the {chosen.name} preset; resolve the preset first")
        if not chosen.final_pass and self.separate_strong:
            raise ValueError(f"the {chosen.name} preset is one pass: it uses no separate final-pass model")
        if chosen.needs_strong and not self.separate_strong:
            raise ValueError(
                f"the {chosen.name} preset needs an approved strong model for its final pass, different from "
                "the cheap model (set --strong or [models] strong); none is configured"
            )

    @property
    def effort_preset(self) -> Preset | None:
        return preset(self.effort) if self.effort is not None else None

    def check_steering(self, repo_root: Path) -> None:
        """Refuse a page ceiling below the steering file's exact page count: steered pages are never trimmed."""
        steering = self.load_steering(repo_root)
        if steering is not None and len(steering.pages) > self.max_pages:
            source = f"the {self.effort} preset" if self.effort else "max_pages"
            raise ValueError(
                f"the steering file names exactly {len(steering.pages)} pages, but {source} allows at most "
                f"{self.max_pages}; steered pages are never trimmed (choose a larger preset or page ceiling)"
            )

    def effective(self) -> list[str]:
        """The settings a run uses, one labelled line each (preview and dry-run output)."""
        chosen = self.effort_preset
        if chosen is None:
            effort = "none (individual settings)"
        else:
            effort = f"{chosen.name} (presets v{chosen.version}; sets limits only, not a quality guarantee)"
        cap = (
            f"{self.budget_tokens:,} estimated input tokens (checked before every call)"
            if self.budget_tokens is not None
            else "none"
        )
        final = "none (one pass)"
        if self.two_pass:
            final = self.strong_spec if self.separate_strong else f"{self.strong_spec} (the drafting model)"
        context = f"at most {self.context_chars:,} characters"
        if self.context_bytes is not None:
            context += f" and {self.context_bytes:,} UTF-8 bytes"
        return [
            f"effort: {effort}",
            f"pages: {self.min_pages} to {self.max_pages}",
            f"source context: {context} per page prompt",
            f"run cap: {cap}",
            f"models: drafting {self.cheap_spec}; final pass {final}",
            f"per call: timeout {self.timeout:g} s, up to {self.retries} retries; {self.jobs} page(s) at a time",
        ]

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

    def load_steering(self, repo_root: Path) -> Steering | None:
        """The repository's validated ``.repowiki/wiki.json`` (None when absent), checked against ``exclude`` too."""
        return load_steering(repo_root, self.policy)

    @property
    def cheap_spec(self) -> str:
        return self.cheap or self.strong or DEFAULT_MODEL

    @property
    def strong_spec(self) -> str:
        return self.strong or self.cheap or DEFAULT_MODEL

    @property
    def separate_strong(self) -> bool:
        return self.strong_spec != self.cheap_spec

    @property
    def two_pass(self) -> bool:
        """A final pass runs when the strong model differs from the cheap one, or when the effort preset
        requires an approved final pass (then on the one configured, approved model)."""
        chosen = self.effort_preset
        return self.separate_strong or (chosen is not None and chosen.final_pass)

    def describe_runners(self) -> tuple[Runner, Runner]:
        """Both runners described from their specs, unverified and inert: for previews and dry runs.

        Nothing is checked, discovered or contacted; such a runner is never approved to send
        (only ``runners()`` records approved runners).
        """
        cheap = make_runner(self.cheap_spec, timeout=self.timeout, allow_remote=self.allow_remote, verify=False)
        strong = cheap
        if self.two_pass:
            strong = make_runner(self.strong_spec, timeout=self.timeout, allow_remote=self.allow_remote, verify=False)
        return cheap, strong

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
        if "policy_commit" in raw or "wiki_ref" in raw:
            raise ValueError(
                "policy_commit and wiki_ref must be selected by the operator, outside repository configuration"
            )
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
    settings = Settings(**resolve_effort(data))
    if settings.effort is not None:
        settings.check_steering(repo_root)
    return settings
