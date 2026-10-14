"""The frozen validation reader: one coherent wiki, read once, validated as read.

Every consumer that trusts a wiki (``repowiki validate``, the site) reads it through ``read_frozen``:

- the commits are resolved once: the wiki commit (``ref``, resolved to a commit id before anything is
  read), the source commit (the index's ``indexed_commit``) and the policy commit (an explicitly selected
  one, else the wiki commit for a committed read); moving a branch afterwards cannot change what was
  opened, and an uncommitted edit to the configuration cannot loosen a committed read's exclusions;
- the index and every page text are captured once, either from that wiki commit or, in working-tree
  mode, from disk under the wiki lock (after recovering an interrupted publish) so a publish cannot
  interleave; committed reads never recover or touch the working tree;
- those exact captured objects are validated in full (``validate.check_wiki``) and are what callers
  render. Nothing is read again afterwards.
"""

from __future__ import annotations

import json
import posixpath
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import CONFIG_FILE, Settings, load_settings
from .git import SHA_RE, GitError, Snapshot, git, has_commit, resolve_commit
from .links import RepoIdentity
from .policy import DEFAULT_POLICY, Policy
from .safety import INDEX, UnsafePathError, check_wiki_dir, read_wiki_file, recover, wiki_lock, wiki_path
from .validate import PageEntry, Problem, check_index, check_wiki


@dataclass
class FrozenWiki:
    """A wiki captured once: what was read, from where, and every problem full validation found."""

    root: Path
    wiki_dir: str
    wiki_commit: str | None  # the commit the wiki was read from; None: the working tree, read under the lock
    index: dict[str, Any] | None = None
    entries: list[PageEntry] = field(default_factory=list)
    texts: dict[str, str] = field(default_factory=dict)
    snap: Snapshot | None = None  # the source snapshot at the index's indexed_commit
    problems: list[Problem] = field(default_factory=list)
    policy: Policy = DEFAULT_POLICY  # the secret policy the wiki was validated with (render and export with it)
    policy_commit: str | None = None  # where that policy came from; None: the working tree's configuration

    @property
    def source_commit(self) -> str:
        return str((self.index or {}).get("indexed_commit") or "")

    @property
    def errors(self) -> list[Problem]:
        return [p for p in self.problems if p.level == "error"]

    @property
    def ok(self) -> bool:
        return self.index is not None and not self.errors


def _resolve(root: Path, ref: str) -> str | None:
    if ref.startswith("-"):
        return None
    try:
        return resolve_commit(root, ref) or None
    except GitError:
        return None


def committed_settings(root: Path, commit: str) -> Settings:
    """The repository configuration exactly as committed at ``commit`` (defaults when it has none)."""
    text = Snapshot.at(root, commit).read(CONFIG_FILE)
    with tempfile.TemporaryDirectory(prefix="repowiki-policy-") as tmp:
        if text is not None:
            (Path(tmp) / CONFIG_FILE).write_text(text, encoding="utf-8")
        return load_settings(Path(tmp))


def read_frozen(
    root: Path,
    wiki_dir: str = "docs/wiki",
    *,
    ref: str | None = None,
    policy: Policy = DEFAULT_POLICY,
    min_citations: int = 1,
    identities: tuple[RepoIdentity, ...] = (),
    policy_commit: str | None = None,
    reader: Callable[[str], str | None] | None = None,
) -> FrozenWiki:
    """Read and fully validate the wiki at ``ref`` (a commit, resolved once) or, with no ``ref``, the
    working tree under the wiki lock. Problems are returned, never raised; ``ok`` says whether the
    captured wiki may be trusted.

    The secret policy and citation minimum come from ``policy_commit`` when one is selected, else from
    the wiki commit for a committed read, else (working tree) from ``policy`` and ``min_citations``.
    ``reader`` replaces the working-tree file reader (it is called under the wiki lock)."""
    try:
        wiki_dir = check_wiki_dir(wiki_dir)
        wiki = wiki_path(root, wiki_dir)
    except UnsafePathError as exc:
        return FrozenWiki(root, wiki_dir, None, problems=[Problem(wiki_dir, 0, str(exc))])
    commit = None
    if ref is not None:
        commit = _resolve(root, ref)
        if not commit:
            return FrozenWiki(root, wiki_dir, None, problems=[Problem(wiki_dir, 0, f"cannot resolve ref {ref!r}")])
    selected = policy_commit or commit
    if selected is not None:
        resolved = _resolve(root, selected)
        if not resolved:
            msg = f"cannot resolve policy commit {selected!r}"
            return FrozenWiki(root, wiki_dir, commit, problems=[Problem(CONFIG_FILE, 0, msg)])
        try:
            settings = committed_settings(root, resolved)
        except (ValueError, OSError) as exc:
            msg = f"invalid configuration at {resolved[:12]}: {exc}"
            return FrozenWiki(root, wiki_dir, commit, problems=[Problem(CONFIG_FILE, 0, msg)])
        policy, min_citations, selected = settings.policy, settings.min_citations, resolved
    if commit is None:
        with wiki_lock(wiki):  # one coherent set: no publish can interleave with these reads
            frozen = _read(root, wiki_dir, wiki, None, policy, min_citations, identities, reader)
    else:
        frozen = _read(root, wiki_dir, wiki, commit, policy, min_citations, identities, None)
    frozen.policy, frozen.policy_commit = policy, selected
    return frozen


def _committed_files(root: Path, commit: str, wiki_dir: str) -> tuple[dict[str, str], set[str]]:
    """(regular files, other entries such as symlinks) under ``wiki_dir`` at ``commit``, relative to it."""
    out = git(root, "ls-tree", "-r", "-z", commit, "--", wiki_dir + "/")
    regular: dict[str, str] = {}
    other: set[str] = set()
    prefix = wiki_dir.rstrip("/") + "/"
    for entry in out.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        mode, kind, _sha = (meta.split() + ["", "", ""])[:3]
        rel = path[len(prefix) :] if path.startswith(prefix) else path
        if kind == "blob" and mode in ("100644", "100755"):
            regular[rel] = path
        else:
            other.add(rel)
    return regular, other


def _read(
    root: Path,
    wiki_dir: str,
    wiki: Path,
    commit: str | None,
    policy: Policy,
    min_citations: int,
    identities: tuple[RepoIdentity, ...],
    reader: Callable[[str], str | None] | None,
) -> FrozenWiki:
    frozen = FrozenWiki(root, wiki_dir, commit)
    probs = frozen.problems
    files: set[str] | None = None
    if commit is None:
        try:
            if recover(wiki):
                note = "rolled back a publish that an earlier run did not finish"
                probs.append(Problem(INDEX, 0, note, level="warning"))
        except UnsafePathError as exc:  # an untrusted transaction record: refuse to read this wiki at all
            probs.append(Problem(wiki_dir, 0, str(exc)))
            return frozen

        def read(name: str) -> str | None:
            return reader(name) if reader is not None else read_wiki_file(wiki, name)

        where = "(run `repowiki generate` first)"
    else:
        wiki_snap = Snapshot.at(root, commit)
        regular, other = _committed_files(root, commit, wiki_dir)
        files = set(regular) | other

        def read(name: str) -> str | None:
            if name in other:
                raise UnsafePathError(f"{wiki_dir}/{name} is not a regular file at {commit[:12]}")
            return wiki_snap.read(regular[name]) if name in regular else None

        where = f"at {commit[:12]}"
    try:
        raw = read(INDEX)
    except UnsafePathError as exc:
        probs.append(Problem(INDEX, 0, str(exc)))
        return frozen
    if raw is None:
        probs.append(Problem(f"{wiki_dir}/{INDEX}", 0, f"not found {where}"))
        return frozen
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        probs.append(Problem(INDEX, 0, f"invalid JSON: {exc}"))
        return frozen
    sha = data.get("indexed_commit") if isinstance(data, dict) else None
    if isinstance(sha, str) and SHA_RE.match(sha) and has_commit(root, sha):
        try:
            frozen.snap = Snapshot.at(root, sha)
        except GitError as exc:
            probs.append(Problem(INDEX, 0, str(exc)))
    frozen.entries = check_index(data, wiki_dir=wiki_dir, policy=policy)[1]
    for e in frozen.entries:
        try:
            text = read(e.file)
        except UnsafePathError as exc:
            probs.append(Problem(e.file, 0, str(exc)))
            continue
        if text is not None:
            frozen.texts[e.file] = text
    unsafe = {p.file for p in probs}
    probs += [
        p
        for p in check_wiki(
            data, frozen.texts, root=root, wiki_dir=wiki_dir, wiki=wiki, snap=frozen.snap, policy=policy,
            min_citations=min_citations, repo_identities=identities, wiki_files=files,
        )  # fmt: skip
        if not (p.file in unsafe and p.message.endswith("but missing"))
    ]
    raw_pages = data.get("pages") if isinstance(data, dict) else None
    listed = {p.get("file") for p in raw_pages if isinstance(p, dict)} if isinstance(raw_pages, list) else set()
    if files is not None:
        present = sorted(f for f in files if "/" not in f)
    else:
        present = sorted(p.name for p in wiki.glob("*.md"))
    for extra in present:
        if extra.endswith(".md") and extra not in listed and posixpath.splitext(extra)[0].lower() != "readme":
            probs.append(Problem(extra, 0, "Markdown file not listed in index.json", level="warning"))
    frozen.index = data if isinstance(data, dict) else None
    return frozen


__all__ = ["FrozenWiki", "read_frozen"]
