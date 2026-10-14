"""Finding links and citations in wiki Markdown.

A citation is an ordinary relative Markdown link whose fragment is a line
range: ``[cli.py](../../src/tool/cli.py#L10-L42)``. Links are read with the
same parser the site renderer uses, so code blocks never produce false hits.

Every link is resolved to one canonical meaning (``resolve_href``) shared by
validation, rendering, generation and update. A web URL into a registered
repository identity (``RepoIdentity``) is a source link too: it is accepted
only when it pins the full indexed commit, and then it names exactly the same
``SourceTarget`` as the equivalent relative link. A web URL into any other
repository is an ordinary external link and is never evidence. Raw HTML
anchors and images are found as well; they are escaped when rendered, so one
pointing at source is reported with a diagnostic asking for a Markdown link.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import SplitResult, quote, unquote, urlsplit

from markdown_it import MarkdownIt

LINES_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")
GITLAB_LINES_RE = re.compile(r"^L(\d+)(?:-(\d+))?$")
FULL_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ENCODED_PATH_RE = re.compile(r"%(?:2e|2f|5c)", re.I)  # encoded '.', '/', '\\': never allowed in a source path
SOURCE_KINDS = ("blob", "tree", "raw", "blame")

_PARSER = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])


@dataclass(frozen=True)
class Link:
    href: str
    line: int  # 1-based line in the page where the enclosing block starts
    image: bool = False
    html: bool = False  # a raw HTML <a href> or <img src>: rendered escaped, never evidence

    @property
    def external(self) -> bool:
        parts = urlsplit(self.href)
        return bool(parts.scheme or parts.netloc) or self.href.startswith("//")

    @property
    def path(self) -> str:
        return unquote(urlsplit(self.href).path)

    @property
    def fragment(self) -> str:
        return unquote(urlsplit(self.href).fragment)

    @property
    def lines(self) -> tuple[int, int] | None:
        m = LINES_RE.match(self.fragment)
        if not m:
            return None
        a = int(m.group(1))
        return a, int(m.group(2) or a)


class _HTMLLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, bool]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        want = {"a": "href", "img": "src"}.get(tag)
        for name, value in attrs:
            if want and name == want and value is not None:
                self.found.append((value.strip(), tag == "img"))


def html_links(raw: str) -> list[tuple[str, bool]]:
    """(href, is_image) of every <a href> and <img src> in a raw HTML fragment."""
    parser = _HTMLLinks()
    parser.feed(raw)
    parser.close()
    return parser.found


def find_links(markdown: str) -> list[Link]:
    """Markdown links and images, plus raw HTML anchors and images outside code (``html=True``)."""
    found: list[Link] = []
    for block in _PARSER.parse(markdown):
        line = (block.map[0] + 1) if block.map else 0
        if block.type == "html_block":
            found += [Link(href, line, image, html=True) for href, image in html_links(block.content)]
            continue
        if block.type != "inline" or not block.children:
            continue
        for tok in block.children:
            if tok.type == "link_open":
                found.append(Link(str(tok.attrGet("href") or ""), line))
            elif tok.type == "image":
                found.append(Link(str(tok.attrGet("src") or ""), line, image=True))
            elif tok.type == "html_inline":
                found += [Link(href, line, image, html=True) for href, image in html_links(tok.content)]
    return found


# --------------------------------------------------------------------------- canonical targets


@dataclass(frozen=True)
class RepoIdentity:
    """A registered web identity of the repository: host and project path (``owner/name`` or nested
    ``group/sub/name``). Only registered identities and aliases make a web URL a source link; nothing
    is looked up over the network and redirects are not followed."""

    host: str
    path: str

    @classmethod
    def parse(cls, url: str) -> RepoIdentity:
        parts = urlsplit(str(url).strip())
        segments = [s for s in parts.path.split("/") if s]
        if segments and segments[-1].lower().endswith(".git"):
            segments[-1] = segments[-1][:-4]
        if (
            parts.scheme.lower() not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not segments
            or ENCODED_PATH_RE.search(parts.path)
            or any(s in (".", "..") or "%" in s for s in segments)
        ):
            raise ValueError(f"not a repository web URL: {url!r}")
        return cls(_host(parts.hostname, parts.port, parts.scheme), "/".join(segments))

    @property
    def gitlab(self) -> bool:
        """GitLab-style URLs (``/-/blob/<sha>/path#L1-42``); otherwise GitHub-style (``/blob/<sha>/path#L1-L42``)."""
        return "gitlab" in self.host

    def matches(self, host: str, segments: list[str]) -> bool:
        mine = self.path.split("/")
        return host == self.host and [s.casefold() for s in segments[: len(mine)]] == [s.casefold() for s in mine]


def identities(urls: Iterable[str | None]) -> tuple[RepoIdentity, ...]:
    """Registered identities from web URLs; unusable values are skipped, duplicates dropped."""
    out: list[RepoIdentity] = []
    for url in urls:
        try:
            ident = RepoIdentity.parse(url) if url else None
        except ValueError:
            ident = None
        if ident and ident not in out:
            out.append(ident)
    return tuple(out)


def _host(hostname: str, port: int | None, scheme: str) -> str:
    default = {"http": 80, "https": 443}.get(scheme.lower())
    return hostname.lower() + (f":{port}" if port and port != default else "")


@dataclass(frozen=True)
class SourceTarget:
    """A canonical source reference: a repository path at the indexed commit, optionally a line range."""

    path: str
    lines: tuple[int, int] | None = None

    def href(self, page_file: str, wiki_dir: str) -> str:
        """The canonical relative Markdown href for this target from a wiki page (percent-encoded, so any
        file name stays one parsable destination and resolves back to this same target)."""
        frag = ""
        if self.lines:
            a, b = self.lines
            frag = f"#L{a}" if a == b else f"#L{a}-L{b}"
        return quote(relative_from_page(page_file, wiki_dir, self.path), safe="/") + frag


@dataclass(frozen=True)
class Resolved:
    """What one href means. ``kind``: ``source`` (``target`` set), ``wiki`` (``inner`` page file),
    ``external`` (another site or repository: never evidence), ``anchor`` (same page) or ``invalid``
    (``problem`` says why). ``source_shaped`` marks hrefs that point, or try to point, at source."""

    kind: str
    target: SourceTarget | None = None
    inner: str | None = None
    problem: str | None = None
    source_shaped: bool = False


def resolve_href(
    href: str,
    page_file: str,
    wiki_dir: str,
    *,
    repo_identities: tuple[RepoIdentity, ...] = (),
    commit: str | None = None,
) -> Resolved:
    """Resolve a link on ``wiki_dir/page_file`` to its canonical meaning (see ``Resolved``)."""
    try:
        parts = urlsplit(href.strip())
        if parts.scheme or parts.netloc or href.strip().startswith("//"):
            if parts.scheme.lower() not in ("", "http", "https"):
                return Resolved("external")
            parts.port  # noqa: B018 - raises ValueError for a malformed port
            return _resolve_web(parts, wiki_dir, repo_identities, commit)
    except ValueError:
        return Resolved("invalid", problem=f"malformed URL: {href}")
    if not parts.path:
        return Resolved("anchor")
    raw_path = parts.path
    wiki_prefix = wiki_dir.rstrip("/") + "/"
    if ENCODED_PATH_RE.search(raw_path) or "\\" in unquote(raw_path):
        why = f"encoded path separators or dots are not allowed: {href}"
        return Resolved("invalid", problem=why, source_shaped=True)
    target = repo_path(page_file, wiki_dir, unquote(raw_path))
    if target is None:
        return Resolved("invalid", problem=f"link escapes the repository: {href}", source_shaped=True)
    if target.startswith(wiki_prefix) or target == wiki_dir.rstrip("/"):
        return Resolved("wiki", inner=target[len(wiki_prefix) :])
    frag = unquote(parts.fragment)
    m = LINES_RE.match(frag)
    if m is None and re.match(r"^L\d", frag):
        return Resolved("invalid", problem=f"bad line fragment (want #Lx or #Lx-Ly): {href}", source_shaped=True)
    lines = (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None
    return Resolved("source", SourceTarget(target, lines), source_shaped=True)


def _resolve_web(
    parts: SplitResult, wiki_dir: str, repo_identities: tuple[RepoIdentity, ...], commit: str | None
) -> Resolved:
    href = parts.geturl()
    if not parts.hostname:
        return Resolved("external")
    host = _host(parts.hostname, parts.port, parts.scheme or "https")
    segments = [unquote(s) for s in parts.path.split("/") if s]

    def bad(why: str) -> Resolved:
        return Resolved("invalid", problem=f"{why}: {href}", source_shaped=True)

    if ENCODED_PATH_RE.search(parts.path) and any(i.host == host for i in repo_identities):
        return bad("encoded path separators or dots in a URL on a registered repository host")
    matched = [i for i in repo_identities if i.matches(host, segments)]
    if not matched:
        return Resolved("external")  # another site or another repository: never evidence

    if len(matched) > 1:
        return bad("ambiguous source URL (it matches more than one registered repository identity)")
    ident = matched[0]
    rest = segments[len(ident.path.split("/")) :]
    frag = unquote(parts.fragment)
    if ident.gitlab:
        if rest[:1] == ["-"] and rest[1:2] and rest[1] in SOURCE_KINDS:
            rest = rest[1:]
        elif rest[:1] and rest[0] in SOURCE_KINDS:
            return bad("ambiguous GitLab source URL (GitLab source links have the form /-/blob/<commit>/<path>)")
        elif re.match(r"^L\d", frag):
            return bad("unsupported source URL (line fragment on a page that is not a file)")
        else:
            return Resolved("external")  # issues, merge requests and other project pages
    else:
        if rest[:1] == ["-"]:
            return bad("unsupported source URL (GitLab-style path on a GitHub-style host)")
        if not (rest[:1] and rest[0] in SOURCE_KINDS):
            if re.match(r"^L\d", frag):
                return bad("unsupported source URL (line fragment on a page that is not a file)")
            return Resolved("external")
    kind, ref, path_segments = rest[0], (rest[1] if len(rest) > 1 else ""), rest[2:]
    if kind in ("raw", "blame"):
        return bad(f"unsupported source URL ({kind}); cite /blob/<indexed commit>/<path> or a relative link")
    if ENCODED_PATH_RE.search(parts.path) or any(s in (".", "..") or "/" in s or "\\" in s for s in path_segments):
        return bad("encoded or traversing path in source URL")
    if not ref or not path_segments:
        return bad("unsupported source URL (no commit or path)")
    if commit is None:
        return bad("source URL cannot be checked without an indexed commit")
    if not FULL_SHA_RE.match(ref.lower()):
        return bad(f"source URL pins {ref!r}, not the full indexed commit {commit[:12]}; use a relative link")
    if ref.lower() != commit.lower():
        return bad(f"source URL pins commit {ref[:12]}, not the indexed commit {commit[:12]}; use a relative link")
    if parts.query and parts.query != "plain=1":
        return bad("unsupported query in source URL")
    lines = None
    if frag:
        m = (GITLAB_LINES_RE if ident.gitlab else LINES_RE).match(frag)
        if m is None:
            return bad("unsupported fragment in source URL (want #L1" + ("-42)" if ident.gitlab else "-L42)"))
        lines = (int(m.group(1)), int(m.group(2) or m.group(1)))
    if kind == "tree" and lines:
        return bad("line range on a directory")
    path = "/".join(path_segments)
    wiki_prefix = wiki_dir.rstrip("/") + "/"
    if path.startswith(wiki_prefix) or path == wiki_dir.rstrip("/"):
        return bad("web URL to a wiki page; link wiki pages relatively")
    return Resolved("source", SourceTarget(path, lines), source_shaped=True)


def repo_path(page_file: str, wiki_dir: str, href_path: str) -> str | None:
    """Repo-relative path a link on ``wiki_dir/page_file`` points at; None if it escapes the repo."""
    if href_path.startswith("/"):
        return None
    here = posixpath.dirname(posixpath.join(wiki_dir, page_file))
    joined = posixpath.normpath(posixpath.join(here, href_path))
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else joined


def relative_from_page(page_file: str, wiki_dir: str, target: str) -> str:
    """Relative href from a wiki page to a repo-relative path."""
    here = posixpath.dirname(posixpath.join(wiki_dir, page_file))
    return posixpath.relpath(target, here or ".")


def cited_files(
    markdown: str,
    page_file: str,
    wiki_dir: str,
    *,
    repo_identities: tuple[RepoIdentity, ...] = (),
    commit: str | None = None,
) -> list[str]:
    """Repo paths of source files a page links to (not other wiki pages), first-seen order.

    Relative links and web URLs into a registered identity at the indexed commit count alike;
    raw HTML links and links to other repositories never do."""
    seen: list[str] = []
    for link in find_links(markdown):
        if link.html:
            continue
        res = resolve_href(link.href, page_file, wiki_dir, repo_identities=repo_identities, commit=commit)
        if res.kind == "source" and res.target and res.target.path and res.target.path not in seen:
            seen.append(res.target.path)
    return seen


# --------------------------------------------------------------------------- canonical rewriting


WEB_URL_RE = re.compile(r"https?://[^\s<>`\"']+", re.I)


def canonicalize_links(
    text: str,
    page_file: str,
    wiki_dir: str,
    *,
    repo_identities: tuple[RepoIdentity, ...],
    commit: str,
    allow: Callable[[SourceTarget], bool] = lambda _t: True,
) -> str:
    """Rewrite every accepted web citation (a registered identity at ``commit``) into its canonical
    relative link, whatever the Markdown form: inline links with any label, images, reference
    definitions (also over two lines) and autolinks (which become ``[path:lines](href)``). Only real
    link destinations found by the parser are rewritten; code blocks and code spans are left alone."""
    wanted: dict[str, SourceTarget] = {}
    for link in find_links(text):
        if link.html:
            continue
        parts = urlsplit(link.href)
        if not (parts.scheme or parts.netloc):
            continue
        res = resolve_href(link.href, page_file, wiki_dir, repo_identities=repo_identities, commit=commit)
        if res.kind == "source" and res.target and allow(res.target):
            wanted[link.href] = res.target
    if not wanted:
        return text

    def match(raw: str) -> tuple[str, SourceTarget] | None:
        """The longest prefix of ``raw`` that is a wanted destination (a URL may stop at ')' or punctuation)."""
        cuts = [len(raw)] + [i for i in range(len(raw) - 1, 0, -1) if raw[i] in ")].,;:!?*_"]
        for cut in cuts:
            target = wanted.get(normalize_href(raw[:cut]))
            if target is not None:
                return raw[:cut], target
        return None

    def sub(chunk: str) -> str:
        out: list[str] = []
        pos = 0
        for m in WEB_URL_RE.finditer(chunk):
            hit = match(m.group(0))
            if hit is None:
                continue
            raw, target = hit
            start, stop = m.start(), m.start() + len(raw)
            href = target.href(page_file, wiki_dir)
            if chunk[start - 1 : start] == "<" and chunk[stop : stop + 1] == ">":
                before = chunk[: start - 1]
                # <url> is a destination after "](" (inline link) or "]:" (reference definition); otherwise an autolink.
                destination = re.search(r"\](?:\(|:)[ \t]*\n?[ \t]*$", before) is not None
                if not destination:  # an autolink: keep it a link, labelled with what it cites
                    label = target.path
                    if target.lines:
                        a, b = target.lines
                        label += f":{a}" if a == b else f":{a}-{b}"
                    out += [chunk[pos : start - 1], f"[{label}]({href})"]
                    pos = stop + 1
                    continue
            out += [chunk[pos:start], href]
            pos = stop
        return "".join([*out, chunk[pos:]])

    return outside_code(text, sub)


# --------------------------------------------------------------------------- removing links

FENCE_RE = re.compile(r"^(```|~~~)")
CODE_SPAN_RE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
_LABEL = r"(?:[^\[\]\\]|\\.|\[(?:[^\[\]\\]|\\.)*\])*"
_DEST = r"<[^<>\n]*>|(?:[^\s()<>\\]|\\.|\((?:[^\s()\\]|\\.)*\))+"
_TITLE = r"""(?:\s+(?:"[^"]*"|'[^']*'|\([^()]*\)))?"""
INLINE_LINK_RE = re.compile(rf"(!?)\[({_LABEL})\]\(\s*({_DEST})?{_TITLE}\s*\)")
REF_DEF_RE = re.compile(rf"^ {{0,3}}\[({_LABEL})\]:[ \t]*({_DEST}){_TITLE}[ \t]*\n?", re.M)
FULL_REF_RE = re.compile(rf"(!?)\[({_LABEL})\]\[({_LABEL})\]")
SHORT_REF_RE = re.compile(rf"(!?)\[({_LABEL})\](?:\[\])?(?![\[(:])")


def outside_code(text: str, fn: Callable[[str], str]) -> str:
    """Apply `fn` to the parts of `text` outside fenced code blocks and inline code spans."""

    def prose(chunk: str) -> str:
        out, pos = [], 0
        for m in CODE_SPAN_RE.finditer(chunk):
            out += [fn(chunk[pos : m.start()]), m.group(0)]
            pos = m.end()
        return "".join([*out, fn(chunk[pos:])])

    out: list[str] = []
    chunk: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = FENCE_RE.match(line.lstrip())
        if fence is None and m:
            out.append(prose("".join(chunk)))
            chunk, fence = [], m.group(1)
            out.append(line)
        elif fence is not None:
            out.append(line)
            closing = line.strip()
            if closing.startswith(fence) and set(closing) == {fence[0]}:
                fence = None
        else:
            chunk.append(line)
    out.append(prose("".join(chunk)))
    return "".join(out)


def normalize_href(raw: str) -> str:
    """A raw Markdown link destination as the parser reports it (percent-encoded, no <>)."""
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1]
    return str(_PARSER.normalizeLink(re.sub(r"\\(.)", r"\1", raw)))


def _label_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def unlink(text: str, bad_hrefs: set[str]) -> str:
    """Turn every link or image whose target is in `bad_hrefs` into its plain text.

    Handles inline links, reference definitions and every kind of reference use
    (full, collapsed, shortcut). Code blocks and code spans are left alone.
    """
    if not bad_hrefs:
        return text
    dead: set[str] = set()

    def drop_defs(chunk: str) -> str:
        def one(m: re.Match[str]) -> str:
            if normalize_href(m.group(2)) in bad_hrefs:
                dead.add(_label_key(m.group(1)))
                return ""
            return m.group(0)

        return REF_DEF_RE.sub(one, chunk)

    def drop_uses(chunk: str) -> str:
        chunk = INLINE_LINK_RE.sub(
            lambda m: m.group(2) if normalize_href(m.group(3) or "") in bad_hrefs else m.group(0), chunk
        )
        chunk = FULL_REF_RE.sub(lambda m: m.group(2) if _label_key(m.group(3)) in dead else m.group(0), chunk)
        return SHORT_REF_RE.sub(lambda m: m.group(2) if _label_key(m.group(2)) in dead else m.group(0), chunk)

    text = outside_code(text, drop_defs)
    return outside_code(text, drop_uses)
