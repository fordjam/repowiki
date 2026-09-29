"""Plan an outline, write pages, validate them and write docs/wiki/index.json."""

from __future__ import annotations

import json
import posixpath
import re
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .git import Snapshot, repo_name
from .links import cited_files, find_links, relative_from_page
from .prompts import PlannedPage, page_prompt, plan_prompt, repair_prompt, revise_prompt
from .runners import Runner, RunnerError
from .survey import Survey, citable, excerpts, survey, tree_listing
from .validate import INDEX, Problem, check_page, link_problem

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


def outline_from_json(data: Any, files: set[str], min_pages: int, max_pages: int) -> list[PlannedPage]:
    """Turn a planner reply into numbered pages; raises ValueError if unusable."""
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
        parent_ref = item.get("parent")
        parent = None
        if isinstance(parent_ref, str) and parent_ref.strip().lower() in by_title:
            parent = by_title[parent_ref.strip().lower()]
        elif isinstance(parent_ref, int) and 1 <= parent_ref < n:
            parent = pages[parent_ref - 1].file
        srcs = item.get("sources")
        srcs = srcs if isinstance(srcs, list) else []
        sources = [_strip_dot(s.strip()) for s in srcs if isinstance(s, str)]
        sources = list(dict.fromkeys(s for s in sources if s in files))[:8]
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


def plan(snap: Snapshot, s: Survey, settings: Settings, runner: Runner, name: str, log: Log) -> list[PlannedPage]:
    prompt = plan_prompt(name, s, tree_listing(snap, s.files), settings.min_pages, settings.max_pages)
    fileset = set(s.files)
    reply = ""
    for attempt in (1, 2):
        try:
            reply = runner.run(prompt)
            return outline_from_json(extract_json(reply), fileset, settings.min_pages, settings.max_pages)
        except (RunnerError, ValueError) as exc:
            log(f"  plan attempt {attempt} with {runner.label} failed: {exc}")
            if isinstance(exc, RunnerError) and attempt == 2:
                raise GenerateError(str(exc)) from exc
            prompt += f"\n\nYour previous reply could not be used ({exc}). Reply with the JSON object only.\n"
    log("  falling back to a structure-only outline")
    fallback = heuristic_outline(s.files, s.readme, s.entry_points, s.tests, settings.max_pages)
    return outline_from_json({"pages": fallback}, fileset, 1, settings.max_pages)


# --------------------------------------------------------------------------- page text

FENCE_RE = re.compile(r"^(```|~~~)")
LINK_TARGET_RE = re.compile(r"(\]\()(\s*<?)([^)\s>]+)(>?(?:\s+\"[^\"]*\")?\s*\))")
DETAILS_RE = re.compile(r"<details>\s*<summary>\s*Relevant source files.*?</details>\s*", re.S | re.I)


def _outside_fences(text: str, fn: Callable[[str], str]) -> str:
    out: list[str] = []
    chunk: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = FENCE_RE.match(line.lstrip())
        if fence is None and m:
            out.append(fn("".join(chunk)))
            chunk, fence = [], m.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            closing = line.strip()
            if closing.startswith(fence) and set(closing) == {fence[0]}:
                fence = None
        else:
            chunk.append(line)
    out.append(fn("".join(chunk)))
    return "".join(out)


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


def relink(text: str, page_file: str, wiki_dir: str, snap: Snapshot, page_files: set[str]) -> str:
    """Rewrite repo-root citation targets into links relative to the page."""

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
        if snap.has_file(clean) or snap.has_dir(clean):
            rel = relative_from_page(page_file, wiki_dir, clean)
            return rel + (f"#{frag}" if frag else "")
        return href

    def sub(chunk: str) -> str:
        return LINK_TARGET_RE.sub(lambda m: m.group(1) + m.group(2) + fix(m.group(3)) + m.group(4), chunk)

    return _outside_fences(text, sub)


def unlink_bad(text: str, bad_hrefs: set[str]) -> str:
    """Replace ``[label](href)`` with ``label`` for each bad href (outside code fences)."""
    if not bad_hrefs:
        return text
    pattern = re.compile(r"(!?)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")

    def sub(chunk: str) -> str:
        return pattern.sub(lambda m: m.group(2) if m.group(3) in bad_hrefs else m.group(0), chunk)

    return _outside_fences(text, sub)


def with_sources_block(text: str, page_file: str, wiki_dir: str, extra: list[str]) -> str:
    """Insert a fresh 'Relevant source files' block right after the title."""
    body = DETAILS_RE.sub("", text)
    sources = list(dict.fromkeys(cited_files(body, page_file, wiki_dir) + extra))
    items = "\n".join(f"- [{s}]({relative_from_page(page_file, wiki_dir, s)})" for s in sources)
    block = (
        "<details>\n<summary>Relevant source files</summary>\n\n"
        "The following files were used as context for this page:\n\n"
        f"{items or '- (none)'}\n\n</details>\n"
    )
    title, _, rest = body.partition("\n")
    return f"{title}\n\n{block}\n{rest.lstrip()}".rstrip() + "\n"


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

    @property
    def wiki_dir(self) -> str:
        return self.settings.wiki_dir

    @property
    def wiki_path(self) -> Path:
        return self.root / self.settings.wiki_dir

    @property
    def page_files(self) -> set[str]:
        return {p.file for p in self.pages}


def _problems(ctx: Context, page: PlannedPage, text: str) -> list[Problem]:
    return check_page(
        page.file, text, wiki_dir=ctx.wiki_dir, wiki_path=ctx.wiki_path, snap=ctx.snap, extra_pages=ctx.page_files
    )


def _finish(ctx: Context, page: PlannedPage, reply: str) -> str:
    text = clean_reply(reply, page.title)
    text = relink(text, page.file, ctx.wiki_dir, ctx.snap, ctx.page_files)
    return with_sources_block(text, page.file, ctx.wiki_dir, [s for s in page.sources if ctx.snap.has_file(s)])


def write_page(ctx: Context, page: PlannedPage, previous: str | None = None) -> str:
    sources = [s for s in page.sources if ctx.snap.has_file(s)]
    ex = excerpts(ctx.snap, sources, ctx.settings.context_chars)
    text = _finish(ctx, page, ctx.cheap.run(page_prompt(ctx.name, page, ctx.pages, ex, previous)))
    if ctx.strong is not ctx.cheap:
        try:
            text = _finish(ctx, page, ctx.strong.run(revise_prompt(ctx.name, page, text, ex)))
        except RunnerError as exc:
            ctx.warnings.append(f"{page.file}: final pass failed, kept the draft ({exc})")
    probs = _problems(ctx, page, text)
    if probs:
        ctx.log(f"  {page.file}: {len(probs)} problem(s), asking {ctx.strong.label} to repair")
        try:
            fixed = _finish(
                ctx, page, ctx.strong.run(repair_prompt(ctx.name, page, text, [p.message for p in probs], ex))
            )
            if len(_problems(ctx, page, fixed)) <= len(probs):
                text = fixed
        except RunnerError as exc:
            ctx.warnings.append(f"{page.file}: repair failed ({exc})")
    bad = {
        link.href
        for link in find_links(text)
        if link_problem(
            link, page.file, wiki_dir=ctx.wiki_dir, wiki_path=ctx.wiki_path, snap=ctx.snap, extra_pages=ctx.page_files
        )
    }
    if bad:
        ctx.warnings.append(f"{page.file}: removed {len(bad)} unverifiable link(s): {', '.join(sorted(bad))[:300]}")
        text = with_sources_block(unlink_bad(text, bad), page.file, ctx.wiki_dir, sources)
    return text


def write_pages(ctx: Context, todo: list[PlannedPage], previous: dict[str, str] | None = None) -> dict[str, str]:
    previous = previous or {}

    def one(page: PlannedPage) -> tuple[str, str]:
        ctx.log(f"  writing {page.file} ({page.title}) with {ctx.cheap.label}")
        return page.file, write_page(ctx, page, previous.get(page.file))

    jobs = max(1, ctx.settings.jobs)
    if jobs == 1:
        return dict(one(p) for p in todo)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        return dict(pool.map(one, todo))


def index_data(name: str, commit: str, pages: list[PlannedPage]) -> dict[str, Any]:
    return {
        "repo": name,
        "indexed_commit": commit,
        "indexed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pages": [{"file": p.file, "title": p.title, "parent": p.parent} for p in pages],
    }


def save(wiki_path: Path, texts: dict[str, str], index: dict[str, Any], old_files: set[str] | None = None) -> None:
    wiki_path.mkdir(parents=True, exist_ok=True)
    for file, text in texts.items():
        (wiki_path / file).write_text(text, encoding="utf-8")
    keep = {p["file"] for p in index["pages"]}
    for stale in (old_files or set()) - keep:
        target = wiki_path / stale
        if target.is_file():
            target.unlink()
    (wiki_path / INDEX).write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@dataclass
class Result:
    pages: list[PlannedPage]
    written: list[str]
    commit: str
    warnings: list[str]
    problems: list[Problem]


def old_page_files(wiki_path: Path) -> set[str]:
    try:
        data = json.loads((wiki_path / INDEX).read_text(encoding="utf-8"))
        return {p["file"] for p in data.get("pages", []) if isinstance(p, dict) and isinstance(p.get("file"), str)}
    except (OSError, ValueError, AttributeError):
        return set()


def generate(root: Path, settings: Settings, *, log: Log = print, plan_only: bool = False, ref: str = "HEAD") -> Result:
    from .validate import validate

    snap = Snapshot.at(root, ref)
    root = snap.root
    name = settings.repo or repo_name(root)
    cheap, strong = settings.runners()
    log(f"repowiki: {name} at {snap.commit[:12]} (cheap={cheap.label}, strong={strong.label})")
    s = survey(snap, settings.wiki_dir)
    if not s.files:
        raise GenerateError("no citable files at this commit")
    pages = plan(snap, s, settings, cheap, name, log)
    log(f"  outline: {len(pages)} pages")
    for p in pages:
        log(f"    {p.file}: {p.title}" + (f" (under {p.parent})" if p.parent else ""))
    if plan_only:
        return Result(pages, [], snap.commit, [], [])
    ctx = Context(root, snap, settings, name, cheap, strong, pages, log)
    texts = write_pages(ctx, pages)
    wiki_path = root / settings.wiki_dir
    save(wiki_path, texts, index_data(name, snap.commit, pages), old_page_files(wiki_path))
    problems = [p for p in validate(root, settings.wiki_dir) if p.level == "error"]
    return Result(pages, list(texts), snap.commit, ctx.warnings, problems)


__all__ = ["Context", "GenerateError", "Result", "citable", "generate", "plan", "write_pages"]
