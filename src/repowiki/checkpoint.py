"""Checkpoints: every finished plan and page is saved as soon as it exists.

Entries are content-addressed: the key is a hash of the model labels and the
exact prompt, so a rerun at the same commit with the same settings reuses
finished work, while any change to the code, the outline or the models
produces new keys. They live in ``.git/repowiki/checkpoints`` (never committed,
never part of the wiki) and only hold validated output.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import __version__
from .git import GitError, git_dir
from .safety import atomic_write


def key(*parts: str) -> str:
    h = hashlib.sha256()
    for part in (__version__, *parts):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:32]


class Checkpoints:
    def __init__(self, root: Path, *, enabled: bool = True, reuse: bool = True) -> None:
        self.enabled = enabled
        self.reuse = reuse
        self.dir: Path | None = None
        if enabled:
            try:
                self.dir = git_dir(root) / "repowiki" / "checkpoints"
            except GitError:
                self.enabled = False

    def _path(self, kind: str, k: str) -> Path | None:
        return self.dir / f"{kind}-{k}.txt" if self.enabled and self.dir else None

    def get(self, kind: str, k: str) -> str | None:
        path = self._path(kind, k)
        if not self.reuse or path is None or not path.is_file() or path.is_symlink():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    def put(self, kind: str, k: str, text: str) -> None:
        path = self._path(kind, k)
        if path is not None:
            atomic_write(path, text)
