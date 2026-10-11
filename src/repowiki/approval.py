"""Read operator approvals from protected state outside the checkout.

This module never creates approval authority. Operators install complete JSON
receipts using an atomic rename; incomplete or insecure receipts fail closed.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import CONFIG_FILE, Settings, load_settings, privacy_config
from .git import Snapshot, git_dir
from .runners import RunnerError, is_cloud_model, parse_spec

LIMITS = ("max_pages", "context_chars", "timeout", "jobs", "retries", "backoff", "budget_tokens")


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def approval_directory() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    if not base.is_absolute():
        raise ValueError("XDG_STATE_HOME must be absolute")
    return base / "repowiki/approvals"


def receipt_path(root: Path) -> Path:
    identity = str(root.resolve()) + "\0" + str(git_dir(root).resolve())
    return approval_directory() / (digest(identity.encode()) + ".json")


def _protected(path: Path, mode: int) -> None:
    info = path.lstat()
    kind = stat.S_ISDIR(info.st_mode) if mode == 0o700 else stat.S_ISREG(info.st_mode)
    if not kind or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != mode:
        raise ValueError("approval directories must be owned by the operator with mode 0700; receipts need 0600")


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate approval key")
        result[key] = value
    return result


def _receipt(root: Path) -> tuple[dict[str, Any], str]:
    path = receipt_path(root)
    admin = git_dir(root).resolve()
    if path.resolve().is_relative_to(root.resolve()) or path.resolve().is_relative_to(admin):
        raise ValueError("approval must be outside the checkout and Git administration")
    try:
        _protected(path.parent.parent, 0o700)
        _protected(path.parent, 0o700)
        _protected(path, 0o600)
        # O_NOFOLLOW also rejects substitution of a symlink after the lstat.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError("insecure approval receipt")
            raw = stream.read(1_000_001)
        if len(raw) > 1_000_000:
            raise ValueError("approval receipt is too large")
        data = json.loads(raw, object_pairs_hook=_unique)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"valid operator approval receipt required: {exc}") from exc
    keys = {"version", "root", "git_dir", "policy_commit", "config_digest", "privacy", "models",
            "endpoints", "limits", "refs", "operations"}
    if not isinstance(data, dict) or set(data) != keys or data["version"] != 1:
        raise ValueError("invalid approval receipt schema")
    return data, digest(raw)


def endpoint(spec: str) -> str:
    backend = spec.split(":", 1)[0]
    if backend == "fake":
        return "builtin"
    if backend == "ollama":
        return os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")
    raise ValueError("adapter endpoint cannot be bound to approval; an endpoint-aware adapter is required")


@dataclass(frozen=True)
class Authorization:
    root: Path
    ref: str
    operation: str
    source_commit: str
    policy_commit: str
    identity: str


def authorize(root: Path, settings: Settings, ref: str, operation: str) -> Authorization:
    """Re-read policy and receipt, then constrain this execution before any runner exists."""
    # This existing local-only refusal needs neither authority nor a server probe.
    # Keep it ahead of approval, without constructing a runner before authorization.
    if not settings.allow_remote:
        for spec in (settings.cheap_spec, settings.strong_spec):
            backend, model = parse_spec(spec)
            if backend == "ollama" and model and is_cloud_model(model):
                raise RunnerError(
                    f"prompts would leave this machine: ollama:{model} is an Ollama cloud model "
                    "(it runs on Ollama's servers). Use a local model and a loopback OLLAMA_HOST, "
                    "or pass --allow-remote.",
                    transient=False,
                )
    snap = Snapshot.at(root, ref)
    root = snap.root.resolve()
    selected = settings.policy_commit or os.environ.get("REPOWIKI_POLICY_COMMIT")
    if not selected:
        raise ValueError("explicit policy commit required (--policy-commit or REPOWIKI_POLICY_COMMIT)")
    policy_snap = Snapshot.at(root, selected)
    text = policy_snap.read(CONFIG_FILE)
    if text is None:
        raise ValueError("committed privacy configuration required")
    cfg = root / CONFIG_FILE
    if cfg.is_symlink() or not cfg.is_file() or cfg.read_bytes() != text.encode():
        raise ValueError("working privacy configuration differs from selected policy commit")
    configured = load_settings(root)
    if configured.privacy is None:
        raise ValueError("explicit privacy configuration required")
    receipt, receipt_digest = _receipt(root)
    if (receipt["root"] != str(root) or receipt["git_dir"] != str(git_dir(root).resolve())
            or receipt["policy_commit"] != policy_snap.commit or receipt["config_digest"] != digest(text.encode())
            or receipt["privacy"] != configured.privacy):
        raise ValueError("approval does not match clone, policy commit or configuration")
    for key in ("refs", "operations", "models"):
        if not isinstance(receipt[key], list) or not all(isinstance(x, str) for x in receipt[key]):
            raise ValueError("invalid approval " + key)
    if ref not in receipt["refs"] or operation not in receipt["operations"]:
        raise ValueError("unapproved ref or operation")
    effective = privacy_config(settings.privacy) if settings.privacy is not None else configured.privacy
    base = configured.privacy
    if (base["visibility"] != "public" and effective["visibility"] == "public"
            or not set(effective["allowlist"]) <= set(base["allowlist"])
            or not set(effective["context_classes"]) <= set(base["context_classes"])
            or not set(base["prompt_exclude"]) <= set(effective["prompt_exclude"])
            or not set(configured.exclude) <= set(settings.exclude)):
        raise ValueError("permissive privacy overlay requires new approval")
    for spec in (settings.cheap_spec, settings.strong_spec):
        if (spec not in receipt["models"] or not isinstance(receipt["endpoints"], dict)
                or receipt["endpoints"].get(spec) != endpoint(spec)):
            raise ValueError("unapproved model or endpoint")
    limits = receipt["limits"]
    if not isinstance(limits, dict) or set(limits) != set(LIMITS):
        raise ValueError("approval must specify all finite execution limits")
    for name in LIMITS:
        ceiling = limits[name]
        if isinstance(ceiling, bool) or not isinstance(ceiling, (int, float)) or not math.isfinite(ceiling) or ceiling < 0:
            raise ValueError("invalid approval limit: " + name)
        if name not in ("retries", "backoff") and ceiling == 0:
            raise ValueError("approval limit must be positive: " + name)
        value = getattr(settings, name)
        if name == "budget_tokens" and value is None:
            value = ceiling
            settings.budget_tokens = value
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 or value > ceiling:
            raise ValueError("execution exceeds approved limit: " + name)
        if name not in ("timeout", "backoff") and (type(value) is not int or type(ceiling) is not int):
            raise ValueError("execution limit must be an integer: " + name)
        if name not in ("retries", "backoff") and value == 0:
            raise ValueError("execution limit must be positive: " + name)
    if settings.allow_remote and not configured.allow_remote:
        raise ValueError("remote access requires approved configuration")
    settings.privacy = effective
    settings.policy_commit = policy_snap.commit
    effective_id = json.dumps({"privacy": effective, "exclude": settings.exclude}, sort_keys=True)
    identity = digest((receipt_digest + policy_snap.commit + digest(text.encode()) + effective_id).encode())
    auth = Authorization(root, ref, operation, snap.commit, policy_snap.commit, identity)
    settings._authorization = auth
    return auth


def recheck(settings: Settings, runner: Any = None) -> Authorization:
    previous = getattr(settings, "_authorization", None)
    if not isinstance(previous, Authorization):
        raise ValueError("execution requires repository authorization")
    current = authorize(previous.root, settings, previous.ref, previous.operation)
    if current != previous:
        settings._authorization = previous
        raise ValueError("approval or source changed after preparation; prepare again")
    if runner is not None:
        from .runners import FakeRunner

        # Injected fake responders cannot invoke a provider and remain fixture-friendly.
        if type(runner) is FakeRunner and "fake" in (settings.cheap_spec, settings.strong_spec):
            return current
        if not any(runner is item for item in getattr(settings, "_approved_runners", ())):
            raise ValueError("unapproved runner")
    return current
