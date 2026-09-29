"""Wiki Markdown -> HTML fragments, with citation chips and safe raw HTML."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from html import escape
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .links import LINES_RE

# Raw HTML in pages is limited to these tags, always stripped of attributes
# (except a bare `open` on <details>), so pages cannot inject script or style.
SAFE_TAGS = frozenset("details summary br hr sup sub kbd b i em strong code p ul ol li mark small div span".split())
TAG_RE = re.compile(r"<!--.*?-->|<(/?)([A-Za-z][A-Za-z0-9-]*)((?:\s[^<>]*?)?)(/?)>", re.S)


def clean_html(raw: str) -> str:
    parts: list[str] = []
    pos = 0
    for m in TAG_RE.finditer(raw):
        parts.append(escape(raw[pos : m.start()], quote=False))
        pos = m.end()
        if m.group(2) is None:
            continue  # drop comments
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3) or ""
        if tag not in SAFE_TAGS:
            parts.append(escape(m.group(0)))
        elif closing:
            parts.append(f"</{tag}>")
        elif tag == "details" and re.search(r"(^|\s)open\b", attrs):
            parts.append("<details open>")
        else:
            parts.append(f"<{tag}>")
    parts.append(escape(raw[pos:], quote=False))
    return "".join(parts)


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[\s_-]+", "-", re.sub(r"[^\w\s-]", "", text.lower()).strip()) or "section"


@dataclass
class Target:
    """Where a link in the page should go once rendered."""

    href: str | None  # None renders as plain text (no link)
    path: str | None = None  # repo path, for citation chips
    lines: tuple[int, int] | None = None


Resolver = Callable[[str], Target]


@dataclass
class Rendered:
    html: str
    title: str | None
    headings: list[tuple[int, str, str]] = field(default_factory=list)
    summary: str = ""
    mermaid: bool = False


def _text_of(inline: Token) -> str:
    if not inline.children:
        return inline.content
    return "".join(
        " " if c.type in ("softbreak", "hardbreak") else c.content
        for c in inline.children
        if c.type in ("text", "code_inline", "softbreak", "hardbreak")
    )


def _closing(children: list[Token], start: int) -> int:
    depth = 0
    for j in range(start, len(children)):
        if children[j].type == "link_open":
            depth += 1
        elif children[j].type == "link_close":
            depth -= 1
            if depth == 0:
                return j
    return len(children) - 1


def _is_citation_label(label: str, path: str) -> bool:
    """True when a link's text only restates the file/lines, e.g. ``cli.py:10-20``."""
    bare = re.sub(r"[:#]L?\d+(?:-L?\d+)?$", "", label).strip()
    return not bare or bare in (path, path.rsplit("/", 1)[-1])


def _apply_links(children: list[Token], resolve: Resolver | None) -> list[Token]:
    out: list[Token] = []
    i = 0
    while i < len(children):
        tok = children[i]
        if tok.type != "link_open" or resolve is None:
            out.append(tok)
            i += 1
            continue
        end = _closing(children, i)
        target = resolve(str(tok.attrGet("href") or ""))
        inner = children[i + 1 : end]
        if target.href is None:
            out.extend(inner)
            i = end + 1
            continue
        tok.attrSet("href", target.href)
        if target.lines and target.path:
            a, b = target.lines
            chip = Token("citation", "", 0)
            chip.meta = {"path": target.path, "lines": f"{a}" if a == b else f"{a}-{b}"}
            tok.attrSet("title", f"{target.path} lines {chip.meta['lines']}")
            label = "".join(t.content for t in inner if t.type in ("text", "code_inline")).strip()
            if _is_citation_label(label, target.path):
                tok.attrSet("class", "cite")
                out.extend([tok, chip, children[end]])
            else:  # prose link: keep the words, add a small line badge
                chip.meta["path"] = ""
                tok.attrSet("class", "cite-labelled")
                out.extend([tok, *inner, chip, children[end]])
        else:
            out.extend([tok, *inner, children[end]])
        i = end + 1
    return out


def _build() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "typographer": False}).enable(["table", "strikethrough"])
    base_fence = md.renderer.rules["fence"]  # type: ignore[attr-defined]

    def fence(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        info = tokens[idx].info.strip().split(maxsplit=1)
        if info and info[0].lower() == "mermaid":
            return f'<pre class="mermaid">{escape(tokens[idx].content)}</pre>\n'
        return base_fence(tokens, idx, options, env)

    def raw_html(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        return clean_html(tokens[idx].content)

    def link_open(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        if urlsplit(tokens[idx].attrGet("href") or "").scheme in ("http", "https"):
            tokens[idx].attrSet("rel", "noopener noreferrer")
        return renderer.renderToken(tokens, idx, options, env)

    def citation(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        meta = tokens[idx].meta
        path, lines = escape(meta["path"]), escape(meta["lines"])
        file = f'<span class="cite-file">{path}</span>' if path else ""
        return f'{file}<span class="cite-lines">{lines}</span>'

    def table_open(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        return '<div class="table-wrap">' + renderer.renderToken(tokens, idx, options, env)

    def table_close(renderer, tokens, idx, options, env):  # type: ignore[no-untyped-def]
        return renderer.renderToken(tokens, idx, options, env) + "</div>\n"

    for name, fn in {
        "fence": fence, "html_block": raw_html, "html_inline": raw_html, "link_open": link_open,
        "citation": citation, "table_open": table_open, "table_close": table_close,
    }.items():  # fmt: skip
        md.add_render_rule(name, fn)
    return md


_MD = _build()


def render(source: str, resolve: Resolver | None = None) -> Rendered:
    env: dict = {}
    tokens = _MD.parse(source, env)
    used: dict[str, int] = {}
    result = Rendered("", None)
    details = 0
    for i, tok in enumerate(tokens):
        if tok.type == "html_block":
            details += tok.content.count("<details") - tok.content.count("</details")
        if tok.type == "heading_open":
            text = _text_of(tokens[i + 1]).strip()
            base = slugify(text)
            n = used.get(base, 0)
            used[base] = n + 1
            anchor = base if n == 0 else f"{base}-{n}"
            tok.attrSet("id", anchor)
            level = int(tok.tag[1])
            if level == 1 and result.title is None:
                result.title = text
            elif level in (2, 3):
                result.headings.append((level, anchor, text))
        elif tok.type == "paragraph_open" and not result.summary and details <= 0 and tok.level == 0:
            text = " ".join(_text_of(tokens[i + 1]).split())
            if not text.startswith("Sources:"):
                result.summary = text
        elif tok.type == "fence" and tok.info.strip().lower().startswith("mermaid"):
            result.mermaid = True
        if tok.type == "inline" and tok.children:
            tok.children = _apply_links(tok.children, resolve)
    result.html = _MD.renderer.render(tokens, _MD.options, env)
    return result


def split_href(href: str) -> tuple[str, str, tuple[int, int] | None]:
    """(path, fragment, line range) of a relative href."""
    parts = urlsplit(href)
    frag = unquote(parts.fragment)
    m = LINES_RE.match(frag)
    lines = (int(m.group(1)), int(m.group(2) or m.group(1))) if m else None
    return unquote(parts.path), frag, lines
