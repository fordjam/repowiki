"""Finding links and citations in wiki Markdown.

A citation is an ordinary relative Markdown link whose fragment is a line
range: ``[cli.py](../../src/tool/cli.py#L10-L42)``. Links are read with the
same parser the site renderer uses, so code blocks never produce false hits.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

LINES_RE = re.compile(r"^L(\d+)(?:-L(\d+))?$")

_PARSER = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])


@dataclass(frozen=True)
class Link:
    href: str
    line: int  # 1-based line in the page where the enclosing block starts
    image: bool = False

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


def find_links(markdown: str) -> list[Link]:
    found: list[Link] = []
    for block in _PARSER.parse(markdown):
        if block.type != "inline" or not block.children:
            continue
        line = (block.map[0] + 1) if block.map else 0
        for tok in block.children:
            if tok.type == "link_open":
                found.append(Link(str(tok.attrGet("href") or ""), line))
            elif tok.type == "image":
                found.append(Link(str(tok.attrGet("src") or ""), line, image=True))
    return found


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


def cited_files(markdown: str, page_file: str, wiki_dir: str) -> list[str]:
    """Repo paths of source files a page links to (not other wiki pages), first-seen order."""
    seen: list[str] = []
    prefix = wiki_dir.rstrip("/") + "/"
    for link in find_links(markdown):
        if link.external or not link.path:
            continue
        target = repo_path(page_file, wiki_dir, link.path)
        if target and not target.startswith(prefix) and target not in seen:
            seen.append(target)
    return seen


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
