"""All unauthorized executions stop before constructing or calling a backend."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from conftest import commit, register, write
from repowiki.approval import authorize, receipt_path
from repowiki.cli import main
from repowiki.config import Settings
from repowiki.generate import generate, plan
from repowiki.git import Snapshot
from repowiki.runners import FakeRunner
from repowiki.survey import survey
from repowiki.update import update

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)  # the run's instant is the caller's, never the clock


@pytest.mark.parametrize("flag", ["--cheap", "--strong"])
@pytest.mark.parametrize("allow_remote", [False, True])
def test_cloud_refusal_precedes_approval_without_calls(repo, monkeypatch, capsys, flag, allow_remote):
    constructed = []
    probes = []
    fake = FakeRunner()

    def runners(self):
        constructed.append(True)
        return fake, fake

    def show(*args, **kwargs):
        probes.append(True)
        raise AssertionError("unauthorized server probe")

    monkeypatch.setattr(Settings, "runners", runners)
    monkeypatch.setattr("repowiki.runners._ollama_show", show)
    args = ["generate", str(repo), flag, "ollama:gpt-oss:120b-cloud"]
    if allow_remote:
        args.append("--allow-remote")
    assert main(args) == 2
    expected = "unapproved model or endpoint" if allow_remote else "cloud model"
    assert expected in capsys.readouterr().err
    assert constructed == [] and probes == [] and fake.calls == []


@pytest.mark.parametrize("missing", ["config", "receipt", "policy"])
def test_missing_authority_has_zero_calls(repo, monkeypatch, missing):
    fake = FakeRunner()
    constructed = []
    def runners(self):
        constructed.append(True)
        return fake, fake
    monkeypatch.setattr(Settings, "runners", runners)
    if missing == "config":
        (repo / ".repowiki.toml").unlink()
    elif missing == "receipt":
        receipt_path(repo).unlink()
    else:
        monkeypatch.delenv("REPOWIKI_POLICY_COMMIT")
    assert main(["generate", str(repo), "--model", "fake"]) == 2
    assert fake.calls == [] and constructed == []


@pytest.mark.parametrize("change", ["public", "model", "budget", "ref", "overlay", "config"])
def test_unapproved_changes_have_zero_calls(repo, monkeypatch, change):
    fake = FakeRunner()
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)
    ref = "HEAD"
    if change == "public":
        settings.privacy = {"visibility": "public", "allowlist": [], "context_classes": [], "prompt_exclude": []}
    elif change == "model":
        settings.cheap = "claude"
    elif change == "budget":
        settings.budget_tokens = 2_000_000
    elif change == "ref":
        ref = "HEAD~1"
    elif change == "overlay":
        settings.privacy = {"visibility": "private", "allowlist": ["src/*"], "context_classes": ["instructions"], "prompt_exclude": []}
    else:
        with (repo / ".repowiki.toml").open("a") as stream:
            stream.write("# changed\n")
    with pytest.raises(ValueError):
        generate(repo, settings, ref=ref, now=NOW, log=lambda _: None)
    assert fake.calls == []


@pytest.mark.parametrize("entry", ["plan", "update"])
def test_direct_entry_cannot_bypass_approval(repo, entry):
    receipt_path(repo).unlink()
    fake = FakeRunner()
    settings = Settings(cheap="fake")
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError):
        if entry == "plan":
            snap = Snapshot.at(repo)
            plan(snap, survey(snap, settings.wiki_dir), settings, fake, "synthetic", lambda _: None)
        else:
            update(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


def test_empty_and_approved_context(repo, monkeypatch):
    register(repo, monkeypatch, allowlist=[], context_classes=["source"])
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")
    assert survey(Snapshot.at(repo), settings.wiki_dir, policy=settings.context_policy).files == []
    register(repo, monkeypatch, allowlist=["src/widgetry/cli.py"], context_classes=["source"])
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "generate")
    assert survey(Snapshot.at(repo), settings.wiki_dir, policy=settings.context_policy).files == ["src/widgetry/cli.py"]


def test_new_policy_preserves_older_source_commit(repo, monkeypatch):
    old = Snapshot.at(repo).commit
    register(repo, monkeypatch, allowlist=["src/*"], context_classes=["source"])
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["refs"].append(old)
    path.write_text(json.dumps(receipt))
    settings = Settings(cheap="fake", min_pages=1)
    result = generate(repo, settings, ref=old, now=NOW, log=lambda _: None)
    assert result.commit == old
    assert json.loads((repo / "docs/wiki/index.json").read_text())["indexed_commit"] == old


@pytest.mark.parametrize("invalid", ["partial", "permissions", "duplicate", "identity"])
def test_invalid_receipt_has_zero_calls(repo, invalid):
    path = receipt_path(repo)
    if invalid == "partial":
        path.write_text('{"version":')
    elif invalid == "permissions":
        path.chmod(0o644)
    elif invalid == "duplicate":
        path.write_text('{"version":1,"version":1}')
    else:
        receipt = json.loads(path.read_text())
        receipt["root"] = str(repo.parent / "other-clone")
        path.write_text(json.dumps(receipt))
    fake = FakeRunner()
    settings = Settings(cheap="fake")
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError):
        generate(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


def test_policy_change_invalidates_saved_plan(repo, monkeypatch):
    from repowiki.fake import answer

    fake = FakeRunner(respond=answer)
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)
    generate(repo, settings, plan_only=True, now=NOW, log=lambda _: None)
    assert len(fake.calls) == 1
    generate(repo, settings, plan_only=True, now=NOW, log=lambda _: None)
    assert len(fake.calls) == 1
    # The allowed context stays identical, but a new policy requires new approval.
    register(repo, monkeypatch, visibility="unknown")
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)
    generate(repo, settings, plan_only=True, now=NOW, log=lambda _: None)
    assert len(fake.calls) == 2


def test_revocation_between_calls_preserves_existing_wiki(repo):
    from repowiki.fake import answer

    generate(repo, Settings(cheap="fake", min_pages=2), now=NOW, log=lambda _: None)
    index = repo / "docs/wiki/index.json"
    before = index.read_bytes()
    def revoke(prompt):
        receipt_path(repo).unlink()
        return answer(prompt)
    fake = FakeRunner(respond=revoke)
    settings = Settings(cheap="fake", min_pages=2, checkpoints=False)
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError):
        generate(repo, settings, now=NOW, log=lambda _: None)
    assert len(fake.calls) == 1
    assert index.read_bytes() == before


def test_preview_approval_cannot_send(repo):
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["operations"] = ["preview"]
    path.write_text(json.dumps(receipt))
    settings = Settings(cheap="fake")
    authorize(repo, settings, "HEAD", "preview")
    snap = Snapshot.at(repo)
    fake = FakeRunner()
    with pytest.raises(ValueError, match="operation"):
        plan(snap, survey(snap, settings.wiki_dir), settings, fake, "synthetic", lambda _: None)
    assert fake.calls == []


def test_runner_label_does_not_grant_approval(repo):
    class Impostor:
        label = "fake"
        calls = []
        def run(self, prompt):
            self.calls.append(prompt)
            return ""
    settings = Settings(cheap="fake")
    fake = Impostor()
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError, match="runner"):
        generate(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


def test_opaque_cli_endpoint_is_not_approvable(repo):
    path = receipt_path(repo)
    receipt = json.loads(path.read_text())
    receipt["models"].append("claude")
    receipt["endpoints"]["claude"] = "claude:managed-by-cli"
    path.write_text(json.dumps(receipt))
    fake = FakeRunner()
    settings = Settings(cheap="claude")
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError, match="endpoint"):
        generate(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


@pytest.mark.parametrize("name,value", [("jobs", 0), ("jobs", 1.5), ("retries", 1.5), ("timeout", 0)])
def test_invalid_limits_stop_before_calls(repo, name, value):
    fake = FakeRunner()
    settings = Settings(cheap="fake")
    setattr(settings, name, value)
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError, match="limit"):
        generate(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


def test_previous_wiki_prompt_exclusion_wins(repo, monkeypatch):
    generate(repo, Settings(cheap="fake", min_pages=2), now=NOW, log=lambda _: None)
    register(repo, monkeypatch, prompt_exclude=["docs/wiki/*"])
    write(repo, "src/widgetry/cli.py", "def main():\n    return 2\n")
    commit(repo, "source update")
    fake = FakeRunner()
    settings = Settings(cheap="fake", min_pages=2)
    settings.runners = lambda: (fake, fake)
    with pytest.raises(ValueError, match="previous wiki"):
        update(repo, settings, now=NOW, log=lambda _: None)
    assert fake.calls == []


@pytest.mark.parametrize("other", ["authorized", "older-commit", "other-clone"])
def test_page_context_is_bound_to_authorized_snapshot(repo, tmp_path, monkeypatch, other):
    from conftest import FILES, run_git
    from repowiki.fake import answer
    from repowiki.generate import Context, write_page
    from repowiki.prompts import PlannedPage

    settings = Settings(cheap="fake", checkpoints=False)
    authorize(repo, settings, "HEAD", "generate")
    fake = FakeRunner(respond=answer)
    if other == "authorized":
        root = repo
        snap = Snapshot.at(repo)
    elif other == "older-commit":
        root = repo
        snap = Snapshot.at(repo, "HEAD~1")
    else:
        root = tmp_path / "unregistered"
        root.mkdir()
        run_git(root, "init", "-q", "-b", "main")
        for rel, content in FILES.items():
            write(root, rel, content)
        commit(root, "unrelated")
        snap = Snapshot.at(root)
    page = PlannedPage("1-overview.md", "Overview", None, "What it is.", ["src/widgetry/cli.py"])
    ctx = Context(root, snap, settings, "synthetic", fake, fake, [page], lambda _: None)
    if other == "authorized":
        write_page(ctx, page)
        assert fake.calls
    else:
        with pytest.raises(ValueError, match="authorized source"):
            write_page(ctx, page)
        assert fake.calls == []


def test_excluded_wiki_page_metadata_stays_out_of_update_prompts(repo, monkeypatch):
    from repowiki.fake import answer

    generate(repo, Settings(cheap="fake", min_pages=2), now=NOW, log=lambda _: None)
    index = json.loads((repo / "docs/wiki/index.json").read_text())
    files = {p["file"]: p["title"] for p in index["pages"]}
    assert "4-testing.md" in files and "2-widgetry.md" in files
    register(repo, monkeypatch, prompt_exclude=["docs/wiki/4-testing.md"])
    write(repo, "src/widgetry/engine.py", "".join(f"# changed line {n}\n" for n in range(1, 30)))
    commit(repo, "engine update")
    fake = FakeRunner(respond=answer)
    settings = Settings(cheap="fake", min_pages=2, checkpoints=False)
    settings.runners = lambda: (fake, fake)
    result = update(repo, settings, now=NOW, log=lambda _: None)
    assert "2-widgetry.md" in result.written and "4-testing.md" not in result.written
    assert fake.calls
    for prompt in fake.calls:
        assert "4-testing.md" not in prompt
        assert f": {files['4-testing.md']}" not in prompt
    assert any("1-overview.md" in prompt for prompt in fake.calls)


def test_excluded_parent_page_stays_out_of_child_update_prompts(repo, monkeypatch):
    from repowiki.fake import answer

    generate(repo, Settings(cheap="fake", min_pages=2), now=NOW, log=lambda _: None)
    path = repo / "docs/wiki/index.json"
    index = json.loads(path.read_text())
    for entry in index["pages"]:
        if entry["file"] == "2-widgetry.md":
            entry["parent"] = "4-testing.md"
    path.write_text(json.dumps(index, indent=2) + "\n")
    register(repo, monkeypatch, prompt_exclude=["docs/wiki/4-testing.md"])
    write(repo, "src/widgetry/engine.py", "".join(f"# changed line {n}\n" for n in range(1, 30)))
    commit(repo, "engine update")
    fake = FakeRunner(respond=answer)
    settings = Settings(cheap="fake", min_pages=2, checkpoints=False)
    settings.runners = lambda: (fake, fake)
    result = update(repo, settings, now=NOW, log=lambda _: None)
    assert result.written == ["2-widgetry.md"]
    assert fake.calls
    for prompt in fake.calls:
        assert "4-testing.md" not in prompt
    assert any("- 2-widgetry.md: Widgetry\n" in prompt for prompt in fake.calls)
