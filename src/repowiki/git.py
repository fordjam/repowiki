"""Thin, read-only wrappers around the git CLI.

Everything repowiki knows about a repository's source comes from a specific
commit (never the working tree), so citations can be pinned to that commit.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class GitError(RuntimeError):
    """A git command failed."""


def git(repo: Path | str, *args: str, check: bool = True, timeout: float = 120) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from exc
    if proc.returncode != 0:
        if not check:
            return ""
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)}: {err or f'exit {proc.returncode}'}")
    return proc.stdout.decode("utf-8", "replace")


def toplevel(path: Path | str) -> Path:
    return Path(git(path, "rev-parse", "--show-toplevel").strip())


def resolve_commit(repo: Path | str, ref: str = "HEAD") -> str:
    if ref.startswith("-"):
        raise GitError(f"refusing suspicious ref {ref!r}")
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").strip()


def has_commit(repo: Path | str, sha: str) -> bool:
    if not sha or sha.startswith("-"):
        return False
    try:
        subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            check=True,
            timeout=30,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def default_branch(repo: Path | str, preferred: str = "main") -> str | None:
    """`preferred` if it exists, else the checked-out branch, else None."""
    for ref in (preferred, "HEAD"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip():
            return ref
    return None


def commits_since(repo: Path | str, sha: str, branch: str, exclude: str | None = None) -> int | None:
    """Commits on `branch` after `sha`; commits touching only `exclude` are not counted."""
    if not has_commit(repo, sha) or branch.startswith("-"):
        return None
    args = ["rev-list", "--count", f"{sha}..{branch}"]
    if exclude:
        args += ["--", ".", f":(exclude){exclude}"]
    out = git(repo, *args, check=False).strip()
    return int(out) if out.isdigit() else None


def changed_files(repo: Path | str, old: str, new: str) -> set[str]:
    """Paths added, modified, deleted or renamed (both sides) between two commits."""
    out = git(repo, "diff", "--name-status", "-z", "--no-renames", old, new)
    parts = [p for p in out.split("\0") if p]
    # -z output alternates status, path
    return {parts[i + 1] for i in range(0, len(parts) - 1, 2)}


def remote_web_url(repo: Path | str) -> str | None:
    """Best-effort https URL for the `origin` remote (GitHub/GitLab style)."""
    url = git(repo, "remote", "get-url", "origin", check=False).strip()
    if not url:
        return None
    m = re.match(r"^(?:ssh://)?git@([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


def repo_name(repo: Path | str) -> str:
    """`owner/name` from the origin remote when available, else the directory name."""
    web = remote_web_url(repo)
    if web:
        parts = web.rstrip("/").split("/")
        if len(parts) >= 5:
            return "/".join(parts[-2:])
    return toplevel(repo).name


@dataclass
class Snapshot:
    """A repository frozen at one commit. File reads are cached."""

    root: Path
    commit: str
    sizes: dict[str, int] = field(default_factory=dict)

    @classmethod
    def at(cls, repo: Path | str, ref: str = "HEAD") -> Snapshot:
        root = toplevel(repo)
        sha = resolve_commit(root, ref)
        if not sha:
            raise GitError(f"{root}: cannot resolve {ref!r} (does the repository have commits?)")
        snap = cls(root, sha)
        out = git(root, "ls-tree", "-r", "-l", "-z", sha)
        for entry in out.split("\0"):
            if not entry:
                continue
            meta, _, path = entry.partition("\t")
            fields = meta.split()
            if len(fields) == 4 and fields[1] == "blob":
                snap.sizes[path] = int(fields[3]) if fields[3].isdigit() else 0
        return snap

    @property
    def files(self) -> list[str]:
        return sorted(self.sizes)

    def has_file(self, path: str) -> bool:
        return path in self.sizes

    def has_dir(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return prefix == "/" or any(p.startswith(prefix) for p in self.sizes)

    def read(self, path: str) -> str | None:
        """Text of `path` at the snapshot commit, or None if missing or binary."""
        return _read_blob(str(self.root), self.commit, path) if path in self.sizes else None

    def line_count(self, path: str) -> int | None:
        text = self.read(path)
        return None if text is None else len(text.splitlines())


@lru_cache(maxsize=4096)
def _read_blob(root: str, commit: str, path: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", root, "show", f"{commit}:{path}"],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or b"\0" in proc.stdout[:8192]:
        return None
    return proc.stdout.decode("utf-8", "replace")
