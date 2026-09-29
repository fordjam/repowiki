"""Command line: repowiki generate | update | validate | site | backends."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .config import Settings, load_settings
from .generate import GenerateError, generate
from .git import GitError, toplevel
from .runners import BACKENDS, RunnerError
from .site import MERMAID_CDN, SiteError, build_site, entries_for_paths, load_repos
from .update import update
from .validate import Problem, validate


def _err(msg: str) -> None:
    print(f"repowiki: error: {msg}", file=sys.stderr)


def _model_flags(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("models (spec = backend[:model], e.g. claude:haiku, opencode:provider/model)")
    g.add_argument("--model", help="one model for every step")
    g.add_argument("--cheap", help="model for planning, drafting and repairs")
    g.add_argument("--strong", help="model for the final page pass")
    g.add_argument("--timeout", type=float, help="seconds per model call (default 900)")
    g.add_argument("--jobs", type=int, help="pages written in parallel (default 1)")


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", nargs="?", default=".", help="path inside the git repository (default: .)")
    p.add_argument("--wiki-dir", help="wiki directory inside the repo (default docs/wiki)")
    p.add_argument("--config", type=Path, help="settings file (default <repo>/.repowiki.toml)")


def _settings(args: argparse.Namespace, root: Path) -> Settings:
    overrides: dict[str, Any] = {"wiki_dir": getattr(args, "wiki_dir", None)}
    model = getattr(args, "model", None)
    overrides["cheap"] = getattr(args, "cheap", None) or model
    overrides["strong"] = getattr(args, "strong", None) or model
    for key in ("timeout", "jobs", "max_pages", "min_pages"):
        overrides[key] = getattr(args, key, None)
    overrides["repo"] = getattr(args, "name", None)
    return load_settings(root, overrides, args.config)


def _report(problems: list[Problem], warnings: list[str] | None = None) -> int:
    for w in warnings or []:
        print(f"  warning: {w}")
    for p in problems:
        print(f"  {p}")
    errors = [p for p in problems if p.level == "error"]
    if errors:
        print(f"validation failed: {len(errors)} error(s)")
        return 1
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    settings = _settings(args, root)
    result = generate(root, settings, plan_only=args.plan_only, ref=args.ref)
    if args.plan_only:
        return 0
    code = _report(result.problems, result.warnings)
    if code == 0:
        print(f"wrote {len(result.written)} pages to {settings.wiki_dir}/ (indexed {result.commit[:12]})")
    return code


def cmd_update(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    settings = _settings(args, root)
    result = update(root, settings, dry_run=args.dry_run, ref=args.ref)
    if args.dry_run:
        return 0
    code = _report(result.problems, result.warnings)
    if code == 0:
        print(f"re-wrote {len(result.written)} page(s); indexed {result.plan.new_commit[:12]}")
    return code


def cmd_validate(args: argparse.Namespace) -> int:
    root = toplevel(args.repo)
    settings = _settings(args, root)
    problems = validate(root, settings.wiki_dir, policy=settings.policy)
    code = _report(problems)
    if code == 0:
        print(f"{settings.wiki_dir}: ok")
    return code


def cmd_site(args: argparse.Namespace) -> int:
    entries = load_repos(args.repos) if args.repos else []
    if args.paths or not entries:
        entries += entries_for_paths([Path(p) for p in (args.paths or ["."])], args.wiki_dir or "docs/wiki", args.ref)
    out = Path(args.out)
    print(f"building site for {len(entries)} repo(s) into {out}/")
    sites = build_site(entries, out, site_title=args.title, mermaid=args.mermaid)
    pages = sum(len(s.pages) for s in sites)
    print(f"done: {pages} pages; open {out / 'index.html'}")
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    for name, cls in BACKENDS.items():
        exe = cls.default_binary
        where = shutil.which(exe)
        print(f"  {name:<9} {'found at ' + where if where else 'not found'}")
    print("  fake      built in (offline, placeholder prose)")
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="repowiki", description=__doc__)
    ap.add_argument("--version", action="version", version=f"repowiki {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="plan and write the whole wiki")
    _common(g)
    _model_flags(g)
    g.add_argument("--name", help="repo name for index.json (default: owner/name from origin, else folder name)")
    g.add_argument("--max-pages", type=int, dest="max_pages")
    g.add_argument("--min-pages", type=int, dest="min_pages")
    g.add_argument("--ref", default="HEAD", help="commit to document (default HEAD)")
    g.add_argument("--plan-only", action="store_true", help="print the outline and stop")
    g.set_defaults(fn=cmd_generate)

    u = sub.add_parser("update", help="re-write only pages whose cited files changed")
    _common(u)
    _model_flags(u)
    u.add_argument("--ref", default="HEAD", help="commit to move the wiki to (default HEAD)")
    u.add_argument("--dry-run", action="store_true", help="list stale pages and stop")
    u.set_defaults(fn=cmd_update)

    v = sub.add_parser("validate", help="check links, citations and index.json")
    _common(v)
    v.set_defaults(fn=cmd_validate)

    s = sub.add_parser("site", help="render a static HTML site for one or many repos")
    s.add_argument("paths", nargs="*", help="repositories to include (default: . unless --repos is given)")
    s.add_argument("--repos", type=Path, help="repos.json listing repositories")
    s.add_argument("-o", "--out", default="site", help="output directory (default ./site)")
    s.add_argument("--title", default="Repository wikis", help="site title")
    s.add_argument("--wiki-dir", help="wiki directory for repos given as paths")
    s.add_argument("--ref", help="read wikis from this git ref instead of the working tree")
    s.add_argument("--mermaid", default=MERMAID_CDN, help="mermaid script URL or local file to copy in")
    s.set_defaults(fn=cmd_site)

    b = sub.add_parser("backends", help="show which model CLIs are installed")
    b.set_defaults(fn=cmd_backends)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except (GenerateError, GitError, RunnerError, SiteError, ValueError, FileNotFoundError) as exc:
        _err(str(exc))
        return 2
    except KeyboardInterrupt:
        _err("interrupted")
        return 130
