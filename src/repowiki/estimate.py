"""What a run sends, to which provider, and roughly how much: `--preview` and the pre-run estimate.

Prompt sizes are exact (they are built from the same code a real run uses);
tokens are approximated as characters / 4. Revise and repair prompts contain
the model's own draft, which is unknown in advance, so those are estimated
with a typical draft size.
"""

from __future__ import annotations

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


@dataclass
class Estimate:
    calls: list[PlannedCall] = field(default_factory=list)
    outline_known: bool = True

    def _live(self) -> list[PlannedCall]:
        return [c for c in self.calls if not c.from_checkpoint]

    @property
    def expected_calls(self) -> int:
        return sum(1 for c in self._live() if not c.only_if_needed)

    @property
    def expected_tokens(self) -> int:
        return approx_tokens(sum(c.prompt_chars for c in self._live() if not c.only_if_needed))

    @property
    def worst_tokens(self) -> int:
        return approx_tokens(sum(c.prompt_chars for c in self._live()))

    def summary(self) -> str:
        repairs = sum(1 for c in self._live() if c.only_if_needed)
        text = f"{self.expected_calls} model call(s), ~{self.expected_tokens:,} prompt tokens"
        if repairs:
            text += f" (estimated ~{self.worst_tokens:,} if all {repairs} page(s) need a repair call)"
        return text + "; retries not included"

    def render(self, withheld: list[str]) -> str:
        out = ["Nothing has been sent. This run would send the following:", ""]
        for c in self.calls:
            target = f" {c.page}" if c.page else ""
            if c.from_checkpoint:
                out.append(f"  {c.step}{target}: reused from a checkpoint, nothing sent")
                continue
            cond = " (only if the page fails validation)" if c.only_if_needed else ""
            out.append(f"  {c.step}{target} -> {c.provider}: ~{approx_tokens(c.prompt_chars):,} tokens{cond}")
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
            f"Total: {self.summary()}.",
            "Every CLI backend runs with its tools disabled, in an empty temporary directory,",
            "with a minimal environment; the prompt is the only repository content it receives.",
        ]
        return "\n".join(out)
