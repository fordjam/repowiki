"""Durable publication for a staged site: immutable releases under a confined, owned sibling store.

The output path is the managed pointer. `out` is a symlink into `.<out.name>.repowiki/` (the store,
a sibling owned by repowiki), which holds `releases/<id>/` — complete, immutable trees — and the
`current` symlink naming the release to serve. Publication never moves a live directory: a release
is fully written and fsynced under its final name first, then one ``os.replace`` of a temporary
symlink swings the pointer, with the directory fsynced after. A kill before or after that replace
leaves either the old or the new complete release being served, never a partial one.

A legacy real-directory output (an older site) is converted once, offline, by a locked, journaled
migration (:func:`migrate_legacy`); recovery (:func:`recover`) completes an interrupted migration or
publication and is safe to repeat.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

STORE_PREFIX = ".repowiki-"  # the store is the owned sibling: .repowiki-<name> beside the output
STORE_MANIFEST = ".repowiki-store.json"
RELEASE_MANIFEST = ".repowiki-release.json"
RELEASES = "releases"
CURRENT = "current"
JOURNAL = "journal.json"
STAGING_PREFIX = ".staging-"
LOCK = "publish.lock"
RETENTION = timedelta(days=30)


class PublicationError(RuntimeError):
    """Why a site publication or recovery refused to proceed (the last good site stays served)."""


def store_path(out: Path) -> Path:
    """The confined, owned sibling store of an output path."""
    return out.parent / f"{STORE_PREFIX}{out.name}"


def fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            fd = os.open(p, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for d in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        fsync_dir(d)
    fsync_dir(root)


def tree_digest(root: Path) -> str:
    """The content identity of a staged tree (the release id): every path and file digest, and never
    the release manifest itself, so a re-render of the same site publishes the same release."""
    h = hashlib.sha256()
    for p in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        if rel == RELEASE_MANIFEST:
            continue
        if p.is_symlink():
            raise PublicationError(f"refusing to publish a symlinked path: {rel}")
        if p.is_dir():
            h.update(f"d\0{rel}\0".encode())
        else:
            h.update(f"f\0{rel}\0".encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def ensure_store(store: Path) -> None:
    """Create the store, or verify an existing one is really repowiki's: fail closed on anything else."""
    if store.is_symlink():
        raise PublicationError(f"refusing to use a symlinked store: {store}")
    if not store.exists():
        store.mkdir(parents=True)
        (store / RELEASES).mkdir()
        (store / STORE_MANIFEST).write_text(
            json.dumps({"version": 1}, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        fsync_dir(store / RELEASES)
        fsync_dir(store)
        return
    if not store.is_dir():
        raise PublicationError(f"the site store at {store} is not a directory")
    if not (store / RELEASES).is_dir():
        raise PublicationError(f"the site store at {store} has no releases directory")
    manifest = store / STORE_MANIFEST
    if manifest.is_symlink() or not manifest.is_file():
        raise PublicationError(f"the site store at {store} has no store manifest; refusing to use it")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"the store manifest at {store} is corrupt: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PublicationError(f"the store manifest at {store} has an unknown version")


def read_release_manifest(release: Path) -> dict:
    """The manifest of a release, refused when it is a symlink or corrupt (the release is then never
    adopted, pointed at, or pruned)."""
    manifest = release / RELEASE_MANIFEST
    if manifest.is_symlink():
        raise PublicationError(f"the release manifest must not be a symlink: {manifest}")
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError(f"the release manifest at {manifest} is corrupt: {exc}") from exc
    expected = release.resolve().name if release.is_symlink() else release.name
    if not isinstance(data, dict) or data.get("id") != expected:
        raise PublicationError(f"the release manifest at {manifest} does not describe this release")
    return data


def _write_release_manifest(staged: Path, digest: str, now: datetime, origin: str) -> None:
    manifest = staged / RELEASE_MANIFEST
    if manifest.exists():
        raise PublicationError(f"the staged site already carries a release manifest: {manifest}")
    manifest.write_text(
        json.dumps({"id": digest, "created": now.isoformat(), "version": 1, "origin": origin},
                   indent=2, sort_keys=True) + "\n",  # fmt: skip
        encoding="utf-8",
    )


@contextmanager
def publication_lock(store: Path):
    """One publisher at a time per site; a second one is refused, never queued."""
    fd = os.open(store / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise PublicationError("another publication is already in progress for this site") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _write_journal(store: Path, intent: dict | None) -> None:
    journal = store / JOURNAL
    if intent is None:
        if journal.exists() or journal.is_symlink():
            journal.unlink()
            fsync_dir(store)
        return
    tmp = store / f".{JOURNAL}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(intent, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, journal)
    fsync_dir(store)


def _point_current(store: Path, digest: str) -> None:
    """The one atomic step: a fresh temporary symlink is swapped over `current`, then the store is
    fsynced. Before it, the old release is served; after it, the new one — both complete."""
    tmp = store / f".{CURRENT}.{os.getpid()}.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(f"{RELEASES}/{digest}", tmp)
    os.replace(tmp, store / CURRENT)
    fsync_dir(store)


def _ensure_out_link(out: Path, store: Path) -> None:
    if out.is_symlink():
        return
    if out.exists():
        raise PublicationError(f"{out} exists and is not a managed pointer to {store}")
    os.symlink(f"{STORE_PREFIX}{out.name}/{CURRENT}", out)
    fsync_dir(out.parent)


def recover(store: Path, out: Path, *, marker: str) -> None:
    """Complete an interrupted migration or publication; safe to run any number of times."""
    intent = None
    journal = store / JOURNAL
    if journal.exists():
        try:
            data = json.loads(journal.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PublicationError(f"the publication journal at {journal} is corrupt: {exc}") from exc
        if isinstance(data, dict) and data.get("op") == "migrate":
            intent = data
        else:
            _write_journal(store, None)  # an unknown or finished entry is not worth failing over
    for stale in sorted((store / RELEASES).glob(f"{STAGING_PREFIX}*")):
        shutil.rmtree(stale, ignore_errors=True)  # a killed staging was never referenced
    if intent:
        _finish_migration(store, out, intent, marker=marker)
    current = store / CURRENT
    if current.is_symlink():
        read_release_manifest(current)  # a corrupt or symlinked manifest refuses the site
    if not out.exists() and current.is_symlink():
        _ensure_out_link(out, store)


def _finish_migration(store: Path, out: Path, intent: dict, *, marker: str) -> None:
    digest = str(intent.get("id", ""))
    created = intent.get("created")
    if not digest or "/" in digest or digest.startswith(".") or not isinstance(created, str):
        _write_journal(store, None)
        raise PublicationError(f"the migration journal names no usable release: {intent!r}")
    final = store / RELEASES / digest
    if final.is_dir():
        read_release_manifest(final)
    elif out.exists() and out.is_dir() and not out.is_symlink() and (out / marker).is_file():
        _write_release_manifest(out, digest, datetime.fromisoformat(created), "legacy")
        _fsync_tree(out)
        os.rename(out, final)  # killed before the move: the legacy directory is still the live site
        fsync_dir(store / RELEASES)
    elif not final.is_dir():
        _write_journal(store, None)
        raise PublicationError(f"the migration journal names a release that does not exist: {digest[:12]}")
    _point_current(store, digest)
    _ensure_out_link(out, store)
    _write_journal(store, None)


def migrate_legacy(out: Path, *, now: datetime, marker: str) -> str:
    """Convert a legacy real-directory site into a managed release — explicitly, once, offline;
    continuous availability is not promised for the duration of this one conversion."""
    if out.is_symlink():
        return (store_path(out) / CURRENT).resolve().name
    if not out.exists() or not out.is_dir():
        raise PublicationError(f"nothing to migrate at {out}")
    if not (out / marker).is_file():
        raise PublicationError(f"refusing to migrate {out}: it is not a repowiki site (no {marker})")
    store = store_path(out)
    ensure_store(store)
    with publication_lock(store):
        recover(store, out, marker=marker)  # finish anything an earlier run left behind
        if out.is_symlink():
            return (store / CURRENT).resolve().name
        digest = tree_digest(out)
        _write_journal(store, {"op": "migrate", "id": digest, "created": now.isoformat()})
        final = store / RELEASES / digest
        if final.is_dir():
            read_release_manifest(final)
        else:
            _write_release_manifest(out, digest, now, "legacy")
            _fsync_tree(out)
            os.rename(out, final)
            fsync_dir(store / RELEASES)
        _point_current(store, digest)
        _ensure_out_link(out, store)
        _write_journal(store, None)
        prune(store, now=now)
        return digest


def prune(store: Path, *, now: datetime) -> None:
    """Retain the current release and every release younger than the retention window; older ones
    are removed under the publication lock. An unreadable release is never pruned by date."""
    current = store / CURRENT
    current_id = current.resolve().name if current.is_symlink() else None
    cutoff = now - RETENTION
    for release in sorted((store / RELEASES).iterdir()):
        if (
            release.name == current_id
            or release.name.startswith(STAGING_PREFIX)
            or not release.is_dir()
            or release.is_symlink()
        ):
            continue
        try:
            created = datetime.fromisoformat(read_release_manifest(release)["created"])
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
        except PublicationError:
            continue  # it refuses recovery anyway; pruning by date must not decide for it
        if created < cutoff:
            shutil.rmtree(release, ignore_errors=True)


def publish(staged: Path, out: Path, *, now: datetime, marker: str) -> str:
    """Move a completely rendered staging directory into the store as an immutable release and swing
    `out` to it. The last good release is served until the pointer replacement, and after any
    failure before it. Returns the release id."""
    if staged.is_symlink() or not staged.is_dir():
        raise PublicationError(f"nothing staged to publish at {staged}")
    store = store_path(out)
    ensure_store(store)
    with publication_lock(store):
        recover(store, out, marker=marker)
        digest = tree_digest(staged)
        _write_release_manifest(staged, digest, now, "build")
        final = store / RELEASES / digest
        if final.is_dir():
            read_release_manifest(final)  # the same site is already published: reuse that release
            shutil.rmtree(staged, ignore_errors=True)
        else:
            _fsync_tree(staged)
            os.rename(staged, final)
            fsync_dir(store / RELEASES)
        _point_current(store, digest)
        _ensure_out_link(out, store)
        prune(store, now=now)
        return digest
