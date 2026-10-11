"""Plan an outline, write pages, validate them and write docs/wiki/index.json."""

from __future__ import annotations

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
from pathlib import Path
from typing import Any

from .approval import Authorization, authorize, recheck
from .checkpoint import Checkpoints, key
from .config import Settings
from .estimate import TYPICAL_DRAFT_CHARS, Estimate, Payload, PlannedCall, approx_tokens
from .git import Snapshot, repo_name
from .links import cited_files, find_links, outside_code, relative_from_page, unlink
from .policy import DEFAULT_POLICY, Policy, is_secret, redact
from .prompts import PlannedPage, page_prompt, plan_prompt, repair_prompt, revise_prompt
from .runners import Runner, RunnerError
from .safety import (
    GENERATED_MARK,
    INDEX,
    generated_by_repowiki,
    read_wiki_file,
    recover,
    transact,
    wiki_file,
    wiki_path,
)
from .survey import Excerpts, Survey, citable, excerpts, survey, tree_listing
from .validate import Problem, check_index, check_page, check_wiki, link_problem

Log = Callable[[str], None]


class GenerateError(RuntimeError):
    """Generation could not produce a valid wiki."""


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


def plan_request(snap: Snapshot, s: Survey, settings: Settings, name: str) -> str:
    return plan_prompt(name, s, tree_listing(snap, s.files), settings.min_pages, settings.max_pages)


def _fallback_sources(s: Survey) -> list[str]:
    return (s.entry_points + ([s.readme] if s.readme else []))[:3] or s.files[:3]


def saved_plan(
    snap: Snapshot, s: Survey, settings: Settings, runner: Runner, name: str, checkpoints: Checkpoints | None
) -> list[PlannedPage] | None:
    """The outline checkpointed by an earlier run with the same inputs, if any (no model call)."""
    if checkpoints is None:
        return None
    saved = checkpoints.get("plan", checkpoint_key("plan", runner, recheck(settings).identity, plan_request(snap, s, settings, name)))
    if saved is None:
        return None
    try:
        data = json.loads(saved)
        return outline_from_json(data, set(s.files), settings.min_pages, settings.max_pages, _fallback_sources(s))
    except ValueError:
        return None


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
) -> list[PlannedPage]:
    if getattr(settings, "_authorization", None) is None:
        authorize(snap.root, settings, snap.commit, "plan")
    auth = recheck(settings, runner)
    if auth.operation not in ("generate", "plan"):
        raise ValueError("planning operation is not approved")
    if auth.root != snap.root.resolve() or auth.source_commit != snap.commit:
        raise ValueError("plan snapshot differs from authorized source")
    s = survey(snap, settings.wiki_dir, policy=settings.context_policy)
    prompt = plan_request(snap, s, settings, name)
    fileset = set(s.files)
    fallback = _fallback_sources(s)
    k = checkpoint_key("plan", runner, auth.identity, prompt)
    saved = saved_plan(snap, s, settings, runner, name, checkpoints)
    if saved is not None:
        log("  reusing the saved outline for this commit")
        return saved
    reply = ""
    for attempt in (1, 2):
        try:
            reply = call(runner, prompt, settings, log, meter)
            data = extract_json(reply)
            pages = outline_from_json(data, fileset, settings.min_pages, settings.max_pages, fallback)
            if checkpoints:
                checkpoints.put("plan", k, json.dumps(data))
            return pages
        except (RunnerError, ValueError) as exc:
            log(f"  plan attempt {attempt} with {runner.label} failed: {exc}")
            if isinstance(exc, RunnerError):
                raise GenerateError(str(exc)) from exc
            prompt += f"\n\nYour previous reply could not be used ({exc}). Reply with the JSON object only.\n"
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


def call(runner: Runner, prompt: str, settings: Settings, log: Log, meter: Meter | None = None) -> str:
    """One model call with bounded retries and exponential backoff for transient failures."""
    if recheck(settings, runner).operation not in ("generate", "plan", "update"):
        raise ValueError("model execution operation is not approved")
    if meter is None:
        meter = getattr(settings, "_call_meter", None) or Meter(budget_tokens=settings.budget_tokens)
        settings._call_meter = meter
    meter.budget_tokens = min(meter.budget_tokens or settings.budget_tokens, settings.budget_tokens)
    for attempt in range(settings.retries + 1):
        recheck(settings, runner)
        if meter is not None:
            meter.charge(prompt)
        try:
            return runner.run(prompt)
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
    text: str, page_file: str, wiki_dir: str, snap: Snapshot, page_files: set[str], policy: Policy = DEFAULT_POLICY
) -> str:
    """Rewrite repo-root citation targets into links relative to the page (never to secret files)."""

    def fix(href: str) -> str:
        if re.match(r"^[a-z][a-z0-9+.-]*:|^#|^//", href, re.I):
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


def with_sources_block(text: str, page_file: str, wiki_dir: str, extra: list[str]) -> str:
    """Insert a fresh 'Relevant source files' block after the title and the generated-page marker at the end."""
    body = DETAILS_RE.sub("", text).replace(GENERATED_MARK, "")
    sources = list(dict.fromkeys(cited_files(body, page_file, wiki_dir) + extra))
    items = "\n".join(f"- [{s}]({relative_from_page(page_file, wiki_dir, s)})" for s in sources)
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

    @property
    def wiki_dir(self) -> str:
        return self.settings.wiki_dir

    @property
    def wiki_path(self) -> Path:
        return wiki_path(self.root, self.settings.wiki_dir)

    @property
    def policy(self) -> Policy:
        return self.settings.policy

    @property
    def page_files(self) -> set[str]:
        return {p.file for p in self.pages}

    def call(self, runner: Runner, prompt: str) -> str:
        return call(runner, prompt, self.settings, self.log, self.meter)


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
    )


def page_sources(ctx: Context, page: PlannedPage) -> list[str]:
    return [s for s in page.sources if ctx.snap.has_file(s) and not ctx.settings.context_policy.secret(s)]


def page_excerpts(ctx: Context, page: PlannedPage) -> Excerpts:
    return excerpts(ctx.snap, page_sources(ctx, page), ctx.settings.context_chars, ctx.settings.context_policy)


def authorized_context(ctx: Context) -> Authorization:
    """The current authorization, refusing a context whose clone or snapshot it does not cover."""
    auth = recheck(ctx.settings)
    if (auth.root != ctx.root.resolve() or auth.root != ctx.snap.root.resolve()
            or auth.source_commit != ctx.snap.commit):
        raise ValueError("page snapshot differs from authorized source")
    return auth


def wiki_context_denied(ctx: Context, page: PlannedPage) -> bool:
    """Whether a previous wiki page (its text, title or file name) may not reach a prompt."""
    return ("previous_wiki" not in ctx.settings.privacy["context_classes"]
            or ctx.settings.context_policy.secret(f"{ctx.wiki_dir}/{page.file}"))


def page_request(ctx: Context, page: PlannedPage, previous: str | None = None) -> tuple[str, Excerpts]:
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
    return page_prompt(ctx.name, page, others, ex.text, previous), ex


MAX_PREVIOUS_CHARS = 30_000


def page_key(ctx: Context, prompt: str) -> str:
    return checkpoint_key("page", ctx.cheap, recheck(ctx.settings).identity, ctx.strong.label, str(ctx.settings.min_citations), prompt)


def _finish(ctx: Context, page: PlannedPage, reply: str, shown: list[str]) -> str:
    text = clean_reply(redact(reply), page.title)
    text = relink(text, page.file, ctx.wiki_dir, ctx.snap, ctx.page_files, ctx.policy)
    return with_sources_block(text, page.file, ctx.wiki_dir, shown)


def write_page(ctx: Context, page: PlannedPage, previous: str | None = None) -> str:
    """Draft, revise, repair and verify one page. Raises PageError if it cannot be made valid."""
    if authorized_context(ctx).operation not in ("generate", "update"):
        raise ValueError("page generation operation is not approved")
    prompt, ex = page_request(ctx, page, previous)
    k = page_key(ctx, prompt)
    saved = ctx.checkpoints.get("page", k) if ctx.checkpoints else None
    if saved is not None and not [p for p in _problems(ctx, page, saved) if p.level == "error"]:
        ctx.log(f"  {page.file}: reusing the checkpoint from an earlier run")
        return saved
    shown = ex.included  # the sources block lists only files the model actually saw
    ctx.log(f"  writing {page.file} ({page.title}) with {ctx.cheap.label}")
    text = _finish(ctx, page, ctx.call(ctx.cheap, prompt), shown)
    if ctx.strong is not ctx.cheap:
        try:
            text = _finish(ctx, page, ctx.call(ctx.strong, revise_prompt(ctx.name, page, text, ex.text)), shown)
        except BudgetError:
            raise
        except RunnerError as exc:
            ctx.warnings.append(f"{page.file}: final pass failed, kept the draft ({exc})")
    probs = [p for p in _problems(ctx, page, text) if p.level == "error"]
    if probs:
        ctx.log(f"  {page.file}: {len(probs)} problem(s), asking {ctx.cheap.label} to repair")
        try:
            reply = ctx.call(ctx.cheap, repair_prompt(ctx.name, page, text, [p.message for p in probs], ex.text))
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
        text = with_sources_block(unlink(text, bad), page.file, ctx.wiki_dir, sources)
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
    """Write pages and index.json inside `wiki` and delete pages dropped from the index, as one transaction.

    Plain file names only, no symlinks. A dropped page is deleted only if repowiki
    wrote it (it carries the generated-page marker). Returns warnings about pages kept.
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
        if target.exists() and not generated_by_repowiki(target):
            warnings.append(f"kept {stale}: dropped from the index but not written by repowiki")
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
    tree = tree_listing(snap, s.files)
    payloads = []
    if s.readme:
        payloads.append(
            Payload(s.readme, len(s.readme_text.encode()), f"text, first {len(s.readme_text):,} characters")
        )
    payloads.append(Payload("(file list)", len(tree.encode()), f"{len(s.files)} paths and sizes, no file contents"))
    note = "plus the names (not contents) of manifests, entry points and tests"
    return PlannedCall("plan", runner.label, None, payloads, len(plan_request(snap, s, settings, name)), note=note)


def page_calls(ctx: Context, pages: list[PlannedPage], previous: dict[str, str] | None = None) -> list[PlannedCall]:
    """The draft, final-pass and repair calls each page would make, with the exact files each carries."""
    calls: list[PlannedCall] = []
    for page in pages:
        old = (previous or {}).get(page.file)
        prompt, ex = page_request(ctx, page, old)
        cached = bool(ctx.checkpoints and ctx.checkpoints.get("page", page_key(ctx, prompt)) is not None)
        payloads = [
            Payload(
                path, ex.sizes.get(path, 0), f"lines 1-{n} of {total}" if n else f"omitted (over budget), {total} lines"
            )
            for path, (n, total) in ex.shown.items()
        ]
        if old:
            payloads.append(Payload(f"{ctx.wiki_dir}/{page.file}", len(old.encode()), "previous version of the page"))
        calls.append(PlannedCall("draft", ctx.cheap.label, page.file, payloads, len(prompt), from_checkpoint=cached))
        draft = "x" * TYPICAL_DRAFT_CHARS
        if ctx.strong is not ctx.cheap:
            chars = len(revise_prompt(ctx.name, page, draft, ex.text))
            calls.append(
                PlannedCall(
                    "final pass", ctx.strong.label, page.file, [], chars, from_checkpoint=cached,
                    note="the same excerpts plus the draft",
                )
            )  # fmt: skip
        chars = len(repair_prompt(ctx.name, page, draft, ["a problem"] * 3, ex.text))
        calls.append(
            PlannedCall(
                "repair", ctx.cheap.label, page.file, [], chars, only_if_needed=True, from_checkpoint=cached,
                note="the same excerpts plus the page and its validation problems",
            )
        )  # fmt: skip
    return calls


def preview(root: Path, settings: Settings, *, ref: str = "HEAD") -> str:
    """Exactly what `generate` would send, to which provider, without sending anything."""
    authorize(root, settings, ref, "preview")
    snap = Snapshot.at(root, ref)
    root = snap.root
    name = settings.repo or repo_name(root)
    cheap, strong = settings.runners()
    s = survey(snap, settings.wiki_dir, policy=settings.context_policy)
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    est = Estimate([plan_call(snap, s, settings, name, cheap)])
    pages = saved_plan(snap, s, settings, cheap, name, checkpoints)
    if pages is None:
        est.outline_known = False
    else:
        est.calls[0].from_checkpoint = True
        ctx = Context(root, snap, settings, name, cheap, strong, pages, lambda _m: None, checkpoints=checkpoints)
        est.calls += page_calls(ctx, pages)
    header = f"repowiki preview: {name} at {snap.commit[:12]}\n"
    return header + est.render([f for f in snap.files if settings.policy.secret(f)])


def check_budget(est: Estimate, meter: Meter, settings: Settings) -> None:
    """Stop before drafting when the estimate already exceeds the token budget."""
    if settings.budget_tokens is not None and meter.tokens + est.expected_tokens > settings.budget_tokens:
        raise GenerateError(
            f"estimated ~{meter.tokens + est.expected_tokens:,} prompt tokens exceeds the budget of "
            f"{settings.budget_tokens:,}; nothing was drafted (the outline is checkpointed). Raise --budget, "
            "or lower max_pages or context_chars."
        )


def generate(root: Path, settings: Settings, *, log: Log = print, plan_only: bool = False, ref: str = "HEAD") -> Result:
    from .validate import validate

    authorize(root, settings, ref, "plan" if plan_only else "generate")
    snap = Snapshot.at(root, ref)
    root = snap.root
    name = settings.repo or repo_name(root)
    cheap, strong = settings.runners()
    wiki = wiki_path(root, settings.wiki_dir)
    log(f"repowiki: {name} at {snap.commit[:12]} (cheap={cheap.label}, strong={strong.label})")
    s = survey(snap, settings.wiki_dir, policy=settings.context_policy)
    if not s.files:
        raise GenerateError("no citable files at this commit")
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    meter = Meter(budget_tokens=settings.budget_tokens)
    pages = plan(snap, s, settings, cheap, name, log, checkpoints=checkpoints, meter=meter)
    log(f"  outline: {len(pages)} pages")
    for p in pages:
        log(f"    {p.file}: {p.title}" + (f" (under {p.parent})" if p.parent else ""))
    ctx = Context(root, snap, settings, name, cheap, strong, pages, log, checkpoints=checkpoints, meter=meter)
    est = Estimate(page_calls(ctx, pages))
    log(f"  estimate for the pages: {est.summary()}")
    if plan_only:
        return Result(pages, [], snap.commit, [], [])
    check_budget(est, meter, settings)
    texts = write_pages(ctx, pages)
    index = index_data(name, snap.commit, pages)
    if recover(wiki):
        log("  rolled back an interrupted publish from an earlier run")
    old = old_page_files(wiki, settings.wiki_dir, settings.policy)
    publish(ctx, wiki, texts, index, old)
    problems = [
        p
        for p in validate(root, settings.wiki_dir, policy=settings.policy, min_citations=settings.min_citations)
        if p.level == "error"
    ]
    return Result(pages, list(texts), snap.commit, ctx.warnings, problems)


def publish(ctx: Context, wiki: Path, texts: dict[str, str], index: dict[str, Any], old: set[str]) -> None:
    """Validate the complete new wiki in memory; only if it passes, swap it into place."""
    pages = {p["file"] for p in index["pages"]}
    everything = dict(texts)
    for file in pages - set(texts):  # unchanged pages (update) are validated too
        text = read_wiki_file(wiki, file)
        if text is not None:
            everything[file] = text
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
    )
    errors = [p for p in problems if p.level == "error"]
    if errors:
        detail = "\n  ".join(str(p) for p in errors[:20])
        raise GenerateError(f"the new wiki failed validation; the wiki on disk was not changed:\n  {detail}")
    ctx.warnings += save(wiki, texts, index, old)


__all__ = [
    "Context", "GenerateError", "PageError", "Result", "citable", "generate", "plan", "preview", "publish",
    "write_pages",
]  # fmt: skip
