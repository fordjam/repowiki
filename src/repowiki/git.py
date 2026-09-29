"""Thin, read-only wrappers around the git CLI.

Everything repowiki knows about a repository's source comes from a specific
commit (never the working tree), so citations can be pinned to that commit.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
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


def renamed_files(repo: Path | str, old: str, new: str) -> dict[str, str]:
    """{old path: new path} for files git detects as renamed (or moved) between two commits."""
    out = git(repo, "diff", "--name-status", "-z", "-M", old, new)
    parts = [p for p in out.split("\0") if p]
    renames: dict[str, str] = {}
    i = 0
    while i < len(parts):
        status = parts[i]
        if status[:1] in ("R", "C") and i + 2 < len(parts):
            if status[0] == "R":
                renames[parts[i + 1]] = parts[i + 2]
            i += 3
        else:
            i += 2
    return renames


def git_dir(repo: Path | str) -> Path:
    """The repository's (common) .git directory, where repowiki keeps checkpoints."""
    out = git(repo, "rev-parse", "--git-common-dir").strip()
    path = Path(out)
    return path if path.is_absolute() else (Path(repo) / path).resolve()


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


def count_lines(text: str) -> int:
    """Lines as git hosts number them: split on \\n, a final newline does not start a new line."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln.removesuffix("\r") for ln in lines]


BINARY_SNIFF = 8192
CACHE_BYTES = 64 * 1024 * 1024  # file text kept in memory per snapshot


@dataclass
class Snapshot:
    """A repository frozen at one commit.

    Blob sizes come from ``git ls-tree`` so budgets are checked before anything
    is read. Reads can be capped at a byte limit; line counts are streamed.
    Full reads are cached up to ``CACHE_BYTES`` in total (least recently used first).
    """

    root: Path
    commit: str
    sizes: dict[str, int] = field(default_factory=dict)
    _cache: OrderedDict[str, str] = field(default_factory=OrderedDict, repr=False)
    _cached_bytes: int = field(default=0, repr=False)
    _lines: dict[str, int | None] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

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

    def _open(self, path: str) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            ["git", "-C", str(self.root), "cat-file", "blob", f"{self.commit}:{path}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _bytes(self, path: str, limit: int | None) -> bytes | None:
        try:
            proc = self._open(path)
        except OSError:
            return None
        assert proc.stdout is not None
        with proc:
            data = proc.stdout.read() if limit is None else proc.stdout.read(limit)
            if limit is not None:
                proc.kill()
        if limit is None and proc.returncode != 0:
            return None
        return None if b"\0" in data[:BINARY_SNIFF] else data

    def read(self, path: str, limit: int | None = None) -> str | None:
        """Text of `path` at the snapshot commit, or None if missing or binary.

        With `limit`, at most `limit` bytes are read; a longer file is returned
        cut back to its last complete line.
        """
        if path not in self.sizes:
            return None
        if limit is not None and self.sizes[path] > limit:
            data = self._bytes(path, limit)
            if data is None:
                return None
            return data[: data.rfind(b"\n") + 1].decode("utf-8", "replace")
        with self._lock:
            if path in self._cache:
                self._cache.move_to_end(path)
                return self._cache[path]
        data = self._bytes(path, None)
        if data is None:
            return None
        text = data.decode("utf-8", "replace")
        with self._lock:
            self._lines[path] = count_lines(text)
            if len(data) <= CACHE_BYTES // 4:
                self._cache[path] = text
                self._cached_bytes += len(data)
                while self._cached_bytes > CACHE_BYTES and self._cache:
                    _, old = self._cache.popitem(last=False)
                    self._cached_bytes -= len(old.encode("utf-8", "replace"))
        return text

    def line_count(self, path: str) -> int | None:
        """Number of lines (None if missing or binary), streamed without holding the file in memory."""
        if path not in self.sizes:
            return None
        with self._lock:
            if path in self._lines:
                return self._lines[path]
        count: int | None = None
        try:
            proc = self._open(path)
        except OSError:
            return None
        assert proc.stdout is not None
        with proc:
            newlines, last, first = 0, b"", True
            while chunk := proc.stdout.read(1 << 20):
                if first and b"\0" in chunk[:BINARY_SNIFF]:
                    proc.kill()
                    break
                first = False
                newlines += chunk.count(b"\n")
                last = chunk[-1:]
            else:
                count = newlines + (1 if last and last != b"\n" else 0)
        if proc.returncode != 0:
            count = None
        with self._lock:
            self._lines[path] = count
        return count
