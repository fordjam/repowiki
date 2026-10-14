"""What a run sends, to which provider, and roughly how much: `--preview` and the pre-run estimate.

A stage is *ready* when its prompt can be built now: the plan call, and each
page's draft once the outline is known. A ready stage is shown exactly: the
sealed envelope's UTF-8 byte count, its sha256 and the origins it carries,
built by the same code a run uses. A stage is *deferred* when its prompt
depends on a reply not received yet (the pages before the outline exists, the
final pass and the repair, which contain the draft); its reason is shown and
its size is only estimated. Tokens are always estimates (characters / 4), labelled
apart from the exact UTF-8 byte counts; a deferred stage's size is a guess with no
upper bound of its own, and only the run cap (``budget_tokens``, checked before every
call) bounds it. No price is ever estimated: provider prices are not known here.

The preview digest covers every ready stage. A run given that digest
(``--expect-preview``) prepares the same stages first and refuses to send
anything if one differs, so an unchanged run sends exactly what was previewed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

TYPICAL_DRAFT_CHARS = 8_000


def approx_tokens(chars: int) -> int:
    """A rough prompt-token count (about four characters per token for English and code)."""
    return (chars + 3) // 4


@dataclass
class Payload:
    """One piece of repository content inside a prompt."""

    path: str
    size: int  # bytes sent
    what: str  # e.g. "lines 1-40 of 40"


@dataclass
class PlannedCall:
    step: str  # plan, draft, final pass, repair
    provider: str
    page: str | None
    payloads: list[Payload]
    prompt_chars: int
    only_if_needed: bool = False
    from_checkpoint: bool = False
    note: str = ""
    prompt_bytes: int | None = None  # exact UTF-8 bytes of a ready stage's sealed envelope
    digest: str | None = None  # sha256 of those bytes
    origins: tuple[tuple[str, tuple[str, ...]], ...] = ()  # the envelope's origin manifest
    deferred: str = ""  # why this stage's prompt cannot be known yet
    reused: str | None = None  # sha256 of the checkpoint a reused stage returns instead of calling
    counted: bool = True  # False for a placeholder row (a deferred group of calls of unknown number)

    @property
    def ready(self) -> bool:
        return self.digest is not None


def _origins(manifest: tuple[tuple[str, tuple[str, ...]], ...]) -> list[str]:
    """The complete origin manifest: one line per origin with every repository path it carried."""
    return [f"{origin}: {', '.join(paths)}" if paths else origin for origin, paths in manifest]


@dataclass
class Estimate:
    calls: list[PlannedCall] = field(default_factory=list)
    outline_known: bool = True

    def stages(self) -> dict[tuple[str, str | None], str]:
        """(step, page) -> "send:<envelope sha256>" or "reuse:<checkpoint sha256>" for every ready stage."""
        out = {}
        for c in self.calls:
            if c.from_checkpoint and c.reused:
                out[(c.step, c.page)] = "reuse:" + c.reused
            elif c.ready and not c.from_checkpoint:
                out[(c.step, c.page)] = "send:" + str(c.digest)
        return out

    def digest(self, identity: str, commit: str) -> str:
        """One digest for the approval identity, source commit, every ready stage's envelope and the exact
        bytes of every checkpoint a stage would reuse instead of calling."""
        rows = [[c.step, c.page, c.provider, c.digest, c.from_checkpoint, c.reused] for c in self.calls if c.ready]
        blob = json.dumps({"identity": identity, "commit": commit, "outline": self.outline_known, "stages": rows})
        return hashlib.sha256(blob.encode()).hexdigest()

    def _live(self) -> list[PlannedCall]:
        return [c for c in self.calls if not c.from_checkpoint and c.counted]

    @property
    def expected_calls(self) -> int:
        return sum(1 for c in self._live() if not c.only_if_needed)

    @property
    def expected_tokens(self) -> int:
        return approx_tokens(sum(c.prompt_chars for c in self._live() if not c.only_if_needed))

    @property
    def worst_tokens(self) -> int:
        return approx_tokens(sum(c.prompt_chars for c in self._live()))

    def _ready(self) -> list[PlannedCall]:
        return [c for c in self._live() if c.ready]

    def _deferred(self) -> list[PlannedCall]:
        return [c for c in self._live() if not c.ready]

    def totals(self, budget_tokens: int | None = None) -> list[str]:
        """Labelled totals: ready stages exactly (bytes) and estimated (tokens), deferred stages as
        estimates that only the run cap bounds. Never a price."""
        ready = self._ready()
        ready_bytes = sum(c.prompt_bytes or 0 for c in ready)
        ready_tokens = approx_tokens(sum(c.prompt_chars for c in ready))
        out = [f"  ready stages: {len(ready)} call(s), {ready_bytes:,} UTF-8 bytes (exact), "
               f"~{ready_tokens:,} estimated input tokens"]  # fmt: skip
        later = self._deferred()
        unknown = any(not c.counted and not c.from_checkpoint for c in self.calls)
        if later or unknown:
            needed = [c for c in later if not c.only_if_needed]
            repairs = len(later) - len(needed)
            guess = approx_tokens(sum(c.prompt_chars for c in needed))
            text = f"  deferred stages: {len(needed)} call(s), ~{guess:,} estimated input tokens"
            if repairs:
                text += f", plus up to {repairs} repair call(s) only if needed"
            if unknown:
                text += "; the page calls' number and size are unknown until the outline exists"
            out.append(text + f" (a guess assuming a typical {TYPICAL_DRAFT_CHARS:,}-character draft, not a bound)")
            if budget_tokens is not None:
                out.append(f"  bounded by the run cap: {budget_tokens:,} estimated input tokens for the whole run; "
                           "the run stops before a call would exceed it")  # fmt: skip
            else:
                out.append("  no run cap is set, so the deferred stages are not bounded")
        out.append("  bytes are exact; tokens are estimates (about 4 characters per token), not a provider's "
                   "count; no price is estimated")  # fmt: skip
        return out

    def summary(self) -> str:
        repairs = sum(1 for c in self._live() if c.only_if_needed)
        text = f"{self.expected_calls} model call(s), ~{self.expected_tokens:,} estimated prompt tokens"
        if repairs:
            text += f" (estimated ~{self.worst_tokens:,} if all {repairs} page(s) need a repair call)"
        return text + "; retries not included"

    def render(
        self, withheld: list[str], digest: str | None = None, *, settings: list[str] | None = None,
        budget_tokens: int | None = None,
    ) -> str:
        out = ["Nothing has been sent. This run would send the following:", ""]
        if settings:
            out += ["Effective settings:"] + [f"  {line}" for line in settings] + [""]
        for c in self.calls:
            target = f" {c.page}" if c.page else ""
            if c.from_checkpoint:
                saved = f" (checkpoint sha256 {c.reused})" if c.reused else ""
                out.append(f"  {c.step}{target}: reused from a checkpoint, nothing sent{saved}")
                continue
            cond = " (only if the page fails validation)" if c.only_if_needed else ""
            if c.ready:
                out.append(
                    f"  {c.step}{target} -> {c.provider}: {c.prompt_bytes:,} bytes, sha256 {c.digest}, "
                    f"~{approx_tokens(c.prompt_chars):,} estimated tokens{cond}"
                )
                out.append("      origins:")
                out += [f"        {line}" for line in _origins(c.origins)]
            else:
                size = "size unknown" if not c.counted else f"~{approx_tokens(c.prompt_chars):,} tokens estimated"
                out.append(f"  {c.step}{target} -> {c.provider}: deferred, {size}{cond}")
                if c.deferred:
                    out.append(f"      deferred: {c.deferred}")
            if c.note:
                out.append(f"      {c.note}")
            width = max((len(p.path) for p in c.payloads), default=0)
            for p in c.payloads:
                out.append(f"      {p.path:<{width}}  {p.size:>9,} bytes  {p.what}")
        if not self.outline_known:
            out += [
                "",
                "  pages: the outline is not known yet. `repowiki generate --plan-only` makes only the plan",
                "  call above and saves the outline; run --preview again to see every page's files.",
            ]
        out += ["", f"Withheld by the secret policy (never sent): {len(withheld)} file(s)"]
        out += [f"  {w}" for w in withheld[:50]] + (["  ..."] if len(withheld) > 50 else [])
        out += [
            "",
            f"Total: {self.summary()} (for the stages listed above; deferred stages are estimates or unknown).",
            *self.totals(budget_tokens),
            "Every CLI backend runs with its tools disabled, in an empty temporary directory,",
            "with a minimal environment; the prompt is the only repository content it receives.",
        ]
        if digest:
            out += [
                "",
                f"Preview digest: {digest}",
                "Pass it as --expect-preview to send exactly the ready stages above; a run whose prepared",
                "prompts differ (source, configuration, previous pages or checkpoints changed) sends nothing.",
            ]
        return "\n".join(out)
