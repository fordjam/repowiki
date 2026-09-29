"""Finding links and citations in wiki Markdown.

A citation is an ordinary relative Markdown link whose fragment is a line
range: ``[cli.py](../../src/tool/cli.py#L10-L42)``. Links are read with the
same parser the site renderer uses, so code blocks never produce false hits.
"""

from __future__ import annotations

import posixpath
import re
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
