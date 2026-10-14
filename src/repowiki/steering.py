"""Committed exact-page steering: ``.repowiki/wiki.json``.

A steering file is an editable plan for the wiki, committed with the repository: notes about the
repository, then an ordered list of pages, each with a stable ID, a file name, a title, a purpose,
an optional parent, page notes, the files or directories it draws on (``sources``) and the files or
directories it is expected to cover (``coverage``).

The file is validated as a whole before anything uses it; any problem raises ``SteeringError`` and
nothing is partly accepted. Steering never widens the privacy boundary: every source or coverage
declaration must be allowed by the secret policy, and a declared directory still only yields the
descendants the policy allows when it is used.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .policy import Policy
from .safety import safe_page_file

STEERING_FILE = ".repowiki/wiki.json"
VERSION = 1
MAX_PAGES = 30
MAX_NOTES = 100  # repository and page note entries, counted once each across the file
MAX_NOTE_CHARS = 10_000
# Enough for the largest valid file: every note character written as a JSON surrogate-pair escape
# (12 bytes), plus page metadata.
MAX_FILE_BYTES = MAX_NOTES * MAX_NOTE_CHARS * 12 + 1_000_000
PAGE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9_-]{0,62}[a-z0-9])?$")

TOP_KEYS = {"version", "repo_notes", "pages"}
PAGE_KEYS = {"id", "file", "title", "purpose", "parent", "notes", "sources", "coverage"}
REQUIRED_PAGE_KEYS = {"id", "file", "title", "purpose"}
NOTE_KEYS = {"text", "author"}
SOURCE_KINDS = ("file", "directory")


class SteeringError(ValueError):
    """The steering file is missing a required part, malformed, unsafe or over a limit."""


@dataclass(frozen=True)
class Note:
    text: str
    author: str | None = None


@dataclass(frozen=True)
class Source:
    kind: str  # "file": exactly this path; "directory": every descendant of this path
    path: str

    def matches(self, path: str) -> bool:
        return path == self.path if self.kind == "file" else path.startswith(self.path + "/")


@dataclass(frozen=True)
class Page:
    id: str
    file: str
    title: str
    purpose: str
    parent: str | None = None
    notes: tuple[Note, ...] = ()
    sources: tuple[Source, ...] = ()
    coverage: tuple[Source, ...] = ()


@dataclass(frozen=True)
class Steering:
    repo_notes: tuple[Note, ...]
    pages: tuple[Page, ...]
    digest: str  # sha256 of the canonical accepted content
    policy: Policy = field(default=Policy(), compare=False, repr=False)  # the exclusions it was accepted under

    def page(self, page_id: str) -> Page:
        for p in self.pages:
            if p.id == page_id:
                return p
        raise KeyError(page_id)

    def children(self, parent: str | None) -> tuple[str, ...]:
        """Page IDs directly under ``parent`` (None: top level), in file order."""
        return tuple(p.id for p in self.pages if p.parent == parent)

    def tree(self) -> list[tuple[str, int]]:
        """Every page as (id, depth), depth-first, siblings in file order."""
        out: list[tuple[str, int]] = []

        def walk(parent: str | None, depth: int) -> None:
            for child in self.children(parent):
                out.append((child, depth))
                walk(child, depth + 1)

        walk(None, 0)
        return out

    def coverage(self) -> dict[str, tuple[tuple[str, str], ...]]:
        """Each page's coverage declarations as (kind, path), in file order."""
        return {p.id: tuple((s.kind, s.path) for s in p.coverage) for p in self.pages}

    def covered_files(self, page_id: str, files: Iterable[str]) -> list[str]:
        """The given repository files a page's coverage declarations name, in the given order.

        A declared directory never yields a descendant the privacy policy excludes."""
        decls = self.page(page_id).coverage
        return [f for f in files if any(d.matches(f) for d in decls) and not self.policy.secret(f)]


def normalize_text(value: str) -> str:
    """Comparison form for titles and purposes: NFKC, whitespace trimmed and collapsed, casefolded."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def load_steering(repo_root: Path, policy: Policy | None = None) -> Steering | None:
    """The repository's validated steering, or None when it has no ``.repowiki/wiki.json``."""
    directory = repo_root / ".repowiki"
    path = repo_root / STEERING_FILE
    # Refuse symlinks before following them: the target may be any file.
    if directory.is_symlink() or path.is_symlink():
        raise SteeringError(f"{STEERING_FILE} must be a regular file, not a symlink")
    if not path.exists():
        return None
    if not path.is_file():
        raise SteeringError(f"{STEERING_FILE} must be a regular file")
    with path.open("rb") as fh:
        raw = fh.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise SteeringError(f"{STEERING_FILE} is too large (limit {MAX_FILE_BYTES} bytes)")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SteeringError(f"{STEERING_FILE} is not valid JSON: {exc}") from None
    return parse_steering(data, policy)


def parse_steering(data: Any, policy: Policy | None = None) -> Steering:
    """Validate parsed steering data as a whole; ``policy`` adds the repository's own exclusions."""
    policy = policy or Policy()
    if not isinstance(data, dict):
        raise SteeringError("steering must be a JSON object")
    _keys(data, TOP_KEYS, {"version", "repo_notes", "pages"}, "steering")
    if data["version"] != VERSION or isinstance(data["version"], bool):
        raise SteeringError(f"unsupported steering version {data['version']!r} (expected {VERSION})")
    repo_notes = _notes(data["repo_notes"], "repo_notes")
    raw_pages = data["pages"]
    if not isinstance(raw_pages, list) or not raw_pages:
        raise SteeringError("pages must be a nonempty list")
    if len(raw_pages) > MAX_PAGES:
        raise SteeringError(f"steering allows at most {MAX_PAGES} pages, got {len(raw_pages)}")
    pages = [_page(p, i, policy) for i, p in enumerate(raw_pages)]
    total = len(repo_notes) + sum(len(p.notes) for p in pages)
    if total > MAX_NOTES:
        raise SteeringError(f"steering allows at most {MAX_NOTES} notes across the file, got {total}")
    _unique(pages)
    _tree(pages)
    canonical = json.dumps(_canonical(repo_notes, pages), sort_keys=True, ensure_ascii=True)
    return Steering(tuple(repo_notes), tuple(pages), hashlib.sha256(canonical.encode()).hexdigest(), policy)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise SteeringError(f"duplicate key {key!r} in {STEERING_FILE}")
        out[key] = value
    return out


def _keys(obj: dict, allowed: set[str], required: set[str], where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        raise SteeringError(f"{where}: unknown key(s) {', '.join(unknown)}")
    missing = sorted(required - set(obj))
    if missing:
        raise SteeringError(f"{where}: missing {', '.join(missing)}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SteeringError(f"{where} must be a nonempty string")
    return value


def _notes(value: Any, where: str) -> list[Note]:
    if not isinstance(value, list):
        raise SteeringError(f"{where} must be a list of notes")
    notes = []
    for i, item in enumerate(value):
        at = f"{where}[{i}] note"
        if isinstance(item, str):
            text, author = item, None
        elif isinstance(item, dict):
            _keys(item, NOTE_KEYS, {"text"}, at)
            text, author = item["text"], item.get("author")
            if author is not None:
                _text(author, f"{at} author")
        else:
            raise SteeringError(f"{at} must be a string or an object with text")
        _text(text, f"{at} text")
        if len(text) > MAX_NOTE_CHARS:
            raise SteeringError(f"{at} is longer than {MAX_NOTE_CHARS} characters ({len(text)})")
        notes.append(Note(text, author))
    return notes


def _page(value: Any, index: int, policy: Policy) -> Page:
    where = f"pages[{index}]"
    if not isinstance(value, dict):
        raise SteeringError(f"{where} must be an object")
    _keys(value, PAGE_KEYS, REQUIRED_PAGE_KEYS, where)
    page_id = value["id"]
    if not isinstance(page_id, str) or not PAGE_ID_RE.fullmatch(page_id):
        raise SteeringError(f"{where} id must be 1-64 lowercase letters, digits, '-' or '_', got {page_id!r}")
    where = f"page {page_id!r}"
    file = value["file"]
    if not safe_page_file(file) or file.startswith(".") or not re.fullmatch(r"[^\s]+", file):
        raise SteeringError(f"{where} file must be a plain .md file name inside the wiki, got {file!r}")
    parent = value.get("parent")
    if parent is not None and not isinstance(parent, str):
        raise SteeringError(f"{where} parent must be a page id or null")
    return Page(
        id=page_id,
        file=file,
        title=_text(value["title"], f"{where} title"),
        purpose=_text(value["purpose"], f"{where} purpose"),
        parent=parent,
        notes=tuple(_notes(value.get("notes", []), f"{where} notes")),
        sources=_sources(value.get("sources", []), f"{where} sources", policy),
        coverage=_sources(value.get("coverage", []), f"{where} coverage", policy),
    )


def _sources(value: Any, where: str, policy: Policy) -> tuple[Source, ...]:
    if not isinstance(value, list):
        raise SteeringError(f"{where} must be a list of source declarations")
    out: list[Source] = []
    for i, item in enumerate(value):
        at = f"{where}[{i}] source declaration"
        if not isinstance(item, dict) or len(item) != 1 or next(iter(item)) not in SOURCE_KINDS:
            raise SteeringError(f'{at} must be {{"file": path}} or {{"directory": path}}')
        kind, path = next(iter(item.items()))
        path = _repo_path(path, at, directory=kind == "directory")
        if policy.secret(path):
            raise SteeringError(f"{at} {path!r} is excluded by the privacy policy")
        source = Source(kind, path)
        if source in out:
            raise SteeringError(f"{at} {path!r} is a duplicate")
        out.append(source)
    return tuple(out)


def _repo_path(value: Any, where: str, *, directory: bool) -> str:
    if not isinstance(value, str):
        raise SteeringError(f"{where} path must be a string")
    path = value[:-1] if directory and value.endswith("/") and len(value) > 1 else value
    bad = (
        not path
        or path.startswith(("/", "~"))
        or "\\" in path
        or re.match(r"^[A-Za-z]:", path)
        or any(c in path for c in "\0\n\r")
        or any(part in ("", ".", "..") for part in path.split("/"))
    )
    if bad or path in (".git",) or path.split("/")[0] == ".git":
        raise SteeringError(f"{where} {value!r} must be a repository-relative path without '.', '..' or '//'")
    return path


def _unique(pages: list[Page]) -> None:
    for label, key in (
        ("id", lambda p: p.id),
        ("file", lambda p: p.file.casefold()),
        ("title", lambda p: normalize_text(p.title)),
        ("purpose", lambda p: normalize_text(p.purpose)),
    ):
        seen: dict[str, str] = {}
        for p in pages:
            k = key(p)
            if k in seen:
                raise SteeringError(f"duplicate {label}: pages {seen[k]!r} and {p.id!r}")
            seen[k] = p.id


def _tree(pages: list[Page]) -> None:
    parents = {p.id: p.parent for p in pages}
    for p in pages:
        if p.parent is None:
            continue
        if p.parent == p.id:
            raise SteeringError(f"page {p.id!r} is its own parent")
        if p.parent not in parents:
            raise SteeringError(f"page {p.id!r} has unknown parent {p.parent!r}")
    for p in pages:
        seen = {p.id}
        node = p.parent
        while node is not None:
            if node in seen:
                raise SteeringError(f"parent cycle through page {p.id!r}")
            seen.add(node)
            node = parents[node]


def _canonical(repo_notes: list[Note], pages: list[Page]) -> dict[str, Any]:
    def notes(ns: Iterable[Note]) -> list[dict[str, Any]]:
        return [{"text": n.text, "author": n.author} for n in ns]

    def srcs(ss: Iterable[Source]) -> list[list[str]]:
        return [[s.kind, s.path] for s in ss]

    return {
        "version": VERSION,
        "repo_notes": notes(repo_notes),
        "pages": [
            {"id": p.id, "file": p.file, "title": p.title, "purpose": p.purpose, "parent": p.parent,
             "notes": notes(p.notes), "sources": srcs(p.sources), "coverage": srcs(p.coverage)}
            for p in pages
        ],
    }  # fmt: skip
