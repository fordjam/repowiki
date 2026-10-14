"""Static HTML site for one or many repositories' wikis."""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from . import site_publish
from .config import CONFIG_FILE, Settings, load_settings
from .git import GitError, Snapshot, commits_since, default_branch, remote_web_url, resolve_commit, toplevel
from .links import RepoIdentity, identities, resolve_href
from .policy import DEFAULT_POLICY, Policy, redact
from .render import Rendered, Target, diagram_sources, render, slugify
from .safety import UnsafePathError, check_wiki_dir, read_wiki_file, wiki_path
from .site_publish import PublicationError
from .snapshot import FrozenWiki, read_frozen
from .validate import MAX_IMAGE_BYTES, diagram_problem, image_content_defect, repo_identities

ASSETS = Path(__file__).resolve().parent / "assets"
MARKER = ".repowiki-site"
# The pinned Mermaid artifact ships with the package and every build verifies it against the digest
# recorded beside it before it is used. The retired CDN constant keeps its name only because the CLI
# imports it as --mermaid's default; since the offline resource policy it has pointed at nothing
# remote, and since the vendored artifact it is the vendor copy itself.
VENDORED_MERMAID = ASSETS / "vendor" / "mermaid.min.js"
MERMAID_MANIFEST = ASSETS / "vendor" / "manifest.json"
MERMAID_CDN = str(VENDORED_MERMAID)
# Every generated page carries this policy, so the reader's browser itself refuses anything not served by
# the site (an image, script, style, font, frame or connection elsewhere), whatever a renderer emits.
# Inline styles stay allowed: diagram renderers style their SVG inline, and an inline style still
# cannot load anything from elsewhere under these sources.
OFFLINE_CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; form-action 'self'"
)
_CSP_META = f'<meta http-equiv="Content-Security-Policy" content="{OFFLINE_CSP}">'
AMBER, RED = 0, 50
Log = Callable[[str], None]


class SiteError(RuntimeError):
    pass


def _pinned_mermaid() -> str:
    """The recorded digest of the vendored Mermaid artifact, verified byte for byte (raises SiteError
    when the manifest or the artifact is missing, or the bytes are not the pinned ones)."""
    try:
        manifest = json.loads(MERMAID_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SiteError(f"the vendored mermaid manifest is missing or unreadable: {exc}") from exc
    recorded = manifest.get("sha256")
    if not isinstance(recorded, str) or not re.fullmatch(r"[0-9a-f]{64}", recorded):
        raise SiteError("the vendored mermaid manifest does not record a sha256 digest")
    try:
        data = VENDORED_MERMAID.read_bytes()
    except OSError as exc:
        raise SiteError(f"the vendored mermaid script is missing: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != recorded:
        raise SiteError(
            f"the vendored mermaid script does not match its pinned digest ({digest[:12]} != {recorded[:12]}); "
            "nothing was built"
        )
    return digest


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
    aliases: tuple[str, ...] = ()  # other registered web URLs of the same repository
    min_citations: int = 1  # the repository's own validation minimum (.repowiki.toml)
    policy_commit: str | None = None  # an explicitly selected policy commit (else REPOWIKI_POLICY_COMMIT)

    def __post_init__(self) -> None:
        if self.url:
            self.url = safe_web_url(self.url)
        if not isinstance(self.aliases, (list, tuple)) or not all(isinstance(a, str) for a in self.aliases):
            raise SiteError(f"{self.name}: aliases must be a list of repository web URLs")
        self.aliases = tuple(safe_web_url(a) for a in self.aliases)
        try:
            self.wiki_dir = check_wiki_dir(self.wiki_dir)
        except UnsafePathError as exc:
            raise SiteError(f"{self.name}: {exc}") from exc
        if self.ref is not None and (not isinstance(self.ref, str) or self.ref.startswith("-")):
            raise SiteError(f"{self.name}: bad ref {self.ref!r}")

    @property
    def slug(self) -> str:
        return slugify(self.name)

    @property
    def identities(self) -> tuple[RepoIdentity, ...]:
        """Registered web identities: web URLs into these are source links, pinned to the indexed commit."""
        return identities([self.url, *self.aliases])


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


def same_origin_path(value: str) -> bool:
    """A same-origin path: root-relative, with no scheme, host, backslashes, traversal or control
    characters — the only kind of hook URL a page may hold."""
    if not value.startswith("/") or value.startswith("//"):
        return False
    if any(ord(c) < 32 or c in "\"'<>\\`" for c in value):
        return False
    parts = urlsplit(value)
    return not (parts.scheme or parts.netloc) and ".." not in value.split("/")


@dataclass
class PortalHooks:
    """Narrow hooks for an embedding portal: branding text (escaped when rendered), a POST logout
    action and the token endpoint the page fetches its CSRF token from at click time. Tokens are
    never baked into the HTML, and both URLs must be same-origin paths."""

    branding: str = ""
    logout_action: str | None = None
    token_endpoint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.branding, str):
            raise SiteError("portal branding must be a string")
        for name in ("logout_action", "token_endpoint"):
            value = getattr(self, name)
            if value is not None and not same_origin_path(value):
                raise SiteError(f"portal {name} must be a same-origin path, got {value!r}")


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
                aliases=tuple(item.get("aliases") or ()),
                min_citations=settings.min_citations,
                policy_commit=item.get("policy_commit") or None,
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
                min_citations=settings.min_citations,
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
    assets: set[str] = field(default_factory=set)  # repo images the pages embed, copied into the site
    policy: Policy = DEFAULT_POLICY  # the policy the wiki was validated with: links and exports obey it
    error: str | None = None

    @property
    def commit(self) -> str:
        return str((self.index or {}).get("indexed_commit") or "")

    @property
    def home(self) -> Page | None:
        return self.pages[0] if self.pages else None


def _read(entry: RepoEntry, rel: str) -> str | None:
    """One working-tree wiki file (the frozen reader calls this under the wiki lock)."""
    return read_wiki_file(wiki_path(entry.path, entry.wiki_dir), rel)


def _selected_policy(root: Path) -> str | None:
    """REPOWIKI_POLICY_COMMIT names one repository's commit: it applies to the repositories that have it
    (a multi-repository site may include others, which use their own commit or working-tree policy)."""
    selected = os.environ.get("REPOWIKI_POLICY_COMMIT", "").strip()
    if not selected or selected.startswith("-"):
        return None
    try:
        return resolve_commit(root, selected) or None
    except GitError:
        return None


def require_policy_selection(entries: list[RepoEntry]) -> None:
    """A set REPOWIKI_POLICY_COMMIT must select a policy: fail closed, never fall back to another one.

    It is refused when it is not a commit name, or when it resolves in none of the repositories it can
    apply to (those without their own ``policy_commit``), e.g. a clone that lacks the reviewed commit."""
    selected = os.environ.get("REPOWIKI_POLICY_COMMIT", "").strip()
    if not selected:
        return
    if selected.startswith("-"):
        raise SiteError(f"REPOWIKI_POLICY_COMMIT {selected!r} is not a commit; nothing was built")
    candidates = [e for e in entries if not e.policy_commit and e.path.is_dir()]
    if candidates and not any(_selected_policy(e.path) for e in candidates):
        raise SiteError(
            f"REPOWIKI_POLICY_COMMIT {selected!r} does not resolve in any repository of this site; "
            "refusing to fall back to another policy, nothing was built"
        )


def freeze(entry: RepoEntry) -> FrozenWiki:
    """The entry's wiki, read once through the frozen validation reader (``snapshot.read_frozen``) and
    fully validated. A missing repository or index, or any validation error, raises ``SiteError``
    (validated mode is the only mode). Committed reads (``ref``) never recover the working tree; the
    policy comes from the selected policy commit (``policy_commit`` or ``REPOWIKI_POLICY_COMMIT``), else
    from the wiki commit for a committed read, else from the working tree."""
    if not entry.path.is_dir():
        raise SiteError(f"{entry.name}: repository not found at {entry.path}")
    try:
        frozen = read_frozen(
            entry.path, entry.wiki_dir, ref=entry.ref, policy=entry.policy, min_citations=entry.min_citations,
            identities=repo_identities(entry.path, (*([entry.url] if entry.url else []), *entry.aliases)),
            policy_commit=entry.policy_commit or _selected_policy(entry.path),
            reader=lambda rel: _read(entry, rel),
        )  # fmt: skip
    except (UnsafePathError, GitError) as exc:
        raise SiteError(f"{entry.name}: {exc}") from exc
    errors = frozen.errors
    if errors or frozen.index is None:
        where = f" at {entry.ref}" if entry.ref else ""
        more = f" (and {len(errors) - 10} more)" if len(errors) > 10 else ""
        detail = "; ".join(str(p) for p in errors[:10]) + more
        raise SiteError(f"{entry.name}: the wiki{where} failed validation, nothing was built: {detail}")
    return frozen


def collect(entry: RepoEntry) -> RepoSite:
    """Freeze and validate the entry's wiki, then render exactly those captured texts."""
    require_policy_selection([entry])
    return _collect(entry, freeze(entry))


def _collect(entry: RepoEntry, frozen: FrozenWiki) -> RepoSite:
    site = RepoSite(entry, index=frozen.index, snap=frozen.snap, policy=frozen.policy)
    for e in frozen.entries:
        site.pages.append(Page(e.file, e.title, e.parent, redact(frozen.texts[e.file])))
    by_file = {p.file: p for p in site.pages}
    for p in site.pages:
        parent = by_file.get(p.parent or "")
        (parent.children if parent and parent is not p else site.roots).append(p)
    branch = default_branch(entry.path, entry.branch)
    if branch:
        site.behind = commits_since(entry.path, site.commit, branch, exclude=entry.wiki_dir)
    known = {p.file for p in site.pages}
    for p in site.pages:
        for line, diagram in diagram_sources(p.source):
            why = diagram_problem(diagram)
            if why:  # validation refuses these already; a page that slipped past it still never ships
                raise SiteError(f"{entry.name}: {p.file}:{line}: {why}; nothing was built")
        p.rendered = render(p.source, _resolver(site, p.file, known), _image_resolver(site, p.file))
    if site.snap is not None and site.sources:
        site.licenses = license_files(site.snap, site.sources, site.policy)
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
        commit = site.commit or None
        res = resolve_href(href, page_file, entry.wiki_dir, repo_identities=entry.identities, commit=commit)
        if res.kind in ("external", "anchor"):
            return Target(href)  # other sites and repositories: plain links, never citation chips
        if res.kind == "invalid":
            return Target(None)  # rendered as text: an unpinned or malformed source link is not evidence
        if res.kind == "wiki":
            inner = res.inner or ""
            if inner in known and not site.policy.secret(prefix + inner):
                frag = unquote(urlsplit(href).fragment)
                return Target(quote(inner[:-3] + ".html") + (f"#{quote(frag)}" if frag else ""))
            return Target(None)
        assert res.target is not None
        target, lines = res.target.path, res.target.lines
        if site.policy.secret(target):
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


def _image_resolver(site: RepoSite, page_file: str) -> Callable[[str], Target]:
    """Resolves a page's Markdown image sources to the site asset the image is copied to (or None:
    validation has already refused anything that is not an approved local image)."""
    prefix = site.entry.wiki_dir + "/"

    def resolve_image(src: str) -> Target:
        res = resolve_href(src, page_file, site.entry.wiki_dir)
        if res.kind == "wiki":
            target = prefix + (res.inner or "")
        elif res.kind == "source" and res.target and not res.target.lines:
            target = res.target.path
        else:
            return Target(None)
        if site.policy.secret(target) or site.snap is None or not site.snap.has_file(target):
            return Target(None)
        site.assets.add(target)
        return Target("_assets/" + quote(target))

    return resolve_image


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


def _mermaid_tag(up: str, mermaid: str | Path) -> str:
    """The local diagram script and its external initializer (`build_site` refuses remote ones)."""
    try:
        pinned = Path(mermaid).resolve() == VENDORED_MERMAID.resolve()
    except OSError:
        pinned = False
    src = "assets/vendor/mermaid.min.js" if pinned else "assets/mermaid.min.js"
    return f'<script src="{up}{src}"></script><script src="{up}assets/mermaid-init.js"></script>'


SEARCH_ENTRY_LIMIT = 400
SEARCH_JSON_BYTES = 64_000


def _search_json(sites: list[RepoSite], up: str) -> str:
    """The bounded, repo-scoped page index the reader's browser searches: titles and root-relative
    urls of the site's own pages, capped by count and size, and safe inside a script element."""
    entries: list[dict[str, str]] = []
    size = 0
    truncated = False
    for s in sites:
        if s.home is None:
            continue
        for p in s.pages:
            if len(entries) >= SEARCH_ENTRY_LIMIT:
                truncated = True
                break
            entry = {
                "r": s.entry.slug,
                "t": p.title,
                "u": f"{up}{quote(s.entry.slug)}/{quote(p.html_name)}",
            }
            size += len(json.dumps(entry))
            if size > SEARCH_JSON_BYTES:
                truncated = True
                break
            entries.append(entry)
        if truncated:
            break
    data = json.dumps({"pages": entries, "more": truncated}, ensure_ascii=True)
    # It travels as the text of a <template>, which the HTML parser reads as markup: no tag may start
    # and no character reference may be decoded inside it (JSON reads both escapes back verbatim).
    return data.replace("<", "\\u003c").replace("&", "\\u0026")


def _repos_nav(sites: list[RepoSite], current: RepoSite | None, up: str) -> str:
    """Every repository's entry page, current one marked — identical titles stay repo-scoped because
    each entry carries its own repository slug."""
    items = ""
    for s in sites:
        if s.home is None:
            continue
        here = ' aria-current="page"' if s is current else ""
        items += f'<li><a href="{up}{quote(s.entry.slug)}/index.html"{here}>{escape(s.entry.name)}</a></li>'
    return f'<p class="repos-label">Repositories</p><ul class="repos">{items}</ul>' if items else ""


def _logout_button(portal: PortalHooks | None) -> str:
    """The structured logout control: same-origin URLs only, no tokens in the markup."""
    if portal is None or not portal.logout_action:
        return ""
    token = f' data-token-endpoint="{escape(portal.token_endpoint)}"' if portal.token_endpoint else ""
    return (
        f'<button type="button" class="portal-logout" data-action="{escape(portal.logout_action)}"{token}>'
        'Log out</button><span class="portal-logout-status" role="alert" hidden></span>'
    )


def _branding(portal: PortalHooks | None) -> str:
    return f'<span class="branding">{escape(portal.branding)}</span>' if portal and portal.branding else ""


def layout(
    title: str,
    body: str,
    *,
    up: str,
    site_title: str,
    mermaid: str | None,
    nav: str = "",
    toc: str = "",
    portal: PortalHooks | None = None,
    search: str = "",
) -> str:
    scripts = _mermaid_tag(up, mermaid) if mermaid else ""
    header_extras = _branding(portal) + _logout_button(portal)
    search_box = (
        '<input type="search" class="portal-search" placeholder="Search pages" aria-label="Search pages">'
        '<div class="search-results" hidden></div>'
    )
    search_data = f'<template id="repowiki-search">{search}</template>' if search else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
{_CSP_META}
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="repowiki">
<title>{escape(title)}</title>
<link rel="stylesheet" href="{up}assets/style.css">
<script src="{up}assets/site.js" defer></script>
{scripts}
</head>
<body>
<header class="top"><button class="menu" type="button" aria-controls="nav" aria-expanded="false">Menu</button>
<a class="brand" href="{up}index.html">{escape(site_title)}</a>{header_extras}{search_box}</header>
<div class="layout{" no-nav" if not nav else ""}">
{nav}
<main class="content">
{body}
</main>
{toc}
</div>
{search_data}
</body>
</html>
"""


def page_html(
    site: RepoSite,
    page: Page,
    site_title: str,
    mermaid: str | Path | None,
    sites: list[RepoSite] | None = None,
    portal: PortalHooks | None = None,
) -> str:
    r = page.rendered
    assert r is not None
    body = r.html if r.title else f"<h1>{escape(page.title)}</h1>\n{r.html}"
    every = sites if sites is not None else [site]
    nav = (
        '<nav id="nav" class="nav" aria-label="Pages">'
        f'<p class="repo-name">{escape(site.entry.name)}</p>'
        f'<p class="repo-meta">{_meta(site)}</p><p>{badge(site.behind)}</p>'
        f"{_repos_nav(every, site, '../')}"
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
        portal=portal,
        search=_search_json(every, "../"),
    )


def source_html(site: RepoSite, path: str, site_title: str) -> str:
    assert site.snap is not None and not site.policy.secret(path)
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


def home_html(sites: list[RepoSite], site_title: str, portal: PortalHooks | None = None, *, now: datetime) -> str:
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
    built = now.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f'<h1>{escape(site_title)}</h1><ul class="cards">{"".join(cards)}</ul>'
        f'<p class="built">Built {built} by repowiki.</p>'
    )
    return layout(
        site_title, body, up="", site_title=site_title, mermaid=None, portal=portal, search=_search_json(sites, "")
    )


# --------------------------------------------------------------------------- write


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def blob_bytes(root: Path, commit: str, path: str) -> bytes:
    """The exact bytes `path` has at `commit`, capped just past the image limit (empty when git
    cannot provide them: the copy then fails loudly instead of shipping a truncated file)."""
    proc = subprocess.Popen(
        ["git", "-C", str(root), "cat-file", "blob", f"{commit}:{path}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert proc.stdout is not None
    with proc:
        return proc.stdout.read(MAX_IMAGE_BYTES + 1)


def _legacy_output(out: Path) -> bool:
    """A pre-releases site: a real directory carrying the repowiki marker (never a managed pointer)."""
    return out.exists() and not out.is_symlink() and out.is_dir() and (out / MARKER).is_file()


def build_site(
    entries: list[RepoEntry],
    out: Path,
    *,
    site_title: str = "Repository wikis",
    mermaid: str | Path | None = MERMAID_CDN,
    portal: PortalHooks | None = None,
    migrate: bool = False,
    now: datetime,
    log: Log = print,
) -> list[RepoSite]:
    """Render everything into a staging directory, then publish it as an immutable release: the
    output becomes a symlink into the site's owned store, and one atomic, fsynced pointer swap makes
    the new release live (the last good release is served until then and after any earlier failure).
    A legacy site directory needs an explicit one-time `migrate`. The offline resource policy holds
    for every build: nothing on a page is fetched from the network. `mermaid` is the pinned vendored
    artifact by default (verified against its recorded digest before use) or a local script file
    copied into the site; remote scripts are refused. Every embedded image must be an approved local
    raster file, checked for type and size as it is copied. `portal` adds the narrow portal hooks
    (branding, logout, theme); without one the site is standalone. `now` is the build's one instant (the
    "Built" stamp, release manifests and retention); the caller reads the clock, this never does."""
    if now.tzinfo is None:
        raise SiteError("build_site needs a timezone-aware `now`")
    if mermaid and re.match(r"^https?://", str(mermaid)):
        raise SiteError(f"offline site policy: mermaid must be a local script file, not {mermaid}")
    if out.exists() and (not out.is_dir() or (any(out.iterdir()) and not (out / MARKER).exists())):
        raise SiteError(f"refusing to overwrite {out}: not empty and not a repowiki site (no {MARKER})")
    legacy = _legacy_output(out)
    if legacy and not migrate:
        raise SiteError(
            f"{out} is a legacy site directory: repowiki publishes immutable releases now and needs "
            "an explicit one-time migration (--migrate)"
        )
    local_mermaid: Path | None = None
    if mermaid:
        candidate = Path(mermaid).expanduser()
        if not candidate.is_file():
            raise SiteError(f"mermaid script not found: {candidate}")
        try:
            pinned = candidate.resolve() == VENDORED_MERMAID.resolve()
        except OSError:
            pinned = False
        if pinned:
            _pinned_mermaid()  # the site ships the vendor copy: it must be the pinned bytes
        else:
            local_mermaid = candidate
    _unique(entries)  # duplicate or reserved slugs fail before anything is read or written
    require_policy_selection(entries)  # an unresolved policy selection fails closed, before any read
    frozen = [freeze(e) for e in entries]  # every wiki read and fully validated before any is rendered
    sites = [_collect(e, f) for e, f in zip(entries, frozen, strict=True)]
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
                _write(base / p.html_name, page_html(s, p, site_title, mermaid, sites, portal))
            _write(base / "index.html", _redirect(quote(s.home.html_name)))
            for path in sorted(s.sources | s.licenses):
                _write(base / "_source" / f"{path}.html", source_html(s, path, site_title))
            for target in sorted(s.assets):
                data = blob_bytes(s.entry.path, s.commit, target)
                suffix = posixpath.splitext(target)[1]
                defect = image_content_defect(data, suffix) if data else "it is missing at the indexed commit"
                if defect:
                    raise SiteError(
                        f"{s.entry.name}: {target}: {defect}; nothing was published and the previous site is intact"
                    )
                dest = base / "_assets" / Path(*target.split("/"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            if s.sources and not s.licenses:
                log(f"  {s.entry.name}: warning: exported source files but found no LICENSE/NOTICE/COPYING file")
            behind = "?" if s.behind is None else s.behind
            log(f"  {s.entry.name}: {len(s.pages)} pages, {behind} commit(s) behind")
        _write(tmp / "index.html", home_html(sites, site_title, portal, now=now))
        try:
            if legacy:
                site_publish.migrate_legacy(out, now=now, marker=MARKER)
            site_publish.publish(tmp, out, now=now, marker=MARKER)
        except PublicationError as exc:
            raise SiteError(str(exc)) from exc
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return sites


def _redirect(target: str) -> str:
    return (
        f'<!doctype html><meta charset="utf-8">{_CSP_META}<meta http-equiv="refresh" content="0; url={target}">'
        f'<title>Redirecting</title><a href="{target}">Continue</a>\n'
    )
