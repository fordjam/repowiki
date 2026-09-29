"""Incremental refresh: re-write only pages whose cited files changed."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .generate import Context, GenerateError, Log, index_data, publish, write_pages
from .git import Snapshot, changed_files, has_commit
from .links import cited_files
from .prompts import PlannedPage
from .safety import INDEX, read_wiki_file, wiki_path
from .validate import Problem, check_index, validate


@dataclass
class UpdatePlan:
    old_commit: str
    new_commit: str
    changed: set[str]
    stale: dict[str, list[str]]  # page file -> changed files it cites


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


def plan_update(root: Path, settings: Settings, ref: str = "HEAD") -> tuple[UpdatePlan, dict, Snapshot]:
    wiki = wiki_path(root, settings.wiki_dir)
    try:
        data = json.loads(read_wiki_file(wiki, INDEX) or "")
    except (OSError, ValueError) as exc:
        raise GenerateError(f"cannot read {settings.wiki_dir}/{INDEX}: {exc}; run `repowiki generate` first") from exc
    probs, entries = check_index(data)
    if probs:
        raise GenerateError("; ".join(str(p) for p in probs))
    old = data["indexed_commit"]
    if not has_commit(root, old):
        raise GenerateError(f"indexed commit {old[:12]} is not in this repository; run `repowiki generate`")
    snap = Snapshot.at(root, ref)
    changed = changed_files(root, old, snap.commit) if old != snap.commit else set()
    wiki_prefix = settings.wiki_dir.rstrip("/") + "/"
    changed = {f for f in changed if not f.startswith(wiki_prefix)}
    stale: dict[str, list[str]] = {}
    for e in entries:
        text = read_wiki_file(wiki, e.file)
        if text is None:
            stale[e.file] = ["(page missing)"]
            continue
        hits = [f for f in cited_files(text, e.file, settings.wiki_dir) if f in changed]
        if hits:
            stale[e.file] = hits
    return UpdatePlan(old, snap.commit, changed, stale), data, snap


def update(
    root: Path, settings: Settings, *, log: Log = print, dry_run: bool = False, ref: str = "HEAD"
) -> UpdateResult:
    up, data, snap = plan_update(root, settings, ref)
    root = snap.root
    log(f"repowiki update: {up.old_commit[:7]} -> {up.new_commit[:7]}, {len(up.changed)} file(s) changed")
    for file, hits in up.stale.items():
        log(f"  stale: {file} (cites {', '.join(hits[:5])}{' ...' if len(hits) > 5 else ''})")
    if dry_run:
        return UpdateResult(up, [], [], [])

    wiki = wiki_path(root, settings.wiki_dir)
    pages: list[PlannedPage] = []
    previous: dict[str, str] = {}
    for entry in data["pages"]:
        file = entry["file"]
        text = read_wiki_file(wiki, file) or ""
        sources = settings.policy.filter(f for f in cited_files(text, file, settings.wiki_dir) if snap.has_file(f))
        pages.append(PlannedPage(file, entry["title"], entry.get("parent"), _summary(text), sources))
        if text:
            previous[file] = text
    todo = [p for p in pages if p.file in up.stale]
    cheap, strong = settings.runners()
    ctx = Context(root, snap, settings, data["repo"], cheap, strong, pages, log)
    written: dict[str, str] = {}
    if todo:
        written = write_pages(ctx, todo, previous)
    else:
        log("  no page cites a changed file; moving indexed_commit only")
    publish(ctx, wiki, written, index_data(data["repo"], snap.commit, pages), set())
    warnings = ctx.warnings
    problems = [p for p in validate(root, settings.wiki_dir, policy=settings.policy) if p.level == "error"]
    return UpdateResult(up, list(written), warnings, problems)
