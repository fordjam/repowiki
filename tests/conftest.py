"""Shared fixtures: a small synthetic git repository built fresh in tmp for every test."""

from __future__ import annotations

import subprocess
import json
from pathlib import Path

import pytest

FILES = {
    "README.md": "# Widgetry\n\nWidgetry turns sprockets into widgets.\n\n## Usage\n\n    widgetry build\n",
    "pyproject.toml": (
        '[project]\nname = "widgetry"\nversion = "0.0.1"\n\n[project.scripts]\nwidgetry = "widgetry.cli:main"\n'
    ),
    "src/widgetry/__init__.py": '"""Widgetry."""\n',
    "src/widgetry/cli.py": (
        "import sys\n\nfrom .engine import build\n\n\n"
        "def main(argv=None):\n    args = argv or sys.argv[1:]\n    print(build(args))\n    return 0\n"
    ),
    "src/widgetry/engine.py": "".join(f"# line {n}\n" for n in range(1, 30))
    + "def build(args):\n    return len(args)\n",
    "src/widgetry/store/db.py": "class Store:\n    def __init__(self):\n        self.items = []\n",
    "tests/test_engine.py": "from widgetry.engine import build\n\n\ndef test_build():\n    assert build([]) == 0\n",
    "assets/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00binary",
}


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, message: str = "change") -> str:
    run_git(repo, "add", "-A")
    run_git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return run_git(repo, "rev-parse", "HEAD")


def write(repo: Path, rel: str, content: str | bytes) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")


def register(repo: Path, monkeypatch: pytest.MonkeyPatch, *, config_prefix: str = "", **privacy: object) -> str:
    """Explicit operator registration of a synthetic clone; never a production bypass."""
    from repowiki.approval import LIMITS, digest, receipt_path
    from repowiki.config import load_settings
    from repowiki.git import git_dir

    config = ('[models]\nmodel = "fake"\n[privacy]\nvisibility = "private"\n'
              'allowlist = ["*"]\ncontext_classes = ["source", "readme", "previous_wiki"]\n')
    for key, value in privacy.items():
        lines = [line for line in config.splitlines() if not line.startswith(key + " =")]
        config = "\n".join(lines) + "\n" + key + " = " + json.dumps(value) + "\n"
    config = config_prefix + config
    write(repo, ".repowiki.toml", config)
    sha = commit(repo, "synthetic privacy policy")
    monkeypatch.setenv("REPOWIKI_POLICY_COMMIT", sha)
    path = receipt_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.parent.chmod(0o700)
    path.parent.chmod(0o700)
    ceilings = (32, 1_000_000, 900, 8, 5, 120, 1_000_000)
    receipt = {
        "version": 1, "root": str(repo.resolve()), "git_dir": str(git_dir(repo).resolve()),
        "policy_commit": sha, "config_digest": digest(config.encode()),
        "privacy": load_settings(repo).privacy, "models": ["fake"], "endpoints": {"fake": "builtin"},
        "limits": dict(zip(LIMITS, ceilings)), "refs": ["HEAD", sha],
        "operations": ["generate", "plan", "preview", "update"],
    }
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    return sha


@pytest.fixture(autouse=True)
def _register_legacy_site_config(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicitly reapprove only the legacy site's known synthetic config change.

    Other config mutations (especially approval refusal cases) remain unapproved.
    The test still uses the real CLI, policy reader, and external receipt checks.
    """
    if request.node.nodeid != "tests/test_usability.py::test_site_reads_wiki_dir_from_the_repo_config":
        return

    def commit_site_config(root: Path, message: str = "change") -> str:
        assert message == "config"
        config = (root / ".repowiki.toml").read_text()
        assert config == 'wiki_dir = "handbook"\n'
        return register(root, monkeypatch, config_prefix=config)

    monkeypatch.setattr(request.module, "commit", commit_site_config)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No user git config, fixed identity, no network remotes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("REPOWIKI_POLICY_COMMIT", raising=False)
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test Author")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")


@pytest.fixture(autouse=True)
def _offline_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never contact an Ollama server: /api/show answers "installed locally"."""
    from repowiki import runners

    monkeypatch.setattr(runners, "_ollama_show", lambda base, model, timeout=10.0: {"details": {}})


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "widgetry"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    for rel, content in FILES.items():
        write(root, rel, content)
    commit(root, "initial")
    register(root, monkeypatch)
    return root
