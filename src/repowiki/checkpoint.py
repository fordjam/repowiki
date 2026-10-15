"""Checkpoints: every finished plan and page is saved as soon as it exists.

Entries are content-addressed: the key is a hash of the model labels and the
exact prompt, so a rerun at the same commit with the same settings reuses
finished work, while any change to the code, the outline or the models
produces new keys. They live in ``.git/repowiki/checkpoints`` (never committed,
never part of the wiki) and only hold validated output.

A finished page is saved with a record of whether its required final pass
succeeded, bound to the page's exact bytes. A page checkpoint with no such record
(an older one) is never reused.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import __version__
from .git import GitError, git_dir
from .safety import atomic_write

PAGE_RECORD = "repowiki-page-checkpoint/1"
FINAL_PASSED = "passed"  # the required final pass ran and its output was validated
FINAL_NOT_REQUIRED = "not-required"  # a one-pass run: there was no final pass to run


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

    def put_page(self, k: str, text: str, *, final_pass: str) -> None:
        """Save a finished page, then the record of how its final pass went (the page's own bytes stay bare)."""
        self.put("page", k, text)
        record = {"record": PAGE_RECORD, "final_pass": final_pass, "sha256": _digest(text)}
        self.put("final", k, json.dumps(record))

    def get_page(self, k: str) -> tuple[str, str] | None:
        """(text, final-pass status) of a saved page, or None when it is missing, unreadable, changed since
        its record was written, or has no record at all (a legacy entry carries no final-pass provenance)."""
        text, raw = self.get("page", k), self.get("final", k)
        if text is None or raw is None:
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(record, dict) or record.get("record") != PAGE_RECORD:
            return None
        status = record.get("final_pass")
        if status not in (FINAL_PASSED, FINAL_NOT_REQUIRED) or record.get("sha256") != _digest(text):
            return None
        return text, status


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
