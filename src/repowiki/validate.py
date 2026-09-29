"""Checks a wiki must pass: index schema, page shape, links and pinned citations."""

from __future__ import annotations

import json
import posixpath
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .git import SHA_RE, GitError, Snapshot, has_commit
from .links import Link, find_links, repo_path
from .policy import DEFAULT_POLICY, Policy
from .safety import INDEX, UnsafePathError, check_wiki_dir, read_wiki_file, safe_page_file, wiki_path


@dataclass(frozen=True)
class Problem:
    file: str
    line: int
    message: str
    level: str = "error"

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.level}: {self.message}"


@dataclass(frozen=True)
class PageEntry:
    file: str
    title: str
    parent: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "title": self.title, "parent": self.parent}


def check_index(
    data: object, root: Path | None = None, *, wiki_dir: str = "docs/wiki", policy: Policy = DEFAULT_POLICY
) -> tuple[list[Problem], list[PageEntry]]:
    """Schema check for a parsed index.json. `root` enables the commit-exists check.

    Page files whose path matches the secret policy (e.g. ``agent-transcript.md``) are problems
    and are left out of the returned entries, so no caller ever reads, renders or sends them.
    """
    probs: list[Problem] = []
    entries: list[PageEntry] = []

    def bad(msg: str) -> None:
        probs.append(Problem(INDEX, 0, msg))

    if not isinstance(data, dict):
        bad("top level must be an object")
        return probs, entries
    if not isinstance(data.get("repo"), str) or not data["repo"].strip():
        bad('"repo" must be a non-empty string')
    sha = data.get("indexed_commit")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        bad('"indexed_commit" must be a hex commit id')
    elif root is not None and not has_commit(root, sha):
        bad(f'"indexed_commit" {sha[:12]} is not a commit in this repository')
    at = data.get("indexed_at")
    try:
        if not isinstance(at, str):
            raise ValueError
        datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        bad('"indexed_at" must be an ISO 8601 timestamp')
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        bad('"pages" must be a non-empty list')
        return probs, entries

    seen: set[str] = set()
    for n, entry in enumerate(pages):
        if not isinstance(entry, dict):
            bad(f"pages[{n}] must be an object")
            continue
        file, title, parent = entry.get("file"), entry.get("title"), entry.get("parent")
        if not safe_page_file(file):
            bad(f"pages[{n}].file must be a plain .md file name inside the wiki directory, got {file!r}")
            continue
        assert isinstance(file, str)
        if policy.secret(f"{wiki_dir}/{file}"):
            bad(f"pages[{n}].file {file} matches the secret policy; repowiki will not read, render or send it")
            continue
        if file in seen:
            bad(f"pages[{n}].file {file} is listed twice")
            continue
        seen.add(file)
        if not isinstance(title, str) or not title.strip():
            bad(f"pages[{n}].title must be a non-empty string")
            title = file
        if parent is not None and not isinstance(parent, str):
            bad(f"pages[{n}].parent must be a page file name or null")
            parent = None
        entries.append(PageEntry(file, str(title), parent))

    files = {e.file for e in entries}
    by_file = {e.file: e for e in entries}
    for e in entries:
        if e.parent is None:
            continue
        if e.parent not in files:
            bad(f"{e.file}: parent {e.parent!r} is not a listed page")
        elif e.parent == e.file:
            bad(f"{e.file}: a page cannot be its own parent")
        else:
            node, hops = by_file.get(e.parent), 0
            while node is not None and node.parent and hops <= len(entries):
                if node.parent == e.file:
                    bad(f"{e.file}: parent chain forms a cycle")
                    break
                node, hops = by_file.get(node.parent), hops + 1
    return probs, entries


def check_page(
    file: str,
    text: str,
    *,
    wiki_dir: str,
    wiki_path: Path,
    snap: Snapshot | None,
    extra_pages: set[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
    min_citations: int = 0,
) -> list[Problem]:
    """Shape, relative links and citations of one page.

    Links into the wiki directory must exist on disk (or be in `extra_pages`,
    pages about to be written). Everything else must exist at the snapshot
    commit, and any ``#Lx-Ly`` range must lie inside the file at that commit.
    """
    probs: list[Problem] = []
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not first.startswith("# "):
        probs.append(Problem(file, 1, "page must start with a '# ' title"))
    if "<details" not in text or "Relevant source files" not in text:
        probs.append(Problem(file, 0, "missing <details> 'Relevant source files' block"))

    links = find_links(text)
    wiki_prefix = wiki_dir.rstrip("/") + "/"
    citations = 0
    for link in links:
        if link.lines and not link.external and link.path:
            target = repo_path(file, wiki_dir, link.path)
            citations += bool(target and not target.startswith(wiki_prefix))
    if citations < min_citations:
        probs.append(
            Problem(
                file,
                0,
                f"has {citations} line citation(s); at least {min_citations} required (e.g. [a.py:3-9](..#L3-L9))",
            )
        )
    for link in links:
        msg = link_problem(
            link,
            file,
            wiki_dir=wiki_dir,
            wiki_path=wiki_path,
            snap=snap,
            extra_pages=extra_pages,
            policy=policy,
            removed=removed,
        )
        if msg:
            probs.append(Problem(file, link.line, msg))
    return probs


def link_problem(
    link: Link,
    file: str,
    *,
    wiki_dir: str,
    wiki_path: Path,
    snap: Snapshot | None,
    extra_pages: set[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
) -> str | None:
    """Why one link on page `file` is invalid, or None if it is fine.

    `extra_pages` are pages about to be written; `removed` are pages about to be deleted.
    """
    if link.external or not link.path:
        return None  # web links and same-page #anchors
    target = repo_path(file, wiki_dir, link.path)
    if target is None:
        return f"link escapes the repository: {link.href}"
    if policy.secret(target):
        return f"links to a file excluded by the secret policy: {link.href}"
    wiki_prefix = wiki_dir.rstrip("/") + "/"
    if target.startswith(wiki_prefix) or target == wiki_dir:
        inner = target[len(wiki_prefix) :]
        if inner in (extra_pages or set()) or ((wiki_path / inner).exists() and inner not in (removed or set())):
            return None
        return f"broken link: {link.href}"
    if snap is None:
        return None
    lines = link.lines
    at = snap.commit[:7]
    if snap.has_file(target):
        if lines is None:
            if re.match(r"^L\d", link.fragment):
                return f"bad line fragment (want #Lx or #Lx-Ly): {link.href}"
            return None
        count = snap.line_count(target)
        a, b = lines
        if count is None:
            return f"cites lines in a binary file: {link.href}"
        if not 1 <= a <= b:
            return f"line range is empty or reversed: {link.href}"
        if b > count:
            return f"cites lines {a}-{b} but {target} has {count} lines at {at}: {link.href}"
        return None
    if target and snap.has_dir(target):
        return f"line range on a directory: {link.href}" if lines is not None else None
    return f"{target or '.'} does not exist at {at}: {link.href}"


def load_index(wiki: Path) -> object:
    raw = read_wiki_file(wiki, INDEX)
    if raw is None:
        raise FileNotFoundError(wiki / INDEX)
    return json.loads(raw)


def check_wiki(
    data: object,
    texts: dict[str, str],
    *,
    root: Path | None,
    wiki_dir: str,
    wiki: Path,
    snap: Snapshot | None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
    min_citations: int = 0,
) -> list[Problem]:
    """Every check for a whole wiki whose page texts are in `texts` (on disk or about to be written)."""
    probs, entries = check_index(data, root, wiki_dir=wiki_dir, policy=policy)
    names = {e.file for e in entries}
    for e in entries:
        text = texts.get(e.file)
        if text is None:
            probs.append(Problem(e.file, 0, "listed in index.json but missing"))
            continue
        probs += check_page(
            e.file,
            text,
            wiki_dir=wiki_dir,
            wiki_path=wiki,
            snap=snap,
            extra_pages=names,
            policy=policy,
            removed=removed,
            min_citations=min_citations,
        )
    return probs


def validate(
    root: Path, wiki_dir: str = "docs/wiki", *, policy: Policy = DEFAULT_POLICY, min_citations: int = 1
) -> list[Problem]:
    """Validate the wiki in the working tree against the commit named in its index."""
    try:
        wiki_dir = check_wiki_dir(wiki_dir)
        wiki = wiki_path(root, wiki_dir)
        data = load_index(wiki)
    except UnsafePathError as exc:
        return [Problem(wiki_dir, 0, str(exc))]
    except FileNotFoundError:
        return [Problem(f"{wiki_dir}/{INDEX}", 0, "not found (run `repowiki generate` first)")]
    except (OSError, json.JSONDecodeError) as exc:
        return [Problem(INDEX, 0, f"invalid JSON: {exc}")]
    probs: list[Problem] = []
    snap: Snapshot | None = None
    sha = data.get("indexed_commit") if isinstance(data, dict) else None
    if isinstance(sha, str) and SHA_RE.match(sha) and has_commit(root, sha):
        try:
            snap = Snapshot.at(root, sha)
        except GitError as exc:
            probs.append(Problem(INDEX, 0, str(exc)))
    texts: dict[str, str] = {}
    safe = check_index(data, wiki_dir=wiki_dir, policy=policy)[1]
    for e in safe:
        try:
            text = read_wiki_file(wiki, e.file)
        except UnsafePathError as exc:
            probs.append(Problem(e.file, 0, str(exc)))
            continue
        if text is not None:
            texts[e.file] = text
    unsafe = {p.file for p in probs}
    probs += [
        p
        for p in check_wiki(
            data, texts, root=root, wiki_dir=wiki_dir, wiki=wiki, snap=snap, policy=policy, min_citations=min_citations
        )
        if not (p.file in unsafe and p.message.endswith("but missing"))
    ]
    raw_pages = data.get("pages") if isinstance(data, dict) else None
    listed = {p.get("file") for p in raw_pages if isinstance(p, dict)} if isinstance(raw_pages, list) else set()
    for extra in sorted(p.name for p in wiki.glob("*.md")):
        if extra not in listed and posixpath.splitext(extra)[0].lower() != "readme":
            probs.append(Problem(extra, 0, "Markdown file not listed in index.json", level="warning"))
    return probs
