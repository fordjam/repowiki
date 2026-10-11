"""Incremental refresh: re-write only pages whose cited files or directories changed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .checkpoint import Checkpoints
from .config import Settings
from .estimate import Estimate
from .generate import (
    Context,
    GenerateError,
    Log,
    Meter,
    check_budget,
    index_data,
    page_calls,
    publish,
    write_pages,
)
from .git import Snapshot, changed_files, has_commit, renamed_files
from .links import cited_files
from .policy import redact
from .prompts import PlannedPage
from .safety import base_digest, wiki_lock, INDEX, read_wiki_file, recover, wiki_path
from .validate import Problem, check_index, validate

MAX_UPDATE_SOURCES = 12


@dataclass
class UpdatePlan:
    old_commit: str
    new_commit: str
    changed: set[str]
    stale: dict[str, list[str]]  # page file -> changed files it cites (directly or through a directory)
    renamed: dict[str, str] = field(default_factory=dict)  # old path -> new path
    candidate_base: str | None = None
    previous: dict[str, str] = field(default_factory=dict)


@dataclass
class UpdateResult:
    plan: UpdatePlan
    written: list[str]
    warnings: list[str]
    problems: list[Problem]


def _summary(text: str) -> str:
    """First prose paragraph of a page (after the title and sources block)."""
    body = re.sub(r"<details>.*?</details>", "", text, flags=re.S)
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if para and not para.startswith(("#", "```", "<", "|", "-", "Sources:")):
            return " ".join(para.split())[:300]
    return ""


def _under(path: str, cited: str) -> bool:
    return path == cited or path.startswith(cited.rstrip("/") + "/")


def changed_hits(cited: list[str], changed: set[str]) -> list[str]:
    """Changed files a page depends on: cited files, and anything under a cited directory."""
    return sorted({f for f in changed for c in cited if _under(f, c)})


def plan_update(root: Path, settings: Settings, ref: str = "HEAD") -> tuple[UpdatePlan, dict, Snapshot]:
    with wiki_lock(wiki_path(root, settings.wiki_dir)):
        return _plan_update(root, settings, ref)


def _plan_update(root: Path, settings: Settings, ref: str = "HEAD") -> tuple[UpdatePlan, dict, Snapshot]:
    wiki = wiki_path(root, settings.wiki_dir)
    recover(wiki)  # undo a publish an earlier run did not finish
    try:
        data = json.loads(read_wiki_file(wiki, INDEX) or "")
    except (OSError, ValueError) as exc:
        raise GenerateError(f"cannot read {settings.wiki_dir}/{INDEX}: {exc}; run `repowiki generate` first") from exc
    probs, entries = check_index(data, wiki_dir=settings.wiki_dir, policy=settings.policy)
    if probs:
        raise GenerateError("; ".join(str(p) for p in probs))
    old = data["indexed_commit"]
    if not has_commit(root, old):
        raise GenerateError(f"indexed commit {old[:12]} is not in this repository; run `repowiki generate`")
    snap = Snapshot.at(root, ref)
    changed = changed_files(root, old, snap.commit) if old != snap.commit else set()
    renamed = renamed_files(root, old, snap.commit) if changed else {}
    wiki_prefix = settings.wiki_dir.rstrip("/") + "/"
    changed = {f for f in changed if not f.startswith(wiki_prefix)}
    stale: dict[str, list[str]] = {}
    previous: dict[str, str] = {}
    for e in entries:
        text = read_wiki_file(wiki, e.file)
        if text is None:
            stale[e.file] = ["(page missing)"]
            continue
        previous[e.file] = text
        hits = changed_hits(cited_files(text, e.file, settings.wiki_dir), changed)
        if hits:
            stale[e.file] = hits
    return UpdatePlan(old, snap.commit, changed, stale, candidate_base=base_digest(wiki),
                      previous=previous, renamed=renamed), data, snap


def update_sources(cited: list[str], up: UpdatePlan, snap: Snapshot, settings: Settings) -> tuple[list[str], str]:
    """Context for re-writing a page: its cited files at the new commit, following renames,
    plus changed files under cited directories. Returns (sources, a note for the prompt)."""
    sources: list[str] = []
    notes: list[str] = []
    for path in cited:
        if snap.has_file(path):
            sources.append(path)
        elif path in up.renamed:
            sources.append(up.renamed[path])
            notes.append(f"- {path} was renamed to {up.renamed[path]}; cite the new path.")
        elif snap.has_dir(path):
            sources += [f for f in sorted(up.changed) if _under(f, path) and snap.has_file(f)]
        elif path in up.changed:
            notes.append(f"- {path} was deleted; remove or replace what the page says about it.")
    sources = settings.policy.filter(dict.fromkeys(sources))[:MAX_UPDATE_SOURCES]
    note = ("Changes to cited files:\n" + "\n".join(notes) + "\n") if notes else ""
    return sources, note


def update(
    root: Path, settings: Settings, *, log: Log = print, dry_run: bool = False, ref: str = "HEAD"
) -> UpdateResult:
    up, data, snap = plan_update(root, settings, ref)
    root = snap.root
    log(f"repowiki update: {up.old_commit[:7]} -> {up.new_commit[:7]}, {len(up.changed)} file(s) changed")
    for file, hits in up.stale.items():
        log(f"  stale: {file} (cites {', '.join(hits[:5])}{' ...' if len(hits) > 5 else ''})")

    wiki = wiki_path(root, settings.wiki_dir)
    pages: list[PlannedPage] = []
    previous: dict[str, str] = {}
    for entry in data["pages"]:
        file = entry["file"]
        text = redact(up.previous.get(file, ""))
        sources, note = update_sources(cited_files(text, file, settings.wiki_dir), up, snap, settings)
        pages.append(PlannedPage(file, entry["title"], entry.get("parent"), _summary(text), sources, note))
        if text:
            previous[file] = text
    todo = [p for p in pages if p.file in up.stale]
    cheap, strong = settings.runners()
    meter = Meter(budget_tokens=settings.budget_tokens)
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    ctx = Context(root, snap, settings, data["repo"], cheap, strong, pages, log, checkpoints=checkpoints, meter=meter)
    ctx.candidate_base = up.candidate_base
    est = Estimate(page_calls(ctx, todo, previous))
    if todo:
        log(f"  estimate: {est.summary()}")
    if dry_run:
        if todo:
            log(est.render([f for f in snap.files if settings.policy.secret(f)]))
        return UpdateResult(up, [], [], [])
    check_budget(est, meter, settings)
    written: dict[str, str] = {}
    if todo:
        written = write_pages(ctx, todo, previous)
    else:
        log("  no page cites a changed file; moving indexed_commit only")
    publish(ctx, wiki, written, index_data(data["repo"], snap.commit, pages), set())
    policy, minimum = settings.policy, settings.min_citations
    problems = [
        p for p in validate(root, settings.wiki_dir, policy=policy, min_citations=minimum) if p.level == "error"
    ]
    return UpdateResult(up, list(written), ctx.warnings, problems)
