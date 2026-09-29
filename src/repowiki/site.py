"""Static HTML site for one or many repositories' wikis."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from .config import CONFIG_FILE, Settings, load_settings
from .git import GitError, Snapshot, commits_since, default_branch, git, has_commit, remote_web_url, toplevel
from .links import repo_path
from .policy import DEFAULT_POLICY, Policy, redact
from .render import Rendered, Target, render, slugify, split_href
from .safety import UnsafePathError, check_wiki_dir, read_wiki_file, wiki_path
from .validate import INDEX, check_index

ASSETS = Path(__file__).resolve().parent / "assets"
MARKER = ".repowiki-site"
MERMAID_CDN = "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs"
AMBER, RED = 0, 50
Log = Callable[[str], None]


class SiteError(RuntimeError):
    pass


@dataclass
class RepoEntry:
    name: str
    path: Path
    url: str | None = None  # web base, e.g. https://github.com/owner/name
    branch: str = "main"  # staleness is measured against this branch
    wiki_dir: str = "docs/wiki"
    ref: str | None = None  # read the wiki from this git ref instead of the working tree
    policy: Policy = DEFAULT_POLICY  # secret files are never linked or exported
    export_sources: bool = False  # copy cited files (plus LICENSE/NOTICE files) into the site

    def __post_init__(self) -> None:
        if self.url:
            self.url = safe_web_url(self.url)
        try:
            self.wiki_dir = check_wiki_dir(self.wiki_dir)
        except UnsafePathError as exc:
            raise SiteError(f"{self.name}: {exc}") from exc
        if self.ref is not None and (not isinstance(self.ref, str) or self.ref.startswith("-")):
            raise SiteError(f"{self.name}: bad ref {self.ref!r}")

    @property
    def slug(self) -> str:
        return slugify(self.name)


def safe_web_url(url: str) -> str:
    """A repository web URL: plain http(s), a host, no credentials, query, fragment or markup characters."""
    u = str(url).strip().rstrip("/")
    parts = urlsplit(u)
    if (
        parts.scheme.lower() not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or any(ord(c) < 33 or c in "\"'<>\\`" for c in u)
    ):
        raise SiteError(f"repository url must be a plain http(s) URL, got {url!r}")
    return u


def load_repos(config: Path) -> list[RepoEntry]:
    data = json.loads(config.read_text(encoding="utf-8"))
    items = data.get("repos") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise SiteError(f'{config}: expected a list of repos or {{"repos": [...]}}')
    entries = []
    for n, item in enumerate(items):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise SiteError(f'{config}: repos[{n}] needs at least a "path"')
        path = (config.parent / os.path.expanduser(item["path"])).resolve()
        settings = repo_settings(path)
        entries.append(
            RepoEntry(
                name=str(item.get("name") or path.name),
                path=path,
                url=(str(item["url"]) if item.get("url") else None),
                branch=str(item.get("branch") or "main"),
                wiki_dir=str(item.get("wiki_dir") or settings.wiki_dir),
                ref=item.get("ref") or None,
                policy=settings.policy,
                export_sources=bool(item.get("export_sources", False)),
            )
        )
    return _unique(entries)


def repo_settings(root: Path) -> Settings:
    """The repository's own .repowiki.toml (wiki_dir, exclude), so `site` sees what `generate` wrote."""
    try:
        return load_settings(root)
    except (ValueError, OSError) as exc:
        raise SiteError(f"{root}/{CONFIG_FILE}: {exc}") from exc


def entries_for_paths(
    paths: list[Path], wiki_dir: str | None = None, ref: str | None = None, export_sources: bool = False
) -> list[RepoEntry]:
    out = []
    for p in paths:
        root = toplevel(p)
        settings = repo_settings(root)
        url = remote_web_url(root)
        try:
            url = safe_web_url(url) if url else None
        except SiteError:
            url = None
        out.append(
            RepoEntry(
                root.name,
                root,
                url,
                wiki_dir=wiki_dir or settings.wiki_dir,
                ref=ref,
                policy=settings.policy,
                export_sources=export_sources,
            )
        )
    return _unique(out)


def _unique(entries: list[RepoEntry]) -> list[RepoEntry]:
    seen: set[str] = set()
    for e in entries:
        if e.slug in seen or e.slug in ("assets", "index"):
            raise SiteError(f"duplicate or reserved repo name: {e.name!r}")
        seen.add(e.slug)
    return entries


# --------------------------------------------------------------------------- collect


@dataclass
class Page:
    file: str
    title: str
    parent: str | None
    source: str
    rendered: Rendered | None = None
    children: list[Page] = field(default_factory=list)

    @property
    def html_name(self) -> str:
        return re.sub(r"\.md$", "", self.file) + ".html"


@dataclass
class RepoSite:
    entry: RepoEntry
    index: dict[str, Any] | None = None
    pages: list[Page] = field(default_factory=list)
    roots: list[Page] = field(default_factory=list)
    behind: int | None = None
    snap: Snapshot | None = None
    sources: set[str] = field(default_factory=set)  # repo files needing a local source page
    licenses: set[str] = field(default_factory=set)  # LICENSE/NOTICE files shipped with those sources
    error: str | None = None

    @property
    def commit(self) -> str:
        return str((self.index or {}).get("indexed_commit") or "")

    @property
    def home(self) -> Page | None:
        return self.pages[0] if self.pages else None


def _read(entry: RepoEntry, rel: str) -> str | None:
    if entry.ref:
        out = git(entry.path, "show", f"{entry.ref}:{entry.wiki_dir}/{rel}", check=False)
        return out or None
    try:
        return read_wiki_file(wiki_path(entry.path, entry.wiki_dir), rel)
    except UnsafePathError as exc:
        raise SiteError(f"{entry.name}: {exc}") from exc


def collect(entry: RepoEntry) -> RepoSite:
    site = RepoSite(entry)
    if not entry.path.is_dir():
        site.error = "repository not found"
        return site
    raw = _read(entry, INDEX)
    if raw is None:
        site.error = "wiki not written yet"
        return site
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SiteError(f"{entry.name}: {entry.wiki_dir}/{INDEX} is not valid JSON ({exc})") from exc
    probs, entries = check_index(data, wiki_dir=entry.wiki_dir, policy=entry.policy)
    if probs:
        raise SiteError(f"{entry.name}: " + "; ".join(str(p) for p in probs))
    site.index = data
    for e in entries:
        text = _read(entry, e.file)
        if text is None:
            raise SiteError(f"{entry.name}: {e.file} is listed in {INDEX} but missing")
        site.pages.append(Page(e.file, e.title, e.parent, redact(text)))
    by_file = {p.file: p for p in site.pages}
    for p in site.pages:
        parent = by_file.get(p.parent or "")
        (parent.children if parent and parent is not p else site.roots).append(p)
    if has_commit(entry.path, site.commit):
        branch = default_branch(entry.path, entry.branch)
        if branch:
            site.behind = commits_since(entry.path, site.commit, branch, exclude=entry.wiki_dir)
        try:
            site.snap = Snapshot.at(entry.path, site.commit)
        except GitError:
            site.snap = None
    known = {p.file for p in site.pages}
    for p in site.pages:
        p.rendered = render(p.source, _resolver(site, p.file, known))
    if site.snap is not None and site.sources:
        site.licenses = license_files(site.snap, site.sources, entry.policy)
    return site


LICENSE_RE = re.compile(r"^(licen[cs]e|copying|notice|copyright|authors|patents)([.-][^/]*)?$", re.I)


def license_files(snap: Snapshot, sources: set[str], policy: Policy = DEFAULT_POLICY) -> set[str]:
    """LICENSE/NOTICE/COPYING files at the root and in every directory above an exported file."""
    dirs = {""}
    for path in sources:
        parts = path.split("/")[:-1]
        dirs |= {"/".join(parts[:n]) for n in range(1, len(parts) + 1)}
    return {
        f
        for f in snap.files
        if posixpath.dirname(f) in dirs and LICENSE_RE.match(posixpath.basename(f)) and not policy.secret(f)
    }


def licenses_for(site: RepoSite, path: str) -> list[str]:
    """The licence files that apply to `path`: those in its directory and every parent, nearest first."""
    here = posixpath.dirname(path)
    chain = [here]
    while here:
        here = posixpath.dirname(here)
        chain.append(here)
    return [lic for d in chain for lic in sorted(site.licenses) if posixpath.dirname(lic) == d and lic != path]


def web_link(url: str, commit: str, path: str, lines: tuple[int, int] | None, is_dir: bool) -> str:
    kind = "tree" if is_dir else "blob"
    host = urlsplit(url).netloc.lower()
    q = quote(path)
    if "gitlab" in host:
        frag = f"#L{lines[0]}-{lines[1]}" if lines else ""
        return f"{url}/-/{kind}/{commit}/{q}{frag}"
    if lines:
        a, b = lines
        return f"{url}/{kind}/{commit}/{q}?plain=1#L{a}" + (f"-L{b}" if b != a else "")
    return f"{url}/{kind}/{commit}/{q}"


def _resolver(site: RepoSite, page_file: str, known: set[str]) -> Callable[[str], Target]:
    entry = site.entry
    prefix = entry.wiki_dir + "/"

    def resolve(href: str) -> Target:
        parts = urlsplit(href)
        if parts.scheme or parts.netloc or href.startswith("#") or not parts.path:
            return Target(href)
        path, frag, lines = split_href(href)
        target = repo_path(page_file, entry.wiki_dir, path)
        if target is None or entry.policy.secret(target):
            return Target(None)
        if target.startswith(prefix):
            inner = target[len(prefix) :]
            if inner in known:
                return Target(quote(inner[:-3] + ".html") + (f"#{quote(frag)}" if frag else ""))
            return Target(None)
        if entry.url:
            if site.snap is not None:
                is_dir = site.snap.has_dir(target) and not site.snap.has_file(target)
            else:
                is_dir = lines is None and not posixpath.splitext(target)[1]
            return Target(web_link(entry.url, site.commit, target, lines, is_dir), target, lines)
        if entry.export_sources and site.snap is not None and site.snap.has_file(target):
            site.sources.add(target)
            anchor = f"#L{lines[0]}-L{lines[1]}" if lines else ""
            return Target(f"_source/{quote(target)}.html{anchor}", target, lines)
        return Target(None)

    return resolve


# --------------------------------------------------------------------------- html


def badge(behind: int | None) -> str:
    if behind is None:
        return (
            '<span class="badge badge-unknown" title="indexed commit not found on the branch">staleness unknown</span>'
        )
    if behind == 0:
        return '<span class="badge badge-fresh">up to date</span>'
    cls = "badge-red" if behind > RED else "badge-amber"
    word = "commit" if behind == 1 else "commits"
    return f'<span class="badge {cls}" title="commits since the wiki was indexed">{behind} {word} behind</span>'


def _date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        return value


def _meta(site: RepoSite) -> str:
    data = site.index or {}
    sha = escape(site.commit[:7])
    commit = sha
    if site.entry.url and site.commit:
        commit = f'<a href="{escape(site.entry.url)}/commit/{escape(site.commit)}">{sha}</a>'
    return f"indexed {escape(_date(str(data.get('indexed_at', ''))))} at <code>{commit}</code>"


def _tree(pages: list[Page], current: Page | None, depth: int = 0) -> str:
    if depth > 8:
        return ""
    items = []
    for p in pages:
        cur = ' aria-current="page"' if p is current else ""
        sub = _tree(p.children, current, depth + 1) if p.children else ""
        items.append(f'<li><a href="{quote(p.html_name)}"{cur}>{escape(p.title)}</a>{sub}</li>')
    return f'<ul class="tree">{"".join(items)}</ul>'


def _mermaid_tag(mermaid: str, up: str) -> str:
    if re.match(r"^https?://", mermaid):
        if mermaid.endswith(".mjs"):
            return (
                '<script type="module">'
                f'import mermaid from "{escape(mermaid)}";'
                "const dark = matchMedia('(prefers-color-scheme: dark)').matches;"
                "mermaid.initialize({startOnLoad: true, securityLevel: 'strict', theme: dark ? 'dark' : 'default'});"
                "</script>"
            )
        return f'<script src="{escape(mermaid)}"></script><script src="{up}assets/mermaid-init.js"></script>'
    return f'<script src="{up}assets/mermaid.min.js"></script><script src="{up}assets/mermaid-init.js"></script>'


def layout(
    title: str, body: str, *, up: str, site_title: str, mermaid: str | None, nav: str = "", toc: str = ""
) -> str:
    scripts = _mermaid_tag(mermaid, up) if mermaid else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="repowiki">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{up}assets/style.css">
<script src="{up}assets/site.js" defer></script>
{scripts}
</head>
<body>
<header class="top"><button class="menu" type="button" aria-controls="nav" aria-expanded="false">Menu</button>
<a class="brand" href="{up}index.html">{escape(site_title)}</a></header>
<div class="layout{" no-nav" if not nav else ""}">
{nav}
<main class="content">
{body}
</main>
{toc}
</div>
</body>
</html>
"""


def page_html(site: RepoSite, page: Page, site_title: str, mermaid: str) -> str:
    r = page.rendered
    assert r is not None
    body = r.html if r.title else f"<h1>{escape(page.title)}</h1>\n{r.html}"
    nav = (
        '<nav id="nav" class="nav" aria-label="Pages">'
        f'<p class="repo-name">{escape(site.entry.name)}</p>'
        f'<p class="repo-meta">{_meta(site)}</p><p>{badge(site.behind)}</p>'
        f"{_tree(site.roots, page)}"
        f'<p class="all-repos"><a href="../index.html">All repositories</a></p></nav>'
    )
    toc = ""
    if r.headings:
        items = "".join(
            f'<li class="h{lvl}"><a href="#{escape(anchor)}">{escape(text)}</a></li>'
            for lvl, anchor, text in r.headings
        )
        toc = f'<aside class="toc" aria-label="On this page"><p>On this page</p><ul>{items}</ul></aside>'
    return layout(
        f"{page.title} - {site.entry.name}",
        f'<article class="wiki">{body}</article>',
        up="../",
        site_title=site_title,
        mermaid=mermaid if r.mermaid else None,
        nav=nav,
        toc=toc,
    )


def source_html(site: RepoSite, path: str, site_title: str) -> str:
    assert site.snap is not None and not site.entry.policy.secret(path)
    text = redact(site.snap.read(path) or "")
    up = "../" * (path.count("/") + 2)
    rows = "".join(
        f'<tr id="L{n}"><td class="ln"><a href="#L{n}">{n}</a></td><td class="code">{escape(line) or " "}</td></tr>'
        for n, line in enumerate(text.splitlines(), 1)
    )
    home = quote(site.home.html_name) if site.home else "index.html"
    base = "../" * (path.count("/") + 1)
    notices = [f'<a href="{base}_source/{quote(lic)}.html">{escape(lic)}</a>' for lic in licenses_for(site, path)]
    footer = (
        f'<p class="licence">Licence and notices: {", ".join(notices)}</p>'
        if notices
        else '<p class="licence">No LICENSE, NOTICE or COPYING file was found in this repository.</p>'
    )
    body = (
        f'<p class="crumbs"><a href="{up}{site.entry.slug}/{home}">'
        f"{escape(site.entry.name)}</a> / <code>{escape(path)}</code> at <code>{escape(site.commit[:7])}</code></p>"
        f'<div class="source"><table>{rows}</table></div>{footer}'
    )
    return layout(f"{path} - {site.entry.name}", body, up=up, site_title=site_title, mermaid=None)


def home_html(sites: list[RepoSite], site_title: str) -> str:
    cards = []
    for s in sites:
        name = escape(s.entry.name)
        if s.home is None:
            cards.append(f'<li class="card empty"><h2>{name}</h2><p>{escape(s.error or "no wiki")}</p></li>')
            continue
        summary = next((p.rendered.summary for p in s.pages[:1] if p.rendered), "")
        if len(summary) > 220:
            summary = summary[:219].rsplit(" ", 1)[0] + "..."
        n = len(s.pages)
        cards.append(
            f'<li class="card"><h2><a href="{s.entry.slug}/{quote(s.home.html_name)}">{name}</a></h2>'
            f"<p>{escape(summary)}</p>"
            f'<p class="card-meta">{n} page{"s" if n != 1 else ""} &middot; {_meta(s)} {badge(s.behind)}</p></li>'
        )
    built = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f'<h1>{escape(site_title)}</h1><ul class="cards">{"".join(cards)}</ul>'
        f'<p class="built">Built {built} by repowiki.</p>'
    )
    return layout(site_title, body, up="", site_title=site_title, mermaid=None)


# --------------------------------------------------------------------------- write


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_site(
    entries: list[RepoEntry],
    out: Path,
    *,
    site_title: str = "Repository wikis",
    mermaid: str = MERMAID_CDN,
    log: Log = print,
) -> list[RepoSite]:
    """Render everything into a temp dir, then swap it into `out` (never half-written)."""
    if out.exists() and (not out.is_dir() or (any(out.iterdir()) and not (out / MARKER).exists())):
        raise SiteError(f"refusing to overwrite {out}: not empty and not a repowiki site (no {MARKER})")
    local_mermaid: Path | None = None
    if not re.match(r"^https?://", mermaid):
        local_mermaid = Path(mermaid).expanduser()
        if not local_mermaid.is_file():
            raise SiteError(f"mermaid script not found: {local_mermaid}")
    sites = [collect(e) for e in entries]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{out.name}-", dir=out.parent))
    try:
        (tmp / MARKER).write_text("generated by repowiki; safe to delete\n", encoding="utf-8")
        shutil.copytree(ASSETS, tmp / "assets")
        if local_mermaid:
            shutil.copy(local_mermaid, tmp / "assets" / "mermaid.min.js")
        for s in sites:
            if s.home is None:
                log(f"  {s.entry.name}: {s.error}")
                continue
            base = tmp / s.entry.slug
            for p in s.pages:
                _write(base / p.html_name, page_html(s, p, site_title, mermaid))
            _write(base / "index.html", _redirect(quote(s.home.html_name)))
            for path in sorted(s.sources | s.licenses):
                _write(base / "_source" / f"{path}.html", source_html(s, path, site_title))
            if s.sources and not s.licenses:
                log(f"  {s.entry.name}: warning: exported source files but found no LICENSE/NOTICE/COPYING file")
            behind = "?" if s.behind is None else s.behind
            log(f"  {s.entry.name}: {len(s.pages)} pages, {behind} commit(s) behind")
        _write(tmp / "index.html", home_html(sites, site_title))
        _swap(tmp, out)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sites


def _swap(new: Path, out: Path) -> None:
    """Move `new` into place at `out`; the previous site is only deleted once the new one is there."""
    if not out.exists():
        new.rename(out)
        return
    old = Path(tempfile.mkdtemp(prefix=f".{out.name}-old-", dir=out.parent))
    old.rmdir()
    out.rename(old)
    try:
        new.rename(out)
    except BaseException:
        old.rename(out)
        raise
    shutil.rmtree(old, ignore_errors=True)


def _redirect(target: str) -> str:
    return (
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0; url={target}">'
        f'<title>Redirecting</title><a href="{target}">Continue</a>\n'
    )
