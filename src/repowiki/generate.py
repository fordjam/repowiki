"""Plan an outline, write pages, validate them and write docs/wiki/index.json."""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from .approval import Authorization, authorize, recheck
from .checkpoint import Checkpoints, key
from .config import Settings
from .estimate import TYPICAL_DRAFT_CHARS, Estimate, Payload, PlannedCall, approx_tokens
from .git import Snapshot, repo_name
from .links import (
    RepoIdentity,
    SourceTarget,
    cited_files,
    find_links,
    outside_code,
    relative_from_page,
    resolve_href,
    unlink,
)
from .outbound import Envelope, OutboundError, Section, dispatch, outbound_policy, seal, size
from .ownership import is_owned
from .policy import DEFAULT_POLICY, Policy, is_secret, redact
from .prompts import PageOrigin, PlannedPage, page_prompt, plan_prompt, repair_prompt, revise_prompt
from .runners import Runner, RunnerError
from .safety import (
    GENERATED_MARK,
    INDEX,
    base_digest,
    check_base,
    read_wiki_file,
    recover,
    transact,
    wiki_file,
    wiki_lock,
    wiki_path,
)
from .survey import Excerpts, Survey, citable, excerpts, survey, tree_entries
from .validate import (
    Fragments,
    Problem,
    check_index,
    check_page,
    check_wiki,
    fragment_problem,
    link_problem,
    repo_identities,
)

Log = Callable[[str], None]


class GenerateError(RuntimeError):
    """Generation could not produce a valid wiki."""


class StaleEnvelopeError(GenerateError):
    """A prepared prompt differs from the one the operator previewed; nothing was sent."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def check_expected(
    expected: dict[tuple[str, str | None], str] | None, step: str, page: str | None, envelope: Envelope
) -> None:
    """Refuse to send a previewed stage whose sealed bytes changed, or one the preview showed as reused."""
    want = None if expected is None else expected.get((step, page))
    if want is not None and want != "send:" + envelope.digest:
        where = f" for {page}" if page else ""
        raise StaleEnvelopeError(f"the {step} prompt{where} differs from the preview; nothing was sent. Preview again.")


def check_reused(expected: dict[tuple[str, str | None], str] | None, step: str, page: str | None, text: str) -> None:
    """Refuse to reuse a checkpoint whose bytes differ from the one the preview showed."""
    want = None if expected is None else expected.get((step, page))
    if want is not None and want != "reuse:" + _sha(text):
        where = f" for {page}" if page else ""
        raise StaleEnvelopeError(f"the saved {step}{where} differs from the preview; nothing was sent. Preview again.")


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48].strip("-") or "page"


# --------------------------------------------------------------------------- plan


def extract_json(text: str) -> Any:
    """First JSON object in a model reply (tolerates code fences and chatter)."""
    fenced = re.search(r"```(?:json)?\s*\n(.*?)```", text, re.S)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ValueError("no JSON object found in the reply")


def outline_from_json(
    data: Any, files: set[str], min_pages: int, max_pages: int, fallback_sources: list[str] | None = None
) -> list[PlannedPage]:
    """Turn a planner reply into numbered pages; raises ValueError if unusable.

    A page the planner gave no usable sources gets `fallback_sources`, so every
    page has code to cite.
    """
    raw = data.get("pages") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ValueError('expected {"pages": [...]}')
    pages: list[PlannedPage] = []
    by_title: dict[str, str] = {}
    for item in raw[:max_pages]:
        if not isinstance(item, dict) or not str(item.get("title") or "").strip():
            continue
        title = " ".join(str(item["title"]).split())[:80]
        if title.lower() in by_title:
            continue
        n = len(pages) + 1
        file = f"{n}-{slugify(title)}.md"
        if is_secret(file):  # e.g. a page titled "Agent transcript": never give it a secret-pattern name
            file = f"{n}-page.md"
        parent_ref = item.get("parent")
        parent = None
        if isinstance(parent_ref, str) and parent_ref.strip().lower() in by_title:
            parent = by_title[parent_ref.strip().lower()]
        elif isinstance(parent_ref, int) and 1 <= parent_ref < n:
            parent = pages[parent_ref - 1].file
        srcs = item.get("sources")
        srcs = srcs if isinstance(srcs, list) else []
        sources = [_strip_dot(s.strip()) for s in srcs if isinstance(s, str)]
        sources = list(dict.fromkeys(s for s in sources if s in files))[:8] or list(fallback_sources or [])
        pages.append(PlannedPage(file, title, parent, str(item.get("summary") or "").strip(), sources))
        by_title[title.lower()] = file
    if len(pages) < min_pages:
        raise ValueError(f"plan has {len(pages)} usable pages, need at least {min_pages}")
    if pages[0].parent is not None:
        pages[0].parent = None
    return pages


def _strip_dot(path: str) -> str:
    while path.startswith("./"):
        path = path[2:]
    return path


def heuristic_outline(
    files: list[str], readme: str | None, entry_points: list[str], tests: list[str], max_pages: int
) -> list[dict[str, Any]]:
    """A structure-only outline: overview, one page per top-level source area, testing."""
    test_set = set(tests)
    overview = [readme] if readme else []
    outline: list[dict[str, Any]] = [
        {"title": "Overview", "parent": None, "summary": "What the project is and how it is organised.",
         "sources": (overview + entry_points)[:6] or files[:4]}
    ]  # fmt: skip
    groups: dict[str, list[str]] = {}
    for f in files:
        if f in test_set or f == readme:
            continue
        parts = f.split("/")
        key = parts[0] if len(parts) > 1 else "(root)"
        if key in ("src", "lib", "pkg", "packages", "apps") and len(parts) > 2:
            key = "/".join(parts[:2])
        groups.setdefault(key, []).append(f)
    room = max_pages - 1 - (1 if tests else 0)
    for key, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))[: max(room, 0)]:
        title = "Project files" if key == "(root)" else posixpath.basename(key).replace("_", " ").title()
        outline.append({"title": title, "parent": None, "summary": f"The code under {key}.", "sources": members[:8]})
    if tests:
        outline.append({"title": "Testing", "parent": None, "summary": "How the project is tested.",
                        "sources": tests[:8]})  # fmt: skip
    return outline


def plan_sections(snap: Snapshot, s: Survey, settings: Settings, name: str) -> list[Section]:
    return plan_prompt(name, s, tree_entries(snap, s.files), settings.min_pages, settings.max_pages)


def plan_request(snap: Snapshot, s: Survey, settings: Settings, name: str) -> Envelope:
    return seal(settings, "plan", plan_sections(snap, s, settings, name))


def _fallback_sources(s: Survey) -> list[str]:
    return (s.entry_points + ([s.readme] if s.readme else []))[:3] or s.files[:3]


def saved_plan_entry(
    snap: Snapshot, s: Survey, settings: Settings, runner: Runner, name: str, checkpoints: Checkpoints | None
) -> tuple[list[PlannedPage], str] | None:
    """The usable outline checkpointed by an earlier run with the same inputs, and its exact bytes."""
    if checkpoints is None:
        return None
    prompt = plan_request(snap, s, settings, name).prompt
    saved = checkpoints.get("plan", checkpoint_key("plan", runner, recheck(settings).identity, prompt))
    if saved is None:
        return None
    try:
        data = json.loads(saved)
        pages = outline_from_json(data, set(s.files), settings.min_pages, settings.max_pages, _fallback_sources(s))
    except ValueError:
        return None
    return pages, saved


def saved_plan(
    snap: Snapshot, s: Survey, settings: Settings, runner: Runner, name: str, checkpoints: Checkpoints | None
) -> list[PlannedPage] | None:
    """The outline checkpointed by an earlier run with the same inputs, if any (no model call)."""
    entry = saved_plan_entry(snap, s, settings, runner, name, checkpoints)
    return entry[0] if entry else None


def plan(
    snap: Snapshot,
    s: Survey,
    settings: Settings,
    runner: Runner,
    name: str,
    log: Log,
    *,
    checkpoints: Checkpoints | None = None,
    meter: Meter | None = None,
    expected: dict[tuple[str, str | None], str] | None = None,
) -> list[PlannedPage]:
    if getattr(settings, "_authorization", None) is None:
        authorize(snap.root, settings, snap.commit, "plan")
    auth = recheck(settings, runner)
    if auth.operation not in ("generate", "plan"):
        raise ValueError("planning operation is not approved")
    if auth.root != snap.root.resolve() or auth.source_commit != snap.commit:
        raise ValueError("plan snapshot differs from authorized source")
    s = survey(snap, settings.wiki_dir, policy=outbound_policy(settings))
    sections = plan_sections(snap, s, settings, name)
    envelope = seal(settings, "plan", sections)
    fileset = set(s.files)
    fallback = _fallback_sources(s)
    k = checkpoint_key("plan", runner, auth.identity, envelope.prompt)
    entry = saved_plan_entry(snap, s, settings, runner, name, checkpoints)
    if entry is not None:
        check_reused(expected, "plan", None, entry[1])
        log("  reusing the saved outline for this commit")
        return entry[0]
    reply = ""
    check_expected(expected, "plan", None, envelope)
    for attempt in (1, 2):
        try:
            reply = call(runner, envelope, settings, log, meter)
            data = extract_json(reply)
            pages = outline_from_json(data, fileset, settings.min_pages, settings.max_pages, fallback)
            if checkpoints:
                checkpoints.put("plan", k, json.dumps(data))
            return pages
        except (RunnerError, ValueError) as exc:
            log(f"  plan attempt {attempt} with {runner.label} failed: {exc}")
            if isinstance(exc, RunnerError):
                raise GenerateError(str(exc)) from exc
            note = f"\n\nYour previous reply could not be used ({exc}). Reply with the JSON object only.\n"
            sections = [*sections, Section("retry", note)]
            envelope = seal(settings, "plan", sections)
    log("  falling back to a structure-only outline")
    outline = heuristic_outline(s.files, s.readme, s.entry_points, s.tests, settings.max_pages)
    return outline_from_json({"pages": outline}, fileset, 1, settings.max_pages, fallback)


# --------------------------------------------------------------------------- calls


def checkpoint_key(kind: str, runner: Runner, *parts: str) -> str:
    return key(kind, getattr(runner, "cache_id", runner.label), *parts)


@dataclass
class Meter:
    """Counts model calls and prompt size; optionally stops a run that would exceed a token budget."""

    calls: int = 0
    prompt_chars: int = 0
    budget_tokens: int | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def tokens(self) -> int:
        return approx_tokens(self.prompt_chars)

    def charge(self, prompt: str) -> None:
        with self.lock:
            if self.budget_tokens is not None and approx_tokens(self.prompt_chars + len(prompt)) > self.budget_tokens:
                raise BudgetError(
                    f"token budget reached (~{self.tokens:,} of {self.budget_tokens:,} prompt tokens used); "
                    "finished pages are checkpointed, rerun with a larger --budget to continue"
                )
            self.calls += 1
            self.prompt_chars += len(prompt)


class BudgetError(RunnerError):
    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False)


_sleep = time.sleep  # replaced in tests


def call(runner: Runner, envelope: Envelope, settings: Settings, log: Log, meter: Meter | None = None) -> str:
    """One model call with bounded retries and exponential backoff for transient failures.

    Every attempt, retries included, goes through the outbound gateway with the sealed envelope.
    """
    if not isinstance(envelope, Envelope):
        raise OutboundError("model calls accept only sealed prompt envelopes")
    if recheck(settings, runner).operation not in ("generate", "plan", "update"):
        raise ValueError("model execution operation is not approved")
    if meter is None:
        meter = getattr(settings, "_call_meter", None) or Meter(budget_tokens=settings.budget_tokens)
        settings._call_meter = meter
    meter.budget_tokens = min(meter.budget_tokens or settings.budget_tokens, settings.budget_tokens)
    for attempt in range(settings.retries + 1):
        recheck(settings, runner)
        if meter is not None:
            meter.charge(envelope.prompt)
        try:
            return dispatch(runner, envelope, settings)
        except RunnerError as exc:
            if not exc.transient or attempt == settings.retries:
                raise
            delay = min(settings.backoff * (2**attempt), 120.0)
            log(f"  {runner.label}: {exc}; retrying in {delay:.0f}s ({attempt + 1}/{settings.retries})")
            _sleep(delay)
    raise AssertionError("unreachable")


# --------------------------------------------------------------------------- page text

LINK_TARGET_RE = re.compile(r"(\]\()(\s*<?)([^)\s>]+)(>?(?:\s+\"[^\"]*\")?\s*\))")
DETAILS_RE = re.compile(r"<details>\s*<summary>\s*Relevant source files.*?</details>\s*", re.S | re.I)


def clean_reply(reply: str, title: str) -> str:
    """Strip a wrapping code fence and any chatter before the first heading."""
    text = reply.strip()
    m = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", text, re.S)
    if m:
        text = m.group(1).strip()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            text = "\n".join([f"# {title}", *lines[i + 1 :]])
            break
    else:
        text = f"# {title}\n\n{text}"
    return text.strip() + "\n"


def relink(
    text: str,
    page_file: str,
    wiki_dir: str,
    snap: Snapshot,
    page_files: set[str],
    policy: Policy = DEFAULT_POLICY,
    repo_identities: tuple[RepoIdentity, ...] = (),
) -> str:
    """Rewrite repo-root citation targets into links relative to the page (never to secret files).

    A web URL into a registered identity at the indexed commit becomes its canonical relative link."""

    def fix(href: str) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*:|^//", href, re.I):
            res = resolve_href(href, page_file, wiki_dir, repo_identities=repo_identities, commit=snap.commit)
            if res.kind == "source" and res.target and not policy.secret(res.target.path):
                return res.target.href(page_file, wiki_dir)
            return href
        if href.startswith("#"):
            return href
        path, _, frag = href.partition("#")
        clean = path
        while clean.startswith("./"):
            clean = clean[2:]
        if clean in page_files or clean.startswith("../"):
            return href
        if clean.startswith("/"):
            clean = clean.lstrip("/")
        if (snap.has_file(clean) or snap.has_dir(clean)) and not policy.secret(clean):
            rel = relative_from_page(page_file, wiki_dir, clean)
            return rel + (f"#{frag}" if frag else "")
        return href

    def sub(chunk: str) -> str:
        return LINK_TARGET_RE.sub(lambda m: m.group(1) + m.group(2) + fix(m.group(3)) + m.group(4), chunk)

    return outside_code(text, sub)


def with_sources_block(
    text: str,
    page_file: str,
    wiki_dir: str,
    extra: list[str],
    *,
    repo_identities: tuple[RepoIdentity, ...] = (),
    commit: str | None = None,
) -> str:
    """Insert a fresh 'Relevant source files' block after the title and the generated-page marker at the end."""
    body = DETAILS_RE.sub("", text).replace(GENERATED_MARK, "")
    cited = cited_files(body, page_file, wiki_dir, repo_identities=repo_identities, commit=commit)
    sources = list(dict.fromkeys(cited + extra))
    items = "\n".join(f"- [{s}]({SourceTarget(s).href(page_file, wiki_dir)})" for s in sources)
    block = (
        "<details>\n<summary>Relevant source files</summary>\n\n"
        "The following files were used as context for this page:\n\n"
        f"{items or '- (none)'}\n\n</details>\n"
    )
    title, _, rest = body.partition("\n")
    return f"{title}\n\n{block}\n{rest.strip()}\n\n{GENERATED_MARK}\n"


# --------------------------------------------------------------------------- writing


@dataclass
class Context:
    root: Path
    snap: Snapshot
    settings: Settings
    name: str
    cheap: Runner
    strong: Runner
    pages: list[PlannedPage]
    log: Log
    warnings: list[str] = field(default_factory=list)
    checkpoints: Checkpoints | None = None
    meter: Meter = field(default_factory=Meter)
    pages_from_wiki: bool = False  # update: titles come from the previous wiki's index
    candidate_base: str | None = None
    expected: dict[tuple[str, str | None], str] | None = None  # previewed stage digests a run must match

    @property
    def wiki_dir(self) -> str:
        return self.settings.wiki_dir

    @cached_property
    def identities(self) -> tuple[RepoIdentity, ...]:
        """Registered web identities of the repository (its origin remote): web URLs into them are source links."""
        return repo_identities(self.root)

    @property
    def wiki_path(self) -> Path:
        return wiki_path(self.root, self.settings.wiki_dir)

    @property
    def policy(self) -> Policy:
        return self.settings.policy

    @property
    def page_files(self) -> set[str]:
        return {p.file for p in self.pages}

    @property
    def origin(self) -> PageOrigin:
        """Where page titles, file names and summaries come from: the planner, or the previous wiki."""
        return PageOrigin("previous_wiki", self.wiki_dir) if self.pages_from_wiki else PageOrigin()

    def call(self, runner: Runner, envelope: Envelope) -> str:
        return call(runner, envelope, self.settings, self.log, self.meter)


def _problems(ctx: Context, page: PlannedPage, text: str) -> list[Problem]:
    return check_page(
        page.file,
        text,
        wiki_dir=ctx.wiki_dir,
        wiki_path=ctx.wiki_path,
        snap=ctx.snap,
        extra_pages=ctx.page_files,
        policy=ctx.policy,
        min_citations=ctx.settings.min_citations,
        repo_identities=ctx.identities,
    )


def page_sources(ctx: Context, page: PlannedPage) -> list[str]:
    policy = outbound_policy(ctx.settings)
    return [s for s in page.sources if ctx.snap.has_file(s) and not policy.secret(s)]


def page_excerpts(ctx: Context, page: PlannedPage) -> Excerpts:
    """The page's source excerpts within ``context_chars`` and, when set, ``context_bytes`` (UTF-8)."""
    sources, policy = page_sources(ctx, page), outbound_policy(ctx.settings)
    budget, limit = ctx.settings.context_chars, ctx.settings.context_bytes
    ex = excerpts(ctx.snap, sources, budget, policy)
    while limit is not None and ex.size > limit:
        if budget == 0:
            raise GenerateError(
                f"{page.file}: the source excerpt headers alone are {ex.size:,} UTF-8 bytes, over the "
                f"context_bytes ceiling of {limit:,}"
            )
        # A character is at least one byte: dropping the excess in characters always shrinks the bytes.
        budget = max(0, min(budget * limit // ex.size, budget - (ex.size - limit)))
        ex = excerpts(ctx.snap, sources, budget, policy)
    return ex


def authorized_context(ctx: Context) -> Authorization:
    """The current authorization, refusing a context whose clone or snapshot it does not cover."""
    auth = recheck(ctx.settings)
    if auth.root != ctx.root.resolve() or auth.root != ctx.snap.root.resolve() or auth.source_commit != ctx.snap.commit:
        raise ValueError("page snapshot differs from authorized source")
    return auth


def wiki_context_denied(ctx: Context, page: PlannedPage) -> bool:
    """Whether a previous wiki page (its text, title or file name) may not reach a prompt."""
    return "previous_wiki" not in ctx.settings.privacy["context_classes"] or outbound_policy(ctx.settings).secret(
        f"{ctx.wiki_dir}/{page.file}"
    )


def page_request(ctx: Context, page: PlannedPage, previous: str | None = None) -> tuple[Envelope, Excerpts]:
    authorized_context(ctx)
    if (previous or ctx.pages_from_wiki) and wiki_context_denied(ctx, page):
        raise ValueError("previous wiki context is not approved")
    others = ctx.pages
    if ctx.pages_from_wiki:
        others = [p for p in ctx.pages if not wiki_context_denied(ctx, p)]
        # A parent reference names another wiki page: keep it only when it is an allowed page.
        allowed = {p.file for p in others} | {p.title for p in others}
        others = [p if p.parent is None or p.parent in allowed else replace(p, parent=None) for p in others]
    ex = page_excerpts(ctx, page)
    if previous and len(previous) > MAX_PREVIOUS_CHARS:
        previous = previous[:MAX_PREVIOUS_CHARS] + "\n... (previous version truncated)"
    sections = page_prompt(ctx.name, page, others, ex.text, previous, sources=tuple(ex.shown), where=ctx.origin)
    return seal(ctx.settings, "draft", sections), ex


MAX_PREVIOUS_CHARS = 30_000


def reusable_page(ctx: Context, page: PlannedPage, envelope: Envelope) -> str | None:
    """A checkpointed page for exactly this prompt that still validates; the run and the preview both
    use this one test, so the preview shows a page as reused exactly when the run reuses it."""
    saved = ctx.checkpoints.get("page", page_key(ctx, envelope)) if ctx.checkpoints else None
    if saved is None or [p for p in _problems(ctx, page, saved) if p.level == "error"]:
        return None
    return saved


def page_key(ctx: Context, envelope: Envelope) -> str:
    final = ctx.strong.label
    if ctx.strong is not ctx.cheap and ctx.strong.label == ctx.cheap.label:
        final += " (final pass on the drafting model)"  # never reuse a one-pass page for a two-pass run
    return checkpoint_key(
        "page",
        ctx.cheap,
        recheck(ctx.settings).identity,
        final,
        str(ctx.settings.min_citations),
        envelope.prompt,
    )


def _finish(ctx: Context, page: PlannedPage, reply: str, shown: list[str]) -> str:
    text = clean_reply(redact(reply), page.title)
    text = relink(text, page.file, ctx.wiki_dir, ctx.snap, ctx.page_files, ctx.policy, ctx.identities)
    return with_sources_block(
        text, page.file, ctx.wiki_dir, shown, repo_identities=ctx.identities, commit=ctx.snap.commit
    )


def write_page(ctx: Context, page: PlannedPage, previous: str | None = None) -> str:
    """Draft, revise, repair and verify one page. Raises PageError if it cannot be made valid."""
    if authorized_context(ctx).operation not in ("generate", "update"):
        raise ValueError("page generation operation is not approved")
    envelope, ex = page_request(ctx, page, previous)
    k = page_key(ctx, envelope)
    saved = reusable_page(ctx, page, envelope)
    if saved is not None:
        check_reused(ctx.expected, "draft", page.file, saved)
        ctx.log(f"  {page.file}: reusing the checkpoint from an earlier run")
        return saved
    check_expected(ctx.expected, "draft", page.file, envelope)
    shown = ex.included  # the sources block lists only files the model actually saw
    ctx.log(f"  writing {page.file} ({page.title}) with {ctx.cheap.label}")
    text = _finish(ctx, page, ctx.call(ctx.cheap, envelope), shown)
    if ctx.strong is not ctx.cheap:
        try:
            revise = revise_prompt(ctx.name, page, text, ex.text, sources=tuple(ex.shown), where=ctx.origin)
            text = _finish(ctx, page, ctx.call(ctx.strong, seal(ctx.settings, "revise", revise)), shown)
        except BudgetError:
            raise
        except RunnerError as exc:
            ctx.warnings.append(f"{page.file}: final pass failed, kept the draft ({exc})")
    probs = [p for p in _problems(ctx, page, text) if p.level == "error"]
    if probs:
        ctx.log(f"  {page.file}: {len(probs)} problem(s), asking {ctx.cheap.label} to repair")
        try:
            repair = repair_prompt(
                ctx.name, page, text, [p.message for p in probs], ex.text, sources=tuple(ex.shown), where=ctx.origin
            )
            reply = ctx.call(ctx.cheap, seal(ctx.settings, "repair", repair))
            fixed = _finish(ctx, page, reply, shown)
            if len([p for p in _problems(ctx, page, fixed) if p.level == "error"]) <= len(probs):
                text = fixed
        except BudgetError:
            raise
        except RunnerError as exc:
            ctx.warnings.append(f"{page.file}: repair failed ({exc})")
    text = _drop_bad_links(ctx, page, text, shown)
    errors = [p for p in _problems(ctx, page, text) if p.level == "error"]
    if errors:
        raise PageError(page.file, "; ".join(p.message for p in errors[:5]))
    if ctx.checkpoints:
        ctx.checkpoints.put("page", k, text)
    return text


def _bad_hrefs(ctx: Context, page: PlannedPage, text: str) -> set[str]:
    own = Fragments({page.file: text}, complete=False)  # other pages' fragments: checked at publish
    return {
        link.href
        for link in find_links(text)
        if link_problem(
            link,
            page.file,
            wiki_dir=ctx.wiki_dir,
            wiki_path=ctx.wiki_path,
            snap=ctx.snap,
            extra_pages=ctx.page_files,
            policy=ctx.policy,
            repo_identities=ctx.identities,
            fragments=own,
        )
    }


def _drop_bad_links(ctx: Context, page: PlannedPage, text: str, sources: list[str]) -> str:
    """Turn links that still fail after repair into plain text (inline and reference-style)."""
    removed: set[str] = set()
    for _ in range(3):
        bad = _bad_hrefs(ctx, page, text)
        if not bad:
            break
        removed |= bad
        text = with_sources_block(
            unlink(text, bad), page.file, ctx.wiki_dir, sources, repo_identities=ctx.identities, commit=ctx.snap.commit
        )
    if removed:
        ctx.warnings.append(
            f"{page.file}: removed {len(removed)} unverifiable link(s): {', '.join(sorted(removed))[:300]}"
        )
    return text


class PageError(GenerateError):
    """One page could not be made valid."""

    def __init__(self, file: str, reason: str) -> None:
        super().__init__(f"{file}: {reason}")
        self.file = file


def write_pages(ctx: Context, todo: list[PlannedPage], previous: dict[str, str] | None = None) -> dict[str, str]:
    """Write every page in `todo`; finished pages are checkpointed as they complete.

    If any page fails, the others still finish (and are checkpointed), then this
    raises and nothing is published; rerunning resumes from the checkpoints.
    """
    previous = previous or {}
    done: dict[str, str] = {}
    failed: list[str] = []
    stop = threading.Event()

    def one(page: PlannedPage) -> None:
        if stop.is_set():
            return
        try:
            done[page.file] = write_page(ctx, page, previous.get(page.file))
        except BudgetError as exc:
            stop.set()
            failed.append(str(exc))
        except (GenerateError, RunnerError) as exc:
            ctx.log(f"  failed: {exc}")
            failed.append(f"{page.file}: {exc}" if not str(exc).startswith(page.file) else str(exc))

    jobs = max(1, ctx.settings.jobs)
    if jobs == 1:
        for page in todo:
            one(page)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            list(pool.map(one, todo))
    if failed:
        saved = f"; {len(done)} finished page(s) are checkpointed, rerun to resume" if ctx.checkpoints and done else ""
        raise GenerateError(
            f"{len(todo) - len(done)} of {len(todo)} page(s) not written; the wiki on disk was not changed{saved}:\n  "
            + "\n  ".join(failed)
        )
    return {p.file: done[p.file] for p in todo}


def index_data(name: str, commit: str, pages: list[PlannedPage]) -> dict[str, Any]:
    return {
        "repo": name,
        "indexed_commit": commit,
        "indexed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pages": [{"file": p.file, "title": p.title, "parent": p.parent} for p in pages],
    }


def save(wiki: Path, texts: dict[str, str], index: dict[str, Any], old_files: set[str] | None = None) -> list[str]:
    with wiki_lock(wiki):
        recover(wiki)
        return _save(wiki, texts, index, old_files)


def _save(wiki: Path, texts: dict[str, str], index: dict[str, Any], old_files: set[str] | None = None) -> list[str]:
    """Write pages and index.json inside `wiki` and delete pages dropped from the index, as one transaction.

    Plain file names only, no symlinks. Exact local ownership is required for replacement
    or deletion; a marker grants no authority. Returns warnings about dropped pages kept.
    """
    keep = {p["file"] for p in index["pages"]}
    warnings: list[str] = []
    doomed: list[str] = []
    for stale in sorted((old_files or set()) - keep):
        try:
            target = wiki_file(wiki, stale)
        except ValueError as exc:
            warnings.append(f"kept {stale}: {exc}")
            continue
        if target.exists() and not is_owned(wiki, stale):
            warnings.append(f"kept {stale}: not written by repowiki or manually edited (no exact local ownership)")
        elif target.exists():
            doomed.append(stale)
    writes = {**texts, INDEX: json.dumps(index, indent=2, ensure_ascii=False) + "\n"}
    transact(wiki, writes, doomed)
    return warnings


@dataclass
class Result:
    pages: list[PlannedPage]
    written: list[str]
    commit: str
    warnings: list[str]
    problems: list[Problem]


def old_page_files(wiki: Path, wiki_dir: str = "docs/wiki", policy: Policy = DEFAULT_POLICY) -> set[str]:
    """Page files named by an existing index.json, keeping only safe plain, non-secret names."""
    try:
        data = json.loads(wiki_file(wiki, INDEX).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {e.file for e in check_index(data, wiki_dir=wiki_dir, policy=policy)[1]}


# --------------------------------------------------------------------------- estimates and preview


def plan_call(snap: Snapshot, s: Survey, settings: Settings, name: str, runner: Runner) -> PlannedCall:
    tree = "\n".join(line for line, _ in tree_entries(snap, s.files))
    payloads = []
    if s.readme:
        payloads.append(
            Payload(s.readme, len(s.readme_text.encode()), f"text, first {len(s.readme_text):,} characters")
        )
    payloads.append(Payload("(file list)", len(tree.encode()), f"{len(s.files)} paths and sizes, no file contents"))
    note = "plus the names (not contents) of manifests, entry points and tests"
    envelope = plan_request(snap, s, settings, name)
    return PlannedCall(
        "plan", runner.label, None, payloads, len(envelope.prompt), note=note,
        prompt_bytes=envelope.size, digest=envelope.digest, origins=envelope.manifest,
    )  # fmt: skip


def page_calls(ctx: Context, pages: list[PlannedPage], previous: dict[str, str] | None = None) -> list[PlannedCall]:
    """The draft, final-pass and repair calls each page would make, with the exact files each carries."""
    calls: list[PlannedCall] = []
    for page in pages:
        old = (previous or {}).get(page.file)
        envelope, ex = page_request(ctx, page, old)
        saved = reusable_page(ctx, page, envelope)
        cached = saved is not None
        payloads = [
            Payload(
                path, ex.sizes.get(path, 0), f"lines 1-{n} of {total}" if n else f"omitted (over budget), {total} lines"
            )
            for path, (n, total) in ex.shown.items()
        ]
        if old:
            payloads.append(Payload(f"{ctx.wiki_dir}/{page.file}", len(old.encode()), "previous version of the page"))
        calls.append(
            PlannedCall(
                "draft", ctx.cheap.label, page.file, payloads, len(envelope.prompt), from_checkpoint=cached,
                prompt_bytes=envelope.size, digest=envelope.digest, origins=envelope.manifest,
                reused=_sha(saved) if saved is not None else None,
            )
        )  # fmt: skip
        draft = "x" * TYPICAL_DRAFT_CHARS
        if ctx.strong is not ctx.cheap:
            chars = size(revise_prompt(ctx.name, page, draft, ex.text, where=ctx.origin))
            calls.append(
                PlannedCall(
                    "final pass", ctx.strong.label, page.file, [], chars, from_checkpoint=cached,
                    note="the same excerpts plus the draft",
                    deferred="its prompt contains the draft reply, unknown until the draft call answers",
                )
            )  # fmt: skip
        chars = size(repair_prompt(ctx.name, page, draft, ["a problem"] * 3, ex.text, where=ctx.origin))
        calls.append(
            PlannedCall(
                "repair", ctx.cheap.label, page.file, [], chars, only_if_needed=True, from_checkpoint=cached,
                note="the same excerpts plus the page and its validation problems",
                deferred="built only if the finished page fails validation; it contains that page",
            )
        )  # fmt: skip
    return calls


def prepared_stages(
    root: Path,
    snap: Snapshot,
    s: Survey,
    settings: Settings,
    name: str,
    cheap: Runner,
    strong: Runner,
    checkpoints: Checkpoints | None,
) -> Estimate:
    """Every stage `generate` would run, prepared exactly as a run prepares it (sealed, nothing sent).

    Shared by `preview` and by a run bound to a preview: the plan envelope, and, when the outline is
    already checkpointed, each page's draft envelope. Later stages are deferred.
    """
    est = Estimate([plan_call(snap, s, settings, name, cheap)])
    entry = saved_plan_entry(snap, s, settings, cheap, name, checkpoints)
    if entry is None:
        est.outline_known = False
        est.calls.append(PlannedCall(
            "draft", cheap.label, "(every page)", [], 0, counted=False,
            deferred="the outline, and so every page's prompt, is not known until the plan call answers",
        ))  # fmt: skip
    else:
        pages, raw = entry
        est.calls[0].from_checkpoint = True
        est.calls[0].reused = _sha(raw)
        ctx = Context(root, snap, settings, name, cheap, strong, pages, lambda _m: None, checkpoints=checkpoints)
        est.calls += page_calls(ctx, pages)
    return est


def preview(root: Path, settings: Settings, *, ref: str = "HEAD") -> str:
    """Exactly what `generate` would send, to which provider, without sending anything.

    Inert: runners are only described (nothing is verified, discovered or contacted), no transaction
    is recovered and nothing is written, checkpoints included. Repeating it gives identical output.
    """
    auth = authorize(root, settings, ref, "preview")
    snap = Snapshot.at(root, ref)
    root = snap.root
    name = settings.repo or repo_name(root)
    cheap, strong = settings.describe_runners()
    s = survey(snap, settings.wiki_dir, policy=outbound_policy(settings))
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    est = prepared_stages(root, snap, s, settings, name, cheap, strong, checkpoints)
    header = f"repowiki preview: {name} at {snap.commit[:12]}\n"
    withheld = [f for f in snap.files if settings.policy.secret(f)]
    rendered = est.render(
        withheld, digest=est.digest(auth.identity, snap.commit), settings=settings.effective(),
        budget_tokens=settings.budget_tokens,
    )
    return header + rendered


def check_budget(est: Estimate, meter: Meter, settings: Settings) -> None:
    """Stop before drafting when the estimate already exceeds the token budget."""
    if settings.budget_tokens is not None and meter.tokens + est.expected_tokens > settings.budget_tokens:
        raise GenerateError(
            f"estimated ~{meter.tokens + est.expected_tokens:,} prompt tokens exceeds the budget of "
            f"{settings.budget_tokens:,}; nothing was drafted (the outline is checkpointed). Raise --budget, "
            "or lower max_pages or context_chars."
        )


def generate(
    root: Path,
    settings: Settings,
    *,
    log: Log = print,
    plan_only: bool = False,
    ref: str = "HEAD",
    expect_preview: str | None = None,
) -> Result:
    """Plan, write, validate and publish the wiki.

    With `expect_preview` (the digest `preview` printed), every ready stage is prepared first and
    nothing is sent unless they match the preview exactly.
    """
    from .validate import validate

    auth = authorize(root, settings, ref, "plan" if plan_only else "generate")
    snap = Snapshot.at(root, ref)
    root = snap.root
    name = settings.repo or repo_name(root)
    cheap, strong = settings.runners()
    wiki = wiki_path(root, settings.wiki_dir)
    with wiki_lock(wiki):
        if recover(wiki):
            log("  rolled back an interrupted publish from an earlier run")
        candidate_base = base_digest(wiki)
        old = old_page_files(wiki, settings.wiki_dir, settings.policy)
    log(f"repowiki: {name} at {snap.commit[:12]} (cheap={cheap.label}, strong={strong.label})")
    s = survey(snap, settings.wiki_dir, policy=outbound_policy(settings))
    if not s.files:
        raise GenerateError("no citable files at this commit")
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    meter = Meter(budget_tokens=settings.budget_tokens)
    expected = None
    if expect_preview is not None:
        bound = prepared_stages(root, snap, s, settings, name, cheap, strong, checkpoints)
        if bound.digest(auth.identity, snap.commit) != expect_preview:
            raise StaleEnvelopeError(
                "the prepared prompts differ from the preview (source, configuration, previous pages or "
                "checkpoints changed); nothing was sent. Preview again."
            )
        expected = bound.stages()
    pages = plan(snap, s, settings, cheap, name, log, checkpoints=checkpoints, meter=meter, expected=expected)
    log(f"  outline: {len(pages)} pages")
    for p in pages:
        log(f"    {p.file}: {p.title}" + (f" (under {p.parent})" if p.parent else ""))
    ctx = Context(
        root, snap, settings, name, cheap, strong, pages, log, checkpoints=checkpoints, meter=meter, expected=expected
    )
    est = Estimate(page_calls(ctx, pages))
    log(f"  estimate for the pages: {est.summary()}")
    if plan_only:
        return Result(pages, [], snap.commit, [], [])
    check_budget(est, meter, settings)
    ctx.candidate_base = candidate_base
    texts = write_pages(ctx, pages)
    index = index_data(name, snap.commit, pages)
    publish(ctx, wiki, texts, index, old)
    problems = [
        p
        for p in validate(root, settings.wiki_dir, policy=settings.policy, min_citations=settings.min_citations)
        if p.level == "error"
    ]
    return Result(pages, list(texts), snap.commit, ctx.warnings, problems)


def _drop_bad_fragments(ctx: Context, texts: dict[str, str], everything: dict[str, str]) -> dict[str, str]:
    """Unlink cross-page links in the pages written this run whose fragment names no heading of the
    target's candidate text (each page was written before the others existed). Pages kept from disk
    are not edited: a bad fragment there fails validation like any other error."""
    fragments = Fragments(everything)
    out: dict[str, str] = {}
    for file, text in texts.items():
        bad = {link.href for link in find_links(text) if fragment_problem(link, file, ctx.wiki_dir, fragments)}
        if bad:
            text = unlink(text, bad)
            gone = ", ".join(sorted(bad))[:300]
            ctx.warnings.append(f"{file}: removed {len(bad)} link(s) to missing headings: {gone}")
        out[file] = text
    return out


def publish(ctx: Context, wiki: Path, texts: dict[str, str], index: dict[str, Any], old: set[str]) -> None:
    with wiki_lock(wiki):
        recover(wiki)
        check_base(wiki, ctx.candidate_base)
        _publish(ctx, wiki, texts, index, old)


def _publish(ctx: Context, wiki: Path, texts: dict[str, str], index: dict[str, Any], old: set[str]) -> None:
    """Validate the complete new wiki in memory; only if it passes, swap it into place."""
    pages = {p["file"] for p in index["pages"]}
    everything = dict(texts)
    for file in pages - set(texts):  # unchanged pages (update) are validated too
        text = read_wiki_file(wiki, file)
        if text is not None:
            everything[file] = text
    texts = _drop_bad_fragments(ctx, texts, everything)
    everything.update(texts)
    problems = check_wiki(
        index,
        everything,
        root=ctx.root,
        wiki_dir=ctx.wiki_dir,
        wiki=wiki,
        snap=ctx.snap,
        policy=ctx.policy,
        removed=old - pages,
        min_citations=ctx.settings.min_citations,
        repo_identities=ctx.identities,
    )
    errors = [p for p in problems if p.level == "error"]
    if errors:
        detail = "\n  ".join(str(p) for p in errors[:20])
        raise GenerateError(f"the new wiki failed validation; the wiki on disk was not changed:\n  {detail}")
    ctx.warnings += save(wiki, texts, index, old)


__all__ = [
    "Context", "GenerateError", "PageError", "Result", "StaleEnvelopeError", "citable", "generate", "plan",
    "preview", "publish", "write_pages",
]  # fmt: skip
