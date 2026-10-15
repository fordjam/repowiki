"""Incremental refresh: re-write only pages whose cited files or directories changed."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import safety
from .approval import authorize, recheck
from .checkpoint import Checkpoints
from .config import Settings
from .estimate import Estimate
from .generate import (
    Context,
    GenerateError,
    Log,
    Meter,
    StaleEnvelopeError,
    check_budget,
    index_data,
    page_calls,
    publish,
    write_pages,
)
from .git import GitError, Snapshot, changed_files, git, renamed_files
from .links import RepoIdentity, canonicalize_links, cited_files
from .outbound import outbound_policy
from .ownership import is_owned
from .policy import redact
from .prompts import PlannedPage
from .safety import base_digest, recover, wiki_file, wiki_lock, wiki_path
from .snapshot import read_frozen
from .validate import Problem, repo_identities, validate

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
    wiki_commit: str = ""
    policy_commit: str = ""


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


def pending_publish(wiki: Path) -> bool:
    """Whether an interrupted publish awaits recovery (checked without changing anything)."""
    record = getattr(safety, "transaction_record", None)  # authoritative record, where the engine keeps one
    if record is not None:
        return os.path.lexists(record(wiki))
    return os.path.lexists(wiki / safety.JOURNAL)


def plan_update(
    root: Path, settings: Settings, ref: str = "HEAD", *, recover_pending: bool = True, wiki_ref: str | None = None
) -> tuple[UpdatePlan, dict, Snapshot]:
    """What changed and which pages are stale. With `recover_pending=False` (dry runs) nothing is
    recovered: a pending interrupted publish is reported instead, and nothing changes."""
    authorize(root, settings, ref, "update")
    with wiki_lock(wiki_path(root, settings.wiki_dir)):
        return _plan_update(root, settings, recover_pending=recover_pending, wiki_ref=wiki_ref)


def _plan_update(
    root: Path, settings: Settings, *, recover_pending: bool = True, wiki_ref: str | None = None
) -> tuple[UpdatePlan, dict, Snapshot]:
    wiki = wiki_path(root, settings.wiki_dir)
    if not recover_pending:
        if pending_publish(wiki):
            raise GenerateError(
                "an interrupted publish is pending; a dry run does not recover it. Run `repowiki validate` "
                "or `repowiki update` (without --dry-run) to recover it first"
            )
    else:
        recover(wiki)  # undo a publish an earlier run did not finish
    selected = wiki_ref or settings.wiki_ref or "refs/heads/main"
    try:
        wiki_snap = Snapshot.at(root, selected)
    except GitError as exc:
        raise GenerateError(f"cannot resolve wiki ref {selected!r}; select an explicit --wiki-ref") from exc
    auth = recheck(settings)
    identities = repo_identities(root)
    frozen = read_frozen(
        root, settings.wiki_dir, ref=wiki_snap.commit, policy_commit=auth.policy_commit, identities=identities
    )
    if not frozen.ok:
        raise GenerateError("committed previous wiki is invalid: " + "; ".join(str(p) for p in frozen.errors))
    data, entries = frozen.index, frozen.entries
    old = data["indexed_commit"]
    snap = Snapshot.at(root, auth.source_commit)
    _check_destination(root, settings)
    _check_destination_matches(wiki, frozen.entries, frozen.texts, frozen.index, selected)
    changed = changed_files(root, old, snap.commit) if old != snap.commit else set()
    renamed = renamed_files(root, old, snap.commit) if changed else {}
    wiki_prefix = settings.wiki_dir.rstrip("/") + "/"
    changed = {f for f in changed if not f.startswith(wiki_prefix)}
    stale: dict[str, list[str]] = {}
    previous = dict(frozen.texts)
    for e in entries:
        text = previous[e.file]
        if not os.path.lexists(wiki_file(wiki, e.file)):
            stale[e.file] = ["(page missing)"]
            continue
        cited = cited_files(text, e.file, settings.wiki_dir, repo_identities=identities, commit=old)
        hits = changed_hits(cited, changed)
        if hits:
            stale[e.file] = hits
    return (
        UpdatePlan(
            old,
            snap.commit,
            changed,
            stale,
            candidate_base=base_digest(wiki),
            previous=previous,
            renamed=renamed,
            wiki_commit=wiki_snap.commit,
            policy_commit=auth.policy_commit,
        ),
        data,
        snap,
    )


def _check_destination(root: Path, settings: Settings) -> None:
    """Inspect destination state only; no local prose becomes context."""
    if git(root, "status", "--porcelain", "--untracked-files=all", "--ignored=matching", "--", settings.wiki_dir):
        raise StaleEnvelopeError("dirty wiki destination; commit or restore local edits first; nothing was sent")


def _check_destination_matches(wiki: Path, entries: list, texts: dict[str, str], index: dict, selected: str) -> None:
    """Staleness is decided from the selected previous wiki, but publication keeps a page it does not
    rewrite exactly as the destination holds it. A clean, committed destination that differs from the
    selected wiki would therefore keep prose the staleness check never saw, so the update refuses: it
    publishes either the selected wiki's pages or nothing."""
    for e in entries:
        path = wiki_file(wiki, e.file)
        if path.is_file() and path.read_bytes() != texts[e.file].encode("utf-8"):
            raise StaleEnvelopeError(
                f"destination page {e.file} differs from the selected previous wiki ({selected}); "
                "update from the checkout that holds that wiki or select the matching --wiki-ref; nothing was sent"
            )
    path = wiki_file(wiki, safety.INDEX)
    if path.is_file():
        try:
            local = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            local = None
        if local != index:
            raise StaleEnvelopeError(
                f"destination index differs from the selected previous wiki ({selected}); "
                "update from the checkout that holds that wiki or select the matching --wiki-ref; nothing was sent"
            )


def update_sources(cited: list[str], up: UpdatePlan, snap: Snapshot, settings: Settings) -> tuple[list[str], str]:
    """Context for re-writing a page: its cited files at the new commit, following renames,
    plus changed files under cited directories. Returns (sources, a note for the prompt)."""
    policy = outbound_policy(settings)
    sources: list[str] = []
    notes: list[str] = []
    for path in cited:
        if policy.secret(path):
            continue
        if snap.has_file(path):
            sources.append(path)
        elif path in up.renamed:
            if policy.secret(up.renamed[path]):
                continue
            sources.append(up.renamed[path])
            notes.append(f"- {path} was renamed to {up.renamed[path]}; cite the new path.")
        elif snap.has_dir(path):
            sources += [f for f in sorted(up.changed) if _under(f, path) and snap.has_file(f)]
        elif path in up.changed:
            notes.append(f"- {path} was deleted; remove or replace what the page says about it.")
    sources = policy.filter(dict.fromkeys(sources))[:MAX_UPDATE_SOURCES]
    note = ("Changes to cited files:\n" + "\n".join(notes) + "\n") if notes else ""
    return sources, note


def repin_unchanged(
    wiki: Path, files: list[str], settings: Settings, identities: tuple[RepoIdentity, ...], up: UpdatePlan
) -> dict[str, str]:
    """Pages kept as they are, whose web citations pin the old indexed commit: the same citations as
    canonical relative links, so they stay valid when the index moves to the new commit. A kept page
    cites no changed file, so each target is unchanged between the two commits."""
    out: dict[str, str] = {}
    for file in files:
        text = up.previous.get(file)
        if not text:
            continue
        fixed = canonicalize_links(
            text, file, settings.wiki_dir, repo_identities=identities, commit=up.old_commit,
            allow=lambda t: not settings.policy.secret(t.path) and t.path not in up.changed,
        )  # fmt: skip
        if fixed != text:
            out[file] = fixed
    return out


def update(
    root: Path,
    settings: Settings,
    *,
    now: datetime,
    log: Log = print,
    dry_run: bool = False,
    ref: str = "HEAD",
    expect_preview: str | None = None,
    wiki_ref: str | None = None,
) -> UpdateResult:
    """Re-write stale pages. `dry_run` is inert (described runners, no recovery, no writes) and logs
    the preview digest; a run given that digest as `expect_preview` sends nothing unless every
    previewed prompt is unchanged. `now` is the run's one instant (the index's `indexed_at`)."""
    up, data, snap = plan_update(root, settings, ref, recover_pending=not dry_run, wiki_ref=wiki_ref)
    root = snap.root
    log(f"repowiki update: {up.old_commit[:7]} -> {up.new_commit[:7]}, {len(up.changed)} file(s) changed")
    for file, hits in up.stale.items():
        log(f"  stale: {file} (cites {', '.join(hits[:5])}{' ...' if len(hits) > 5 else ''})")

    wiki = wiki_path(root, settings.wiki_dir)
    with wiki_lock(wiki):
        for entry in data["pages"]:
            if os.path.lexists(wiki_file(wiki, entry["file"])) and not is_owned(wiki, entry["file"]):
                raise GenerateError(f"{entry['file']}: destination ownership conflict; nothing was sent")
    identities = repo_identities(root)
    pages: list[PlannedPage] = []
    previous: dict[str, str] = {}
    for entry in data["pages"]:
        file = entry["file"]
        text = redact(up.previous.get(file, ""))
        cited = cited_files(text, file, settings.wiki_dir, repo_identities=identities, commit=up.old_commit)
        sources, note = update_sources(cited, up, snap, settings)
        pages.append(PlannedPage(file, entry["title"], entry.get("parent"), _summary(text), sources, note))
        if text:
            previous[file] = text
    todo = [p for p in pages if p.file in up.stale]
    if todo and "previous_wiki" not in settings.privacy["context_classes"]:
        raise ValueError("previous wiki context is not approved")
    cheap, strong = settings.describe_runners() if dry_run else settings.runners()
    meter = Meter(budget_tokens=settings.budget_tokens)
    checkpoints = Checkpoints(root, enabled=settings.checkpoints, reuse=settings.resume)
    ctx = Context(
        root,
        snap,
        settings,
        data["repo"],
        cheap,
        strong,
        pages,
        log,
        checkpoints=checkpoints,
        meter=meter,
        pages_from_wiki=True,
    )
    ctx.candidate_base = up.candidate_base
    settings._update_destination = (wiki, up.candidate_base)
    est = Estimate(page_calls(ctx, todo, previous))
    digest = est.digest(
        recheck(settings).identity + up.old_commit + up.wiki_commit + up.policy_commit + up.candidate_base,
        snap.commit,
    )
    log(f"  source commit: {up.new_commit}; wiki commit: {up.wiki_commit}; policy commit: {up.policy_commit}")
    if todo:
        log(f"  estimate: {est.summary()}")
    if dry_run:
        if todo:
            withheld = [f for f in snap.files if settings.policy.secret(f)]
            effective = settings.effective()
            log(est.render(withheld, digest=digest, settings=effective, budget_tokens=settings.budget_tokens))
        else:
            log(f"Preview digest: {digest}")
        return UpdateResult(up, [], [], [])
    if expect_preview is not None:
        if digest != expect_preview:
            raise StaleEnvelopeError(
                "the prepared prompts differ from the dry run (source, configuration, previous pages or "
                "checkpoints changed); nothing was sent. Run --dry-run again."
            )
        ctx.expected = est.stages()
    check_budget(est, meter, settings)
    written: dict[str, str] = {}
    if todo:
        written = write_pages(ctx, todo, previous)
    else:
        log("  no page cites a changed file; moving indexed_commit only")
    repinned = repin_unchanged(wiki, [p.file for p in pages if p.file not in written], settings, identities, up)
    for file in repinned:
        log(f"  {file}: web citations pinned to {up.old_commit[:7]} rewritten as relative links")
    index = index_data(data["repo"], snap.commit, pages, now=now)
    index.update(previous_wiki_commit=up.wiki_commit, policy_commit=up.policy_commit)
    publish(ctx, wiki, {**repinned, **written}, index, set())
    policy, minimum = settings.policy, settings.min_citations
    problems = [
        p for p in validate(root, settings.wiki_dir, policy=policy, min_citations=minimum) if p.level == "error"
    ]
    return UpdateResult(up, list(written), ctx.warnings, problems)
