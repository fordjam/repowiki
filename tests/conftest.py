"""Shared fixtures: a small synthetic git repository built fresh in tmp for every test.

The default suite is hermetic: no installed backend CLI can be discovered, HOME and the XDG
directories are empty synthetic ones, and network connections other than loopback are refused.
Tests that run a real backend binary are marked ``integration`` and run only when the operator
names that binary explicitly (``REPOWIKI_INTEGRATION_<BACKEND>=/path/to/binary``).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_BINARIES = ("claude", "opencode", "codex", "ollama")
INTEGRATION_VAR = "REPOWIKI_INTEGRATION_{}"
DEAD_PROXY = "http://127.0.0.1:9"  # the discard port: a child that honours proxies cannot reach anything
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
LOCAL_NAMES = ("localhost", "localhost.", "")
# Inherited credentials never reach a test or a backend started by one (the default suite or integration).
CREDENTIAL_RE = re.compile(
    r"^(ANTHROPIC|OPENAI|CODEX|OPENCODE|CLAUDE_CODE|AWS|GOOGLE|GCLOUD|CLOUDSDK|CLOUD_ML|VERTEX|AZURE|GITHUB|GH|"
    r"GITLAB|HF|HUGGINGFACE|GROQ|MISTRAL|DEEPSEEK|OPENROUTER|XAI|GEMINI|MOONSHOT|ZAI|ZHIPU|TOGETHER|FIREWORKS)_"
    r"|(_API_KEY|_KEY|_TOKEN|_SECRET|_SECRET_KEY|_PASSWORD|_PASSWD|_CREDENTIALS?|_AUTH)$"
    r"|^(REPOWIKI_PASS_ENV|CLAUDE_CONFIG_DIR)$",
    re.I,
)
# macOS child-process sandbox: no outbound connection except loopback, no name lookups.
OFFLINE_PROFILE = (
    "(version 1)(allow default)(deny network-outbound)"
    '(allow network-outbound (remote ip "localhost:*"))(allow network-outbound (remote unix-socket))'
    '(deny network-outbound (remote unix-socket (path-literal "/private/var/run/mDNSResponder")))'
)
# A child that must be refused: a non-loopback connection and a name lookup.
NETWORK_CANARY = """
import socket
out = []
try:
    socket.create_connection(("192.0.2.1", 9), timeout=5)
    out.append("connected")
except OSError as exc:
    out.append("denied" if "timed out" not in str(exc) else "timeout")
try:
    socket.getaddrinfo("example.com", 443)
    out.append("resolved")
except OSError:
    out.append("denied")
print(" ".join(out))
"""

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


def adopt_page(page: Path) -> None:
    """Explicitly review/adopt one known synthetic fixture before testing its publication.

    Tests that protect collisions or manual edits never call this helper.
    """
    from repowiki.ownership import adopt, review

    reviewed_bytes = page.read_bytes()
    candidate = review(page.parent, page.name)
    assert candidate.sha256 == hashlib.sha256(reviewed_bytes).hexdigest()
    assert adopt(page.parent, page.name, reviewed_sha256=candidate.sha256, expected_base=candidate.base)
    assert page.read_bytes() == reviewed_bytes


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


# --------------------------------------------------------------------------- hermetic default suite


def integration_binary(backend: str, environ: dict[str, str] | None = None) -> str | None:
    """The operator-selected binary for a backend's integration tests, or None (those tests skip).

    Only an explicit absolute path to an executable file counts: nothing is looked up on PATH.
    """
    value = (os.environ if environ is None else environ).get(INTEGRATION_VAR.format(backend.upper()), "")
    path = Path(value)
    if value and path.is_absolute() and path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def offline_prefix() -> list[str] | None:
    """argv prefix that runs a child process without network access, where this machine offers one."""
    if sys.platform == "darwin" and os.path.exists("/usr/bin/sandbox-exec"):
        return ["/usr/bin/sandbox-exec", "-p", OFFLINE_PROFILE]
    unshare = shutil.which("unshare")
    if sys.platform.startswith("linux") and unshare:
        return [unshare, "--user", "--map-root-user", "--net"]
    return None


def wrapper_runs(prefix: list[str]) -> bool:
    """Whether this process may start a child under ``prefix`` at all (a sandboxed parent may forbid it)."""
    try:
        out = subprocess.run([*prefix, sys.executable, "-I", "-c", "pass"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


def network_denied(prefix: list[str]) -> bool:
    """Subprocess canary: under ``prefix`` a child is refused a non-loopback connection and a name lookup."""
    try:
        out = subprocess.run(
            [*prefix, sys.executable, "-I", "-c", NETWORK_CANARY], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and out.stdout.split() == ["denied", "denied"]


@pytest.fixture
def offline() -> list[str]:
    """For integration tests: the argv prefix that denies a real backend binary the network, proven by
    the subprocess canary first. Without one the test is skipped rather than run online."""
    prefix = offline_prefix()
    if prefix is None or not network_denied(prefix):
        pytest.skip("integration: cannot deny child processes the network on this machine")
    return prefix


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration(backend): runs a real backend binary; opt in with REPOWIKI_INTEGRATION_<BACKEND>"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("integration")
        if marker is None:
            continue
        backend = marker.args[0]
        if integration_binary(backend) is None:
            var = INTEGRATION_VAR.format(backend.upper())
            item.add_marker(pytest.mark.skip(reason=f"integration: set {var} to the {backend} binary to run"))


def hermetic_path(original: str, shadow_root: Path) -> str:
    """PATH with every backend CLI hidden: a directory holding one is replaced by a shadow directory
    of symlinks to everything else in it, so git, sh and the rest still resolve."""
    out: list[str] = []
    for i, entry in enumerate(original.split(os.pathsep)):
        directory = Path(entry)
        if not entry or not directory.is_dir():
            continue
        if not any(os.path.lexists(directory / name) for name in BACKEND_BINARIES):
            out.append(entry)
            continue
        shadow = shadow_root / str(i)
        shadow.mkdir(parents=True, exist_ok=True)
        for child in directory.iterdir():
            if child.name.lower() not in BACKEND_BINARIES and not os.path.lexists(shadow / child.name):
                (shadow / child.name).symlink_to(child)
        out.append(str(shadow))
    return os.pathsep.join(out)


@pytest.fixture(scope="session")
def _hermetic_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    return hermetic_path(os.environ.get("PATH", os.defpath), tmp_path_factory.mktemp("hermetic-path"))


class NetworkBlocked(OSError):
    """A default test tried to reach a non-loopback address."""


def _local_host(host: object) -> bool:
    if host is None or (isinstance(host, str) and host.lower() in LOCAL_NAMES):
        return True
    try:
        ip = ipaddress.ip_address(str(host).split("%")[0])
    except ValueError:
        return False
    return ip.is_loopback or ip.is_unspecified


@pytest.fixture(autouse=True)
def _hermetic(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory, _hermetic_path: str
) -> None:
    """No discoverable backend CLI, no inherited credentials, an empty synthetic HOME, and no network
    beyond loopback in this process (backend binaries in integration tests run under ``offline``)."""
    monkeypatch.setenv("PATH", _hermetic_path)
    for name in list(os.environ):
        if CREDENTIAL_RE.search(name):
            monkeypatch.delenv(name, raising=False)
    for name in BACKEND_BINARIES:
        monkeypatch.delenv(f"REPOWIKI_{name.upper()}_BIN", raising=False)
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for var, sub in (("XDG_CONFIG_HOME", ".config"), ("XDG_DATA_HOME", ".local/share"), ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(var, str(home / sub))
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OLLAMA_HOST"):
        monkeypatch.delenv(var, raising=False)
    for var in PROXY_VARS:
        monkeypatch.setenv(var, DEAD_PROXY)
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1,::1")
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def check(sock: socket.socket, address: object) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6) and not _local_host(address[0]):  # type: ignore[index]
            raise NetworkBlocked(f"network blocked in the default test suite: {address!r}")

    def connect(self: socket.socket, address: object) -> None:
        check(self, address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: object) -> int:
        check(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: object, *args: object, **kwargs: object) -> list:
        if not _local_host(host):
            raise NetworkBlocked(f"name lookup blocked in the default test suite: {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
