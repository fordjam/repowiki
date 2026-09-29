"""Shared fixtures: a small synthetic git repository built fresh in tmp for every test."""

from __future__ import annotations

import subprocess
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


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No user git config, fixed identity, no network remotes."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test Author")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "widgetry"
    root.mkdir()
    run_git(root, "init", "-q", "-b", "main")
    for rel, content in FILES.items():
        write(root, rel, content)
    commit(root, "initial")
    return root
