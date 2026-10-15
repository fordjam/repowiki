"""The outbound gateway: the only way prompt text reaches a model backend.

A prompt is built from origin-tagged ``Section``s (repowiki's own template text,
source excerpts, file paths, the README, previous wiki pages, planner replies,
drafts, validation messages ...). ``seal`` turns them into an immutable
``Envelope`` under the current authorization:

1. every section must carry a known origin, and the settings must carry an
   approved privacy policy (``recheck``); otherwise nothing is built;
2. a section that names a path the prompt policy denies is refused: callers
   filter paths before reading, so such a section means a caller skipped that;
3. the complete serialized prompt is redacted (credential shapes, quoted
   secret literals of any length, private-key blocks; lines stay in place);
4. names of repository files the prompt policy denies are replaced wherever
   they appear (page titles, planner replies, previous pages, validation
   messages, cached replies), so excluded paths never leave as metadata either.

``dispatch`` hands a sealed envelope to one approved runner, after rechecking
the authorization. Production runners accept only an envelope released to them
by ``dispatch`` (``accept``) and refuse plain strings, so a retry or another
caller cannot reach a backend around the gateway.
"""

from __future__ import annotations

import contextvars
import hashlib
import re
import threading
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .policy import Policy, prompt_policy, redact

if TYPE_CHECKING:
    from .config import Settings

# Where a piece of prompt text came from.
ORIGINS = frozenset({
    "template",       # repowiki's own instructions and headings
    "path",           # repository file names (tree, manifests, entry points, tests, rename notes)
    "readme",         # README text
    "source",         # line-numbered source excerpts
    "outline",        # titles and file names of the other wiki pages
    "planner_reply",  # titles and summaries a planner reply (or its checkpoint) produced
    "previous_wiki",  # the previous wiki: page text, titles, summaries
    "draft",          # a model draft being revised or repaired
    "validation",     # validation messages for a repair
    "retry",          # the note added when a reply could not be used
})  # fmt: skip
STAGES = frozenset({"plan", "draft", "revise", "repair"})
EXCLUDED_PATH = "[excluded path]"


class OutboundError(ValueError):
    """A prompt or a model call was refused by the outbound gateway."""


@dataclass(frozen=True)
class Section:
    origin: str
    text: str
    paths: tuple[str, ...] = ()  # repository paths whose content or name this text carries


def template(text: str) -> Section:
    return Section("template", text)


_MINT = object()


@dataclass(frozen=True)
class Envelope:
    """A sealed prompt: filtered, redacted and bound to one authorization. Only ``seal`` makes one."""

    stage: str
    prompt: str
    origins: tuple[str, ...]
    identity: str
    token: object = field(default=None, repr=False, compare=False)
    manifest: tuple[tuple[str, tuple[str, ...]], ...] = ()  # (origin, repository paths it carried)

    def __post_init__(self) -> None:
        if self.token is not _MINT:
            raise OutboundError("prompt envelopes are created only by the outbound gateway")

    @property
    def data(self) -> bytes:
        """The exact bytes a backend receives."""
        return self.prompt.encode("utf-8")

    @property
    def size(self) -> int:
        return len(self.data)

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def outbound_policy(settings: Settings) -> Policy:
    """What may reach a prompt: the context policy plus every prompt-only default not approved."""
    privacy = settings.privacy or {}
    return prompt_policy(
        settings.context_policy, privacy.get("context_classes", ()), privacy.get("allowlist", ()), settings.wiki_dir
    )


_excluded_cache: dict[tuple[str, str, Policy], re.Pattern[str] | None] = {}
_excluded_lock = threading.Lock()


def excluded_paths(root: Path, commit: str, policy: Policy) -> frozenset[str]:
    """Repository files at `commit` that `policy` keeps out of prompts (names only; nothing is read)."""
    from .git import Snapshot

    return frozenset(f for f in Snapshot.at(root, commit).files if policy.secret(f))


def _variants(path: str) -> set[str]:
    """The spellings a reference to `path` may take: Unicode normalization forms and URL-escaped spaces."""
    out: set[str] = set()
    for form in ("NFC", "NFD"):
        name = unicodedata.normalize(form, path).lower()
        out |= {name, name.replace(" ", "%20")}
    return out


def _trie(words: Iterable[str]) -> str:
    """One regex alternation for many words, sharing prefixes so matching stays linear in the text."""
    root: dict[str, Any] = {}
    for word in words:
        node = root
        for ch in word:
            node = node.setdefault(ch, {})
        node[""] = {}

    def build(node: dict[str, Any]) -> str:
        alts = [re.escape(ch) + build(child) for ch, child in sorted(node.items()) if ch]
        if not alts:
            return ""
        body = alts[0] if len(alts) == 1 else "(?:" + "|".join(alts) + ")"
        return f"(?:{body})?" if "" in node else body

    return build(root)


# A reference starts after a non-path character, or right after "./", "../" or a lone leading "/".
_BEFORE = r"(?:(?<![\w.\-/~@+])|(?<=\./)|(?<=(?<![\w.\-/~@+])/))"
_AFTER = r"(?![\w\-/~@+]|\.[\w])"
_TRIE_LIMIT = 300  # longer names go in a plain alternation (keeps the pattern's nesting shallow)


def excluded_pattern(names: Iterable[str]) -> re.Pattern[str] | None:
    """Case-insensitive matcher for every spelling of every excluded name, or None when there are none."""
    words = {v for name in names for v in _variants(name)}
    if not words:
        return None
    short = [w for w in words if len(w) <= _TRIE_LIMIT]
    long = sorted((w for w in words if len(w) > _TRIE_LIMIT), key=len, reverse=True)
    alts = ([_trie(short)] if short else []) + [re.escape(w) for w in long]
    return re.compile(_BEFORE + "(?:" + "|".join(alts) + ")" + _AFTER, re.IGNORECASE)


def _excluded_matcher(root: Path, commit: str, policy: Policy) -> re.Pattern[str] | None:
    k = (str(root), commit, policy)
    with _excluded_lock:
        if k in _excluded_cache:
            return _excluded_cache[k]
    pattern = excluded_pattern(excluded_paths(root, commit, policy))
    with _excluded_lock:
        if len(_excluded_cache) > 64:
            _excluded_cache.clear()
        _excluded_cache[k] = pattern
    return pattern


def scrub(text: str, excluded: Iterable[str] | re.Pattern[str] | None) -> str:
    """Replace every reference to an excluded file: any letter case, Unicode normalization form,
    ``./``, ``/`` or ``../`` prefix, spaces or ``%20``. Matching runs on NFC text, so a reference
    in either form is found; text without a match is returned unchanged."""
    pattern = excluded if isinstance(excluded, re.Pattern) or excluded is None else excluded_pattern(excluded)
    if pattern is None:
        return text
    out = pattern.sub(EXCLUDED_PATH, text)
    if unicodedata.is_normalized("NFC", out):
        return out
    nfc = unicodedata.normalize("NFC", out)
    again = pattern.sub(EXCLUDED_PATH, nfc)
    return again if again != nfc else out


def seal(settings: Settings, stage: str, sections: Iterable[Section]) -> Envelope:
    """Filter, serialize and redact one prompt under the current authorization."""
    from .approval import recheck

    if stage not in STAGES:
        raise OutboundError(f"unknown prompt stage {stage!r}")
    if settings.privacy is None:
        raise OutboundError("prompts require an approved privacy policy")
    auth = recheck(settings)
    policy = outbound_policy(settings)
    parts: list[str] = []
    origins: list[str] = []
    carried: dict[str, list[str]] = {}
    for section in sections:
        if not isinstance(section, Section) or section.origin not in ORIGINS:
            raise OutboundError("every prompt section needs a known origin")
        denied = [p for p in section.paths if policy.secret(p)]
        if denied:
            raise OutboundError(f"a {section.origin} section names a path the privacy policy keeps out of prompts")
        parts.append(section.text)
        origins.append(section.origin)
        carried.setdefault(section.origin, []).extend(section.paths)
    prompt = scrub(redact("".join(parts)), _excluded_matcher(auth.root, auth.source_commit, policy))
    manifest = tuple((o, tuple(dict.fromkeys(carried[o]))) for o in dict.fromkeys(origins))
    return Envelope(stage, prompt, tuple(dict.fromkeys(origins)), auth.identity, _MINT, manifest)


def size(sections: Iterable[Section]) -> int:
    """Serialized length before redaction (for estimates; nothing is sent)."""
    return sum(len(s.text) for s in sections)


# --------------------------------------------------------------------------- gateway

_released: contextvars.ContextVar[tuple[int, Envelope] | None] = contextvars.ContextVar(
    "repowiki_released_envelope", default=None
)


def dispatch(runner: Any, envelope: Envelope, settings: Settings) -> str:
    """Send one sealed envelope to one approved runner. Every attempt (retries too) comes through here."""
    from .approval import recheck

    if not isinstance(envelope, Envelope):
        raise OutboundError("model calls accept only sealed prompt envelopes")
    auth = recheck(settings, runner)
    if auth.identity != envelope.identity:
        raise OutboundError("prompt envelope was sealed under a different authorization")
    if auth.operation == "update":
        from .safety import check_base, wiki_lock

        destination = getattr(settings, "_update_destination", None)
        if destination is None:
            raise OutboundError("update destination was not prepared")
        wiki, expected_base = destination
        with wiki_lock(wiki):
            check_base(wiki, expected_base)
    token = _released.set((id(runner), envelope))
    try:
        return runner.run(envelope)
    finally:
        _released.reset(token)


def accept(runner: Any, envelope: Any) -> str:
    """For a runner's ``run``: the prompt of an envelope ``dispatch`` released to this runner."""
    if not isinstance(envelope, Envelope):
        raise OutboundError("model backends refuse unsealed prompt strings; use the outbound gateway")
    held = _released.get()
    if held is None or held[0] != id(runner) or held[1] is not envelope:
        raise OutboundError("prompt envelope was not released to this backend by the outbound gateway")
    return envelope.prompt


def released(runner: Any, envelope: Any) -> bool:
    """True when `envelope` is the one ``dispatch`` is currently sending to `runner`."""
    held = _released.get()
    return isinstance(envelope, Envelope) and held is not None and held[0] == id(runner) and held[1] is envelope


__all__ = [
    "EXCLUDED_PATH", "Envelope", "ORIGINS", "OutboundError", "STAGES", "Section", "accept", "dispatch",
    "excluded_paths", "excluded_pattern", "outbound_policy", "scrub", "seal", "size", "template",
]  # fmt: skip
