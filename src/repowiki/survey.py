"""What the planner sees: README, a trimmed file tree, entry points and tests."""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field

from .git import Snapshot
from .policy import DEFAULT_POLICY, Policy

# Paths that never make good citations: dependencies, build output, lock files, media.
SKIP_DIRS = {
    "node_modules", "vendor", "third_party", "dist", "build", ".venv", "venv", "__pycache__",
    ".git", ".idea", ".vscode", "target", ".next", ".cache", "coverage",
}  # fmt: skip
SKIP_SUFFIXES = (
    ".lock", ".min.js", ".min.css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".webp", ".pdf", ".zip", ".gz", ".tar", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mp3",
    ".sqlite", ".db", ".bin", ".pyc", ".so", ".dylib", ".dll", ".exe", ".jar", ".class",
)  # fmt: skip
SKIP_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock", "go.sum"}

ENTRY_NAMES = {
    "main.py", "__main__.py", "cli.py", "app.py", "manage.py", "server.py", "main.go", "main.rs",
    "lib.rs", "index.js", "index.ts", "main.ts", "main.js", "server.js", "server.ts", "Main.java",
    "Program.cs", "main.c", "main.cpp", "Makefile", "Dockerfile",
}  # fmt: skip
MANIFESTS = ("pyproject.toml", "setup.cfg", "setup.py", "package.json", "Cargo.toml", "go.mod", "pom.xml",
             "build.gradle", "Gemfile", "composer.json", "CMakeLists.txt", "mix.exs")  # fmt: skip
README_RE = re.compile(r"^readme(\.[a-z]+)?$", re.I)
TEST_RE = re.compile(r"(^|/)(tests?|spec|__tests__)/|(^|/)test_[^/]+$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")


def citable(path: str, wiki_dir: str, policy: Policy = DEFAULT_POLICY) -> bool:
    """True for files a wiki page may quote and cite (never secrets, dependencies or media)."""
    if path.startswith(wiki_dir.rstrip("/") + "/") or policy.secret(path):
        return False
    parts = path.split("/")
    if any(p in SKIP_DIRS for p in parts[:-1]):
        return False
    name = parts[-1]
    return name not in SKIP_NAMES and not name.lower().endswith(SKIP_SUFFIXES)


@dataclass
class Survey:
    files: list[str]
    readme: str | None = None
    readme_text: str = ""
    manifests: list[str] = field(default_factory=list)
    entry_points: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    languages: list[tuple[str, int]] = field(default_factory=list)


def survey(snap: Snapshot, wiki_dir: str, readme_chars: int = 8000, policy: Policy = DEFAULT_POLICY) -> Survey:
    files = [f for f in snap.files if citable(f, wiki_dir, policy)]
    s = Survey(files)
    readmes = sorted((f for f in files if README_RE.match(posixpath.basename(f))), key=lambda f: (f.count("/"), f))
    if readmes:
        s.readme = readmes[0]
        s.readme_text = (snap.read(s.readme) or "")[:readme_chars]
    s.manifests = [f for f in files if posixpath.basename(f) in MANIFESTS and f.count("/") <= 1]
    s.entry_points = _entry_points(snap, files, s.manifests)
    s.tests = [f for f in files if TEST_RE.search(f)]
    exts = Counter(posixpath.splitext(f)[1].lower() for f in files if posixpath.splitext(f)[1])
    s.languages = exts.most_common(8)
    return s


def _entry_points(snap: Snapshot, files: list[str], manifests: list[str]) -> list[str]:
    found: list[str] = []
    fileset = set(files)
    for manifest in manifests:
        for target in _declared_entries(manifest, snap.read(manifest) or ""):
            for candidate in _module_paths(target, fileset):
                if candidate not in found:
                    found.append(candidate)
    for f in files:
        name = posixpath.basename(f)
        if (name in ENTRY_NAMES or f.startswith(("bin/", "cmd/"))) and f.count("/") <= 3 and f not in found:
            found.append(f)
    return found[:40]


def _declared_entries(manifest: str, text: str) -> list[str]:
    name = posixpath.basename(manifest)
    try:
        if name == "pyproject.toml":
            data = tomllib.loads(text)
            scripts = {**data.get("project", {}).get("scripts", {}), **data.get("project", {}).get("gui-scripts", {})}
            poetry = data.get("tool", {}).get("poetry", {}).get("scripts", {})
            return [str(v) for v in [*scripts.values(), *poetry.values()] if isinstance(v, str)]
        if name == "package.json":
            data = json.loads(text)
            out = [data.get("main"), data.get("module")]
            bin_ = data.get("bin")
            out += list(bin_.values()) if isinstance(bin_, dict) else [bin_]
            return [str(v) for v in out if isinstance(v, str)]
    except (ValueError, AttributeError):
        return []
    return []


def _module_paths(target: str, fileset: set[str]) -> list[str]:
    """``pkg.cli:main`` or ``./bin/tool.js`` → matching file paths."""
    target = target.strip()
    if ":" in target and "/" not in target:
        mod = target.split(":", 1)[0].replace(".", "/")
        candidates = [f"{mod}.py", f"{mod}/__init__.py", f"src/{mod}.py", f"src/{mod}/__init__.py"]
    else:
        clean = posixpath.normpath(target.lstrip("./")) if target else ""
        candidates = [clean]
    return [c for c in candidates if c in fileset]


def tree_listing(snap: Snapshot, files: list[str], limit: int = 600) -> str:
    """Indented-free path list with sizes; large repos collapse deep directories."""
    if len(files) <= limit:
        return "\n".join(f"{f} ({_size(snap.sizes.get(f, 0))})" for f in files)
    shown: list[str] = []
    collapsed: Counter[str] = Counter()
    for depth in range(1, 12):
        shallow = [f for f in files if f.count("/") < depth]
        if len(shallow) > limit:
            break
        shown = shallow
    shown_set = set(shown)
    for f in files:
        if f not in shown_set:
            collapsed[_dir_at(f, max((s.count("/") for s in shown), default=0) + 1)] += 1
    lines = [f"{f} ({_size(snap.sizes.get(f, 0))})" for f in shown]
    lines += [f"{d}/ ... {n} more files" for d, n in sorted(collapsed.items())]
    return "\n".join(lines)


def _dir_at(path: str, depth: int) -> str:
    return "/".join(path.split("/")[:depth]) if path.count("/") >= depth else posixpath.dirname(path)


def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


def numbered(text: str, start: int = 1, end: int | None = None) -> str:
    lines = text.splitlines()
    end = len(lines) if end is None else min(end, len(lines))
    width = len(str(end))
    return "\n".join(f"{n:>{width}} | {lines[n - 1]}" for n in range(start, end + 1))


def excerpts(snap: Snapshot, paths: list[str], budget: int, policy: Policy = DEFAULT_POLICY) -> str:
    """Line-numbered source for `paths`, whole files first, trimmed to the character budget."""
    blocks: list[str] = []
    used = 0
    for path in paths:
        if policy.secret(path):
            continue  # never quote a secret, whatever asked for it
        text = snap.read(path)
        if text is None:
            continue
        total = len(text.splitlines())
        remaining = budget - used
        if remaining < 400:
            blocks.append(f"=== {path} ({total} lines; omitted, over budget) ===")
            continue
        body = numbered(text)
        shown_to = total
        if len(body) > remaining:
            cut = body[:remaining].rsplit("\n", 1)[0]
            shown_to = cut.count("\n") + 1 if cut else 0
            body = cut + f"\n... (truncated; lines {shown_to + 1}-{total} not shown)"
        blocks.append(f"=== {path} (lines 1-{shown_to} of {total}) ===\n{body}")
        used += len(body)
    return "\n\n".join(blocks)
