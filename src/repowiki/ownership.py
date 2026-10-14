"""Exact-byte page ownership, authoritative only in this worktree's Git administration."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import safety
from .policy import DEFAULT_POLICY, Policy

ROOT = "repowiki-ownership"
TARGET = "@ownership"
MAX_OWNERSHIP_BYTES = 1_000_000


class OwnershipError(safety.UnsafePathError):
    """A collision or edited page cannot be replaced without individual review."""


def record_path(wiki: Path) -> Path:
    admin, identity = safety._identity(wiki)
    return admin / ROOT / f"{identity}.json"


def _directory(wiki: Path, *, create: bool = False) -> Path:
    path = record_path(wiki).parent
    safety._real_dir(path.parent)
    if create or os.path.lexists(path):
        safety._real_dir(path, create=create)
    return path


def _unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def load(wiki: Path) -> dict[str, str]:
    """Read only the bounded, regular Git-admin receipt. Checkout receipts are inert."""
    _directory(wiki)
    path = record_path(wiki)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise safety.RecoveryError("ownership record is not a regular file") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise safety.RecoveryError("ownership record is not a regular file")
        raw = fh.read(MAX_OWNERSHIP_BYTES + 1)
    try:
        if len(raw) > MAX_OWNERSHIP_BYTES:
            raise ValueError("too large")
        data = json.loads(raw, object_pairs_hook=_unique)
        if (
            not isinstance(data, dict)
            or set(data) != {"version", "identity", "pages"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or data["identity"] != safety._identity(wiki)[1]
            or not isinstance(data["pages"], dict)
        ):
            raise ValueError("unknown schema or identity")
        pages = data["pages"]
        folded: set[str] = set()
        for name, digest in pages.items():
            if (
                not safety.safe_page_file(name)
                or name.casefold() in folded
                or not isinstance(digest, str)
                or not safety._HEX64.fullmatch(digest)
            ):
                raise ValueError("unsafe name or hash")
            folded.add(name.casefold())
        return pages
    except (ValueError, TypeError) as exc:
        raise safety.RecoveryError("untrusted ownership record; nothing was changed") from exc


def check_keys(pages: dict[str, str]) -> None:
    """Refuse a receipt that load() would reject: two names that differ only by case are one page
    on a case-insensitive checkout, so an alias of an owned page never gets its own entry."""
    seen: dict[str, str] = {}
    for name in pages:
        other = seen.setdefault(name.casefold(), name)
        if other != name:
            raise OwnershipError(
                f"{name}: differs only by case from the owned page {other}; use the existing spelling"
            )


def encode(wiki: Path, pages: dict[str, str]) -> bytes:
    return json.dumps({"version": 1, "identity": safety._identity(wiki)[1], "pages": pages}, sort_keys=True).encode()


def is_owned(wiki: Path, name: str) -> bool:
    """Caller holds the wiki lock; a marker, index entry or checkout hash grants no authority."""
    digest = load(wiki).get(name)
    return digest is not None and safety._sha(safety.wiki_file(wiki, name)) == digest


def checked_update(wiki: Path, writes: dict[str, str], deletes: list[str]) -> dict[str, str]:
    """Check every replacement/deletion before staging, then calculate the new receipt."""
    pages = load(wiki)
    for name in [*writes, *deletes]:
        if name == safety.INDEX:
            continue  # index.json is transaction metadata, never a page adoption target
        target = safety.wiki_file(wiki, name)
        if target.exists() and (name not in pages or safety._sha(target) != pages[name]):
            raise OwnershipError(f"{name}: unowned or manually edited page; review and adopt its exact bytes first")
        if name in writes:
            pages[name] = hashlib.sha256(writes[name].encode("utf-8")).hexdigest()
        else:
            pages.pop(name, None)
    check_keys(pages)
    return pages


@dataclass(frozen=True)
class Candidate:
    page: str
    sha256: str
    base: str


def _page(wiki: Path, name: str, policy: Policy) -> Path:
    if not safety.safe_page_file(name) or policy.secret(name):
        raise OwnershipError("adoption requires one plain, non-secret .md page name")
    path = safety.wiki_file(wiki, name)
    if not path.exists():
        raise OwnershipError(f"{name}: adoption requires an existing regular file")
    return path


def review(wiki: Path, name: str, *, policy: Policy = DEFAULT_POLICY) -> Candidate:
    """Offline candidate for operator review; never reads other wiki pages or calls a runner."""
    with safety.wiki_lock(wiki):
        safety.recover(wiki)
        digest = safety._sha(_page(wiki, name, policy))
        assert digest is not None
        return Candidate(name, digest, safety.base_digest(wiki))


def adopt(wiki: Path, name: str, *, reviewed_sha256: str, expected_base: str, policy: Policy = DEFAULT_POLICY) -> bool:
    """Grant authority to one explicitly reviewed page, with exact bytes and stale-base protection.

    Changes only Git administration, using the publication transaction. Returns False for a
    fresh review of already owned bytes. A copied clone/worktree must review and adopt again.
    """
    if not (
        isinstance(reviewed_sha256, str)
        and safety._HEX64.fullmatch(reviewed_sha256)
        and isinstance(expected_base, str)
        and safety._HEX64.fullmatch(expected_base)
    ):
        raise OwnershipError("adoption requires the reviewed sha256 and candidate base digests")
    with safety.wiki_lock(wiki):
        safety.recover(wiki)
        safety.check_base(wiki, expected_base)
        path = _page(wiki, name, policy)
        if safety._sha(path) != reviewed_sha256:
            raise OwnershipError("candidate bytes differ from the explicit review; nothing was adopted")
        pages = load(wiki)
        if pages.get(name) == reviewed_sha256:
            return False
        pages[name] = reviewed_sha256
        check_keys(pages)  # before any write: the stored receipt must stay loadable
        safety._transact(wiki, {}, [], adopted=pages, reviewed={name: reviewed_sha256})
        return True
