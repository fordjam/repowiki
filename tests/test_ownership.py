"""R10: synthetic exact-byte ownership and crash recovery, with no model or network."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import run_git
from repowiki import ownership, safety


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def accept(wiki: Path, name: str) -> bool:
    candidate = ownership.review(wiki, name)
    return ownership.adopt(wiki, name, reviewed_sha256=candidate.sha256, expected_base=candidate.base)


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("delete", [False, True])
def test_unowned_collision_preserves_every_byte(repo: Path, marked: bool, delete: bool) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    original = b"# Handwritten\r\n\r\nPrecious synthetic text.\r\n"
    if marked:
        original += safety.GENERATED_MARK.encode() + b"\r\n"
    page = wiki / "notes.md"
    page.write_bytes(original)
    # Checkout markers and hash receipts cannot grant authority, even when the hash is correct.
    (wiki / "ownership.json").write_text(json.dumps({"notes.md": sha(original)}))
    (repo / ".repowiki-ownership.json").write_bytes(ownership.encode(wiki, {"notes.md": sha(original)}))
    with pytest.raises(ownership.OwnershipError, match="unowned or manually edited"):
        safety.transact(wiki, {} if delete else {page.name: "replacement"}, [page.name] if delete else [])
    assert page.read_bytes() == original
    assert not ownership.record_path(wiki).exists()


@pytest.mark.parametrize("delete", [False, True])
def test_owned_page_one_byte_manual_edit_blocks_mutation(repo: Path, delete: bool) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": f"# Page\n\n{safety.GENERATED_MARK}\n"}, [])
    page = wiki / "page.md"
    receipt = ownership.record_path(wiki).read_bytes()
    original = page.read_bytes() + b" "
    page.write_bytes(original)
    with pytest.raises(ownership.OwnershipError):
        safety.transact(wiki, {} if delete else {page.name: "new"}, [page.name] if delete else [])
    assert page.read_bytes() == original
    assert ownership.record_path(wiki).read_bytes() == receipt


def test_reviewed_exact_bytes_permit_replacement_and_deletion_without_marker(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    page = wiki / "page.md"
    original = b"# Reviewed\r\n\r\nSynthetic prose.\r\n"
    page.write_bytes(original)
    candidate = ownership.review(wiki, page.name)
    assert candidate.sha256 == sha(original)
    assert not ownership.record_path(wiki).exists()  # candidate creation grants nothing
    assert accept(wiki, page.name)
    assert page.read_bytes() == original
    safety.transact(wiki, {page.name: "replacement\n"}, [])
    assert ownership.load(wiki) == {page.name: sha(b"replacement\n")}
    safety.transact(wiki, {}, [page.name])
    assert not page.exists() and ownership.load(wiki) == {}


def test_repeat_adoption_is_unchanged_and_authority_changes_stale_the_base(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_text("# Reviewed\n")
    before = safety.base_digest(wiki)
    assert accept(wiki, "page.md")
    assert safety.base_digest(wiki) != before
    receipt = ownership.record_path(wiki)
    info, raw = receipt.stat(), receipt.read_bytes()
    assert not accept(wiki, "page.md")
    assert receipt.stat() == info and receipt.read_bytes() == raw


@pytest.mark.parametrize("change", ["page", "base", "authority"])
def test_stale_adoption_candidate_refuses(repo: Path, change: str) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    page = wiki / "page.md"
    page.write_bytes(b"# Review\n")
    candidate = ownership.review(wiki, page.name)
    if change == "page":
        page.write_bytes(page.read_bytes() + b" ")
    elif change == "base":
        (wiki / "index.json").write_text("{}\n")
    else:
        accept(wiki, page.name)
    raw = page.read_bytes()
    with pytest.raises(safety.StaleBaseError):
        ownership.adopt(wiki, page.name, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    assert page.read_bytes() == raw


def test_exact_candidate_hash_is_required_even_with_a_fresh_base(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_bytes(b"# Review\r\n")
    with pytest.raises(ownership.OwnershipError, match="candidate bytes differ"):
        ownership.adopt(wiki, "page.md", reviewed_sha256=sha(b"# Review\n"), expected_base=safety.base_digest(wiki))
    assert not ownership.record_path(wiki).exists()


def test_transaction_record_limit_does_not_invalidate_existing_ownership(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "# Reviewed\n"}, [])
    receipt = ownership.record_path(wiki).read_bytes()
    monkeypatch.setattr(safety, "MAX_RECORD_BYTES", len(receipt) - 1)
    assert ownership.load(wiki) == {"page.md": sha(b"# Reviewed\n")}
    with pytest.raises(safety.UnsafePathError, match="too many files"):
        safety.transact(wiki, {"page.md": "replacement"}, [])
    assert (wiki / "page.md").read_bytes() == b"# Reviewed\n"
    assert ownership.record_path(wiki).read_bytes() == receipt


def test_ownership_record_growth_refuses_before_changing_pages(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "# Reviewed\n"}, [])
    receipt = ownership.record_path(wiki).read_bytes()
    monkeypatch.setattr(ownership, "MAX_OWNERSHIP_BYTES", len(receipt))
    with pytest.raises(safety.UnsafePathError, match="too many files for one ownership record"):
        safety.transact(wiki, {"new.md": "# Added\n"}, [])
    assert not (wiki / "new.md").exists()
    assert ownership.record_path(wiki).read_bytes() == receipt


@pytest.mark.parametrize("bad", ["../page.md", "index.json", "credentials.md", ".env.md", "page.md\n"])
def test_adoption_rejects_unsafe_or_secret_names_before_reading(repo: Path, bad: str) -> None:
    with pytest.raises(ownership.OwnershipError):
        ownership.review(repo / "docs/wiki", bad)


def test_adoption_and_ownership_refuse_symlinks_and_nonregular_files(repo: Path, tmp_path: Path) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(b"unchanged")
    page = wiki / "page.md"
    page.symlink_to(victim)
    with pytest.raises(safety.UnsafePathError, match="symlink"):
        ownership.review(wiki, page.name)
    page.unlink()
    os.mkfifo(page)
    with pytest.raises(safety.UnsafePathError, match="regular file"):
        ownership.review(wiki, page.name)
    page.unlink()
    page.write_text("# Reviewed\n")
    assert accept(wiki, page.name)
    receipt = ownership.record_path(wiki)
    receipt.unlink()
    receipt.symlink_to(victim)
    with pytest.raises(safety.RecoveryError, match="regular file"):
        safety.transact(wiki, {page.name: "replacement"}, [])
    assert victim.read_bytes() == b"unchanged"


@pytest.mark.parametrize("edit", ["path", "hash", "identity", "extra", "boolean-version", "duplicate"])
def test_malformed_authority_is_kept_and_confers_no_authority(repo: Path, edit: str) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "# Synthetic\n"}, [])
    receipt = ownership.record_path(wiki)
    data = json.loads(receipt.read_bytes())
    if edit == "path":
        data["pages"] = {"../../README.md": "0" * 64}
    elif edit == "hash":
        data["pages"]["page.md"] = "instructions, not a hash"
    elif edit == "identity":
        data["identity"] = "0" * 64
    elif edit == "extra":
        data["command"] = "ignored"
    elif edit == "boolean-version":
        data["version"] = True
    raw = json.dumps(data)
    if edit == "duplicate":
        raw = raw[:-1] + ', "pages": {}}'
    receipt.write_text(raw)
    before = (wiki / "page.md").read_bytes()
    with pytest.raises(safety.RecoveryError, match="untrusted ownership"):
        safety.transact(wiki, {"page.md": "replacement"}, [])
    assert (wiki / "page.md").read_bytes() == before and receipt.read_text() == raw


def test_other_wiki_clone_and_worktree_do_not_inherit_authority(repo: Path, tmp_path: Path) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "# Original\n"}, [])
    receipt = ownership.record_path(wiki).read_bytes()
    other = repo / "other-wiki"
    other.mkdir()
    (other / "page.md").write_bytes((wiki / "page.md").read_bytes())
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "--quiet", "--no-hardlinks", str(repo), str(clone)], check=True)
    worktree = tmp_path / "worktree"
    run_git(repo, "worktree", "add", "--quiet", "--detach", str(worktree))
    for target in (other, clone / "docs/wiki", worktree / "docs/wiki"):
        target.mkdir(parents=True, exist_ok=True)
        (target / "page.md").write_bytes((wiki / "page.md").read_bytes())
        assert ownership.load(target) == {}
        with pytest.raises(ownership.OwnershipError):
            safety.transact(target, {"page.md": "replacement"}, [])
        # Copying an old local receipt into the new administration still fails identity validation.
        path = ownership.record_path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(receipt)
        with pytest.raises(safety.RecoveryError, match="untrusted ownership"):
            ownership.load(target)
        path.unlink()
        assert accept(target, "page.md")


def test_review_candidate_is_bound_to_its_wiki_even_for_identical_file_metadata(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    other = repo / "other-wiki"
    wiki.mkdir(parents=True)
    other.mkdir()
    (wiki / "page.md").write_bytes(b"# Synthetic\n")
    os.link(wiki / "page.md", other / "page.md")
    candidate = ownership.review(wiki, "page.md")
    with pytest.raises(safety.StaleBaseError):
        ownership.adopt(other, candidate.page, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    assert not ownership.record_path(other).exists()


@pytest.mark.parametrize(
    "step",
    ["prepared", "moved-new:page.md", "moved-old:old.md", "moved-new:@ownership", "committed", "cleanup:staging"],
)
def test_publication_and_ownership_recover_together(repo: Path, step: str) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "old", "old.md": "dropped", "index.json": "old index"}, [])
    before = {p.name: p.read_bytes() for p in wiki.iterdir()}
    old_ownership = ownership.load(wiki)
    env = dict(os.environ, PYTHONPATH=str(Path(safety.__file__).resolve().parents[1]))
    script = f"""import os, sys
from pathlib import Path
from repowiki import safety
safety._step = lambda name: os.kill(os.getpid(), 9) if name == {step!r} else None
safety.transact(Path(sys.argv[1]), {{'page.md': 'new', 'added.md': 'added', 'index.json': 'new index'}}, ['old.md'])
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(wiki)], env=env, capture_output=True, text=True, timeout=30
    )
    assert child.returncode == -9, child.stderr
    assert safety.recover(wiki)
    committed = step in ("committed", "cleanup:staging")
    after = {"page.md": b"new", "added.md": b"added", "index.json": b"new index"}
    assert {p.name: p.read_bytes() for p in wiki.iterdir()} == (after if committed else before)
    assert ownership.load(wiki) == ({"page.md": sha(b"new"), "added.md": sha(b"added")} if committed else old_ownership)
    assert not safety.recover(wiki)


ADOPTION_STEPS = [
    "staged",
    "prepared",
    "moved-old:@ownership",
    "moved-new:@ownership",
    "synced",
    "committed",
    "cleanup:staging",
    "cleaned",
]


@pytest.mark.parametrize("step", ADOPTION_STEPS)
@pytest.mark.parametrize("existing", [False, True])
def test_interrupted_adoption_recovers_complete_old_or_new(repo: Path, step: str, existing: bool) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_bytes(b"# Reviewed\r\n")
    if existing:
        safety.transact(wiki, {"other.md": "# Other\n"}, [])
    old = ownership.load(wiki)
    page_bytes = {p.name: p.read_bytes() for p in wiki.iterdir()}
    env = dict(os.environ, PYTHONPATH=str(Path(safety.__file__).resolve().parents[1]))
    script = f"""import os, sys
from pathlib import Path
from repowiki import ownership, safety
wiki = Path(sys.argv[1])
c = ownership.review(wiki, 'page.md')
safety._step = lambda name: os.kill(os.getpid(), 9) if name == {step!r} else None
ownership.adopt(wiki, c.page, reviewed_sha256=c.sha256, expected_base=c.base)
"""
    child = subprocess.run(
        [sys.executable, "-c", script, str(wiki)], env=env, capture_output=True, text=True, timeout=30
    )
    transition_exists = existing or step != "moved-old:@ownership"
    # With no previous receipt, moving an old one never occurs: assert that this
    # parameter completes adoption rather than dropping it or reporting a skip.
    assert child.returncode == (-9 if transition_exists else 0), child.stderr
    committed = not transition_exists or ADOPTION_STEPS.index(step) >= ADOPTION_STEPS.index("committed")
    assert safety.recover(wiki) == (transition_exists and step != "cleaned")
    assert ownership.load(wiki) == (old | {"page.md": sha(page_bytes["page.md"])} if committed else old)
    assert {p.name: p.read_bytes() for p in wiki.iterdir()} == page_bytes
    assert not safety.recover(wiki)


def test_prepared_adoption_refuses_bytes_changed_while_staging(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    page = wiki / "page.md"
    page.write_bytes(b"# Reviewed\n")
    candidate = ownership.review(wiki, page.name)
    monkeypatch.setattr(safety, "_step", lambda name: page.write_bytes(b"# Edited\n") if name == "prepared" else None)
    with pytest.raises(safety.StaleBaseError, match="reviewed adoption bytes changed"):
        ownership.adopt(wiki, page.name, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    assert page.read_bytes() == b"# Edited\n" and ownership.load(wiki) == {}


def test_case_alias_adoption_is_refused_before_any_write_and_keeps_ownership_loadable(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    wiki.mkdir(parents=True)
    (wiki / "page.md").write_bytes(b"# Reviewed\n")
    assert accept(wiki, "page.md")
    if not (wiki / "Page.md").exists():  # a case-sensitive checkout: the alias is a different file
        (wiki / "Page.md").write_bytes(b"# Alias\n")
    receipt = ownership.record_path(wiki)
    authority = receipt.read_bytes()
    base = safety.base_digest(wiki)
    candidate = ownership.review(wiki, "Page.md")
    with pytest.raises(ownership.OwnershipError, match="differs only by case"):
        ownership.adopt(wiki, "Page.md", reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    assert receipt.read_bytes() == authority and safety.base_digest(wiki) == base  # nothing was committed
    assert ownership.load(wiki) == {"page.md": sha(b"# Reviewed\n")}  # still readable, one entry
    assert not accept(wiki, "page.md")  # repeating the existing spelling is unchanged
    safety.transact(wiki, {"page.md": "next\n"}, [])  # publication still proceeds
    assert ownership.load(wiki) == {"page.md": sha(b"next\n")}


def test_publication_never_commits_a_case_alias_of_an_owned_page(repo: Path) -> None:
    wiki = repo / "docs/wiki"
    safety.transact(wiki, {"page.md": "# Owned\n"}, [])
    receipt = ownership.record_path(wiki)
    authority, original = receipt.read_bytes(), (wiki / "page.md").read_bytes()
    with pytest.raises(ownership.OwnershipError):
        safety.transact(wiki, {"Page.md": "# Alias\n"}, [])
    assert receipt.read_bytes() == authority and (wiki / "page.md").read_bytes() == original
    assert ownership.load(wiki) == {"page.md": sha(b"# Owned\n")}
    safety.transact(wiki, {"page.md": "# Next\n"}, [])
    assert ownership.load(wiki) == {"page.md": sha(b"# Next\n")}
