"""Checks a wiki must pass: index schema, page shape, links and pinned citations."""

from __future__ import annotations

import html
import json
import posixpath
import re
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .git import SHA_RE, Snapshot, has_commit, remote_web_url
from .links import Link, RepoIdentity, SourceTarget, find_links, identities, resolve_href
from .policy import DEFAULT_POLICY, Policy
from .render import diagram_sources, heading_ids
from .safety import INDEX, UnsafePathError, read_wiki_file, safe_page_file, wiki_lock, wiki_path


@dataclass(frozen=True)
class Problem:
    file: str
    line: int
    message: str
    level: str = "error"

    def __str__(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"{where}: {self.level}: {self.message}"


HTML_SOURCE_HINT = "write it as a Markdown link, e.g. [file.py:3-9](../../src/file.py#L3-L9)"

# The offline resource policy for embedded images: only raster files inside the repository ship with
# the site, and they must be what they claim to be. No user SVG or data URI; nothing is fetched.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"})
MAX_IMAGE_BYTES = 5 * 1024 * 1024
# A PNG is decoded (inflated) in full to prove it is complete; this caps the memory-free work that takes.
MAX_DECODED_BYTES = 256 * 1024 * 1024
_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WebP", ".gif": "GIF"}


def image_content_defect(data: bytes, suffix: str | None = None) -> str | None:
    """Why `data` is not a complete, well-formed approved raster image (PNG, JPEG, WebP or GIF), or None.

    A magic number is not enough: a truncated or corrupt file would replace a good published asset. Each
    format's structure is walked to its end with every dimension checked (PNG: every chunk CRC and the
    full inflated image size; JPEG: frame header, scans and end marker; GIF: screen, image and trailer
    blocks; WebP: RIFF size and the VP8/VP8L/VP8X frame header). With `suffix`, the content must also be
    the format the file name claims."""
    if len(data) > MAX_IMAGE_BYTES:
        return f"it is {len(data)} bytes, over the {MAX_IMAGE_BYTES // (1024 * 1024)} MiB image limit"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, why = "PNG", _png_defect(data)
    elif data.startswith(b"\xff\xd8"):
        kind, why = "JPEG", _jpeg_defect(data)
    elif data.startswith((b"GIF87a", b"GIF89a")):
        kind, why = "GIF", _gif_defect(data)
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, why = "WebP", _webp_defect(data)
    else:
        return "its content is not a PNG, JPEG, WebP or GIF image"
    if why:
        return f"it is not a valid {kind} image: {why}"
    want = _FORMATS.get((suffix or "").lower())
    if suffix is not None and want != kind:
        return f"its content is a {kind} image, not the {want or suffix} its name claims"
    return None


_PNG_DEPTHS = {0: (1, 2, 4, 8, 16), 2: (8, 16), 3: (1, 2, 4, 8), 4: (8, 16), 6: (8, 16)}
_PNG_CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}
_ADAM7 = ((0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2))


def _png_defect(data: bytes) -> str | None:
    pos, ihdr, palette, idat = 8, None, False, zlib.decompressobj()
    expected = inflated = 0
    rows: list[tuple[int, int]] = []  # (row count, bytes per row) of each pass
    scan = _Scanlines(rows)
    while True:
        if pos + 12 > len(data):
            return "truncated (no IEND chunk)"
        length, ctype = int.from_bytes(data[pos : pos + 4], "big"), data[pos + 4 : pos + 8]
        end = pos + 12 + length
        if end > len(data):
            return f"truncated {ctype.decode('latin-1')!r} chunk"
        body = data[pos + 8 : pos + 8 + length]
        if zlib.crc32(ctype + body) != int.from_bytes(data[end - 4 : end], "big"):
            return f"bad CRC in the {ctype.decode('latin-1')!r} chunk"
        if ihdr is None:
            if ctype != b"IHDR" or length != 13:
                return "the first chunk is not IHDR"
            width, height = int.from_bytes(body[:4], "big"), int.from_bytes(body[4:8], "big")
            depth, color, compression, filtering, interlace = body[8:13]
            if not (0 < width < 2**31 and 0 < height < 2**31):
                return f"bad dimensions {width}x{height}"
            if depth not in _PNG_DEPTHS.get(color, ()) or compression or filtering or interlace > 1:
                return "bad IHDR header"
            bits = depth * _PNG_CHANNELS[color]
            passes = _ADAM7 if interlace else ((0, 0, 1, 1),)
            for x0, y0, dx, dy in passes:
                w, h = (width - x0 + dx - 1) // dx, (height - y0 + dy - 1) // dy
                if w and h:
                    expected += h * (1 + (w * bits + 7) // 8)
                    rows.append((h, (w * bits + 7) // 8))
            if expected > MAX_DECODED_BYTES:
                return f"{width}x{height} is too large to decode"
            ihdr = color
        elif ctype == b"PLTE":
            palette = True
        elif ctype == b"IDAT":
            if ihdr == 3 and not palette:
                return "palette image without a PLTE chunk"
            try:
                while body and not idat.eof:
                    out = idat.decompress(body, 1 << 20)
                    inflated += len(out)
                    body = idat.unconsumed_tail
                    if inflated > expected:
                        return "image data is larger than its dimensions"
                    bad = scan.feed(out)
                    if bad is not None:
                        return f"scanline {bad[0] + 1} has filter type {bad[1]} (only 0-4 exist)"
            except zlib.error as exc:
                return f"corrupt image data ({exc})"
        elif ctype == b"IEND":
            break
        pos = end
    if not idat.eof or inflated != expected:
        return "image data is incomplete"
    return None


class _Scanlines:
    """Walks inflated PNG data row by row (pass by pass when interlaced): every row starts with a
    filter-type byte, and only types 0-4 exist."""

    def __init__(self, rows: list[tuple[int, int]]) -> None:
        self.rows = rows
        self.index = 0  # rows seen, across passes
        self.left = 0  # bytes of the current row's pixel data still to come

    def _row_bytes(self) -> int:
        seen = self.index
        for count, size in self.rows:
            if seen < count:
                return size
            seen -= count
        return 0

    def feed(self, chunk: bytes) -> tuple[int, int] | None:
        """The (row, filter type) of the first bad row in `chunk`, or None."""
        pos = 0
        while pos < len(chunk):
            if self.left:
                step = min(self.left, len(chunk) - pos)
                self.left -= step
                pos += step
                continue
            if chunk[pos] > 4:
                return self.index, chunk[pos]
            self.left = self._row_bytes()
            self.index += 1
            pos += 1
        return None


_JPEG_SOF = frozenset({*range(0xC0, 0xC4), *range(0xC5, 0xC8), *range(0xC9, 0xCC), *range(0xCD, 0xD0)})


def _jpeg_defect(data: bytes) -> str | None:
    pos, frame = 2, False
    while True:
        if pos + 2 > len(data) or data[pos] != 0xFF:
            return "truncated or corrupt marker"
        while pos + 1 < len(data) and data[pos + 1] == 0xFF:
            pos += 1  # fill bytes
        if pos + 2 > len(data):
            return "truncated marker"
        marker = data[pos + 1]
        pos += 2
        if marker == 0xD9:
            return None if frame else "no image before the end marker"
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > len(data):
            return "truncated segment"
        length = int.from_bytes(data[pos : pos + 2], "big")
        if length < 2 or pos + length > len(data):
            return "truncated segment"
        seg = data[pos + 2 : pos + length]
        pos += length
        if marker in _JPEG_SOF:
            if len(seg) < 6:
                return "short frame header"
            height, width, comps = int.from_bytes(seg[1:3], "big"), int.from_bytes(seg[3:5], "big"), seg[5]
            if not (height and width and 1 <= comps <= 4 and len(seg) == 6 + 3 * comps):
                return f"bad frame header ({width}x{height}, {comps} components)"
            frame = True
        elif marker == 0xDA:
            if not frame:
                return "scan before the frame header"
            while True:  # entropy-coded data runs to the next marker other than stuffing or a restart
                pos = data.find(b"\xff", pos)
                if pos < 0 or pos + 1 >= len(data):
                    return "truncated scan (no end marker)"
                nxt = data[pos + 1]
                if nxt == 0 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    pos += 1
                    continue
                break


def _gif_sub_blocks(data: bytes, pos: int) -> int | None:
    while pos < len(data):
        size = data[pos]
        pos += 1 + size
        if size == 0:
            return pos
    return None


def _gif_lzw_defect(min_size: int, data: bytes, pos: int) -> str | None:
    """Why an image's LZW data sub-blocks (starting at `pos`, ending with the zero terminator) are
    not a well-formed, non-empty code stream, or None."""
    stream = bytearray()
    while data[pos]:
        stream += data[pos + 1 : pos + 1 + data[pos]]
        pos += 1 + data[pos]
    if not stream:
        return "the image has no LZW data"
    clear, eoi = 1 << min_size, (1 << min_size) + 1
    size, next_code, prev, codes = min_size + 1, eoi + 1, None, 0
    bit, total = 0, len(stream) * 8
    while bit + size <= total:
        code = (int.from_bytes(stream[bit // 8 : bit // 8 + 3], "little") >> (bit % 8)) & ((1 << size) - 1)
        bit += size
        if code == clear:
            size, next_code, prev = min_size + 1, eoi + 1, None
            continue
        if code == eoi:
            break
        if prev is None:
            if code >= clear:
                return f"LZW code {code} before any entry exists"
        else:
            if code > next_code:
                return f"LZW code {code} is past the table ({next_code})"
            if next_code < 4096:
                next_code += 1
                if next_code == 1 << size and size < 12:
                    size += 1
        prev = code
        codes += 1
    return None if codes else "the image's LZW data holds no pixels"


def _gif_defect(data: bytes) -> str | None:
    if len(data) < 13:
        return "truncated header"
    width, height, packed = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little"), data[10]
    if not (width and height):
        return f"bad dimensions {width}x{height}"
    pos = 13 + (3 << ((packed & 7) + 1) if packed & 0x80 else 0)
    images = 0
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            return None if images else "no image before the trailer"
        if block == 0x21:
            end = _gif_sub_blocks(data, pos + 2)
        elif block == 0x2C:
            if pos + 11 > len(data):
                return "truncated image descriptor"
            w, h = int.from_bytes(data[pos + 5 : pos + 7], "little"), int.from_bytes(data[pos + 7 : pos + 9], "little")
            flags = data[pos + 9]
            if not (w and h):
                return f"bad image dimensions {w}x{h}"
            start = pos + 10 + (3 << ((flags & 7) + 1) if flags & 0x80 else 0)
            if start >= len(data) or not 2 <= data[start] <= 8:
                return "bad LZW minimum code size"
            end = _gif_sub_blocks(data, start + 1)
            if end is None or end > len(data):
                return "truncated image data"
            why = _gif_lzw_defect(data[start], data, start + 1)
            if why:
                return why
            images += 1
        else:
            return f"unknown block 0x{block:02x}"
        if end is None or end > len(data):
            return "truncated block"
        pos = end
    return "truncated (no trailer)"


def _webp_defect(data: bytes) -> str | None:
    size = int.from_bytes(data[4:8], "little")
    if size < 4 or 8 + size > len(data):
        return "truncated RIFF container"
    pos, end, frame, canvas = 12, 8 + size, False, False
    while pos + 8 <= end:
        fourcc, csize = data[pos : pos + 4], int.from_bytes(data[pos + 4 : pos + 8], "little")
        body = data[pos + 8 : pos + 8 + csize]
        if pos + 8 + csize > end:
            return f"truncated {fourcc.decode('latin-1')!r} chunk"
        if fourcc == b"VP8 ":
            w, h = int.from_bytes(body[6:8], "little") & 0x3FFF, int.from_bytes(body[8:10], "little") & 0x3FFF
            if len(body) < 10 or body[3:6] != b"\x9d\x01\x2a" or not (w and h):
                return "bad VP8 frame header"
            frame = True
        elif fourcc == b"VP8L":
            if len(body) < 5 or body[0] != 0x2F:
                return "bad VP8L header"
            frame = True
        elif fourcc == b"VP8X":
            if len(body) < 10:
                return "bad VP8X header"
            canvas = True
        elif fourcc == b"ANMF" and canvas:
            frame = True
        pos += 8 + csize + (csize & 1)
    return None if frame else "no image frame"


# Mermaid fetches by itself only through a few constructs: image nodes (`A@{ img: "…" }`), resource tags
# in HTML labels, and CSS (`style`, `classDef`, `themeCSS`) with url(), image-set(), @import or
# @font-face. A diagram carrying any of them is refused, whatever encoding hides it (Mermaid `#58;`
# entities, HTML entities, YAML/JS/CSS backslash escapes, %-encoding). Plain URLs in labels are text and
# `click` links are explicit navigation; both stay allowed.
_DIAGRAM_FETCHES = (
    (re.compile(r"@\{.*?[\s{,'\"]img['\"]?\s*:", re.I | re.S), "an image node"),
    (
        re.compile(
            r"<(img|image|svg|link|script|style|iframe|frame|object|embed|video|audio|source|picture|input"
            r"|use|feimage|track|meta|base)(?![\w-])",
            re.I,
        ),
        "a resource tag",
    ),
    (re.compile(r"(?<![\w-])url\(|(?<!\w)(?:-webkit-)?image-set\(|@import|@font-face", re.I), "CSS that loads a URL"),
)
_ENTITY = re.compile(r"#(x[0-9A-Fa-f]{1,6}|\d{1,7}|[A-Za-z][A-Za-z0-9]{1,31});")
_ESCAPE = re.compile(r"\\(?:[xX]([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|U([0-9A-Fa-f]{8})|([0-9A-Fa-f]{1,6})\s?|(.))", re.S)
_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u00ad\u200b-\u200f\u2060-\u2064\ufeff]")


def _char(code: int) -> str:
    return chr(code) if 0 < code <= 0x10FFFF else ""


def _unescape(m: re.Match[str]) -> str:
    if m.group(5) is not None:
        return m.group(5)
    return _char(int(next(g for g in m.groups() if g), 16))


def _entity(m: re.Match[str]) -> str:
    name = m.group(1)
    if name.isdigit():
        return _char(int(name))
    if name[0] in "xX" and all(c in "0123456789abcdefABCDEF" for c in name[1:]):
        return _char(int(name[1:], 16))
    return html.unescape(f"&{name};")


def _decoded(text: str) -> str:
    """`text` with every encoding layer Mermaid, YAML, JSON, CSS or a URL could unwrap removed."""
    for _ in range(5):
        before = text
        text = unquote(html.unescape(_ENTITY.sub(_entity, _ESCAPE.sub(_unescape, text))))
        if text == before:
            break
    return _INVISIBLE.sub("", text)


def diagram_problem(diagram: str) -> str | None:
    """Why rendering a Mermaid diagram would make the reader's browser fetch something, or None.

    The offline resource policy holds for diagrams as it does for images: a diagram that could load a
    resource (an image node, a resource tag in an HTML label, CSS that loads a URL) is refused, so the
    build fails and the last good site stays in place."""
    text = _decoded(diagram)
    for pattern, what in _DIAGRAM_FETCHES:
        if pattern.search(text):
            return f"offline site policy: this diagram has {what}, which would load a resource; remove it"
    return None


def image_problem(
    link: Link, file: str, *, wiki_dir: str, snap: Snapshot | None, policy: Policy
) -> str | None:
    """Why a Markdown image on page `file` is refused by the offline resource policy, or None.

    A remote, protocol-relative or ``data:`` source would make the reader's browser fetch something,
    so only a local image inside the repository is accepted; it must exist at the indexed commit, be
    an approved type at most 5 MiB, and not be excluded by the secret policy. A refused image fails
    the build, so the last good site stays in place; nothing is silently dropped."""
    raw = link.href.strip()
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc or raw.startswith("//"):
        kind = f"{parts.scheme.lower()}:" if parts.scheme else "a protocol-relative URL"
        return f"offline site policy: images must be local files in the repository, not {kind}: {link.href}"
    res = resolve_href(link.href, file, wiki_dir)
    if res.kind == "invalid":
        return res.problem
    if res.kind == "anchor":
        return f"image has no path: {link.href}"
    if res.kind == "wiki":
        target = wiki_dir.rstrip("/") + "/" + (res.inner or "")
    else:
        assert res.target is not None
        if res.target.lines:
            return f"line range on an image: {link.href}"
        target = res.target.path
    if policy.secret(target):
        return f"links to a file excluded by the secret policy: {link.href}"
    suffix = posixpath.splitext(target)[1].lower()
    if suffix not in IMAGE_SUFFIXES:
        want = "PNG, JPEG, WebP or GIF"
        return f"offline site policy: images must be {want}, not {suffix or 'this file type'}: {link.href}"
    if snap is None:
        return None
    if not snap.has_file(target):
        return (
            f"missing image {target} at {snap.commit[:7]}: {link.href} "
            "(nothing was built; the last good site stays in place)"
        )
    if snap.sizes.get(target, 0) > MAX_IMAGE_BYTES:
        mib = MAX_IMAGE_BYTES // (1024 * 1024)
        return f"{target} at {snap.commit[:7]} is over the {mib} MiB image limit: {link.href}"
    return None


def repo_identities(root: Path | None, extra: tuple[str, ...] = ()) -> tuple[RepoIdentity, ...]:
    """The repository's registered web identities: its ``origin`` remote's web URL (from local Git
    configuration; nothing is fetched) plus explicitly registered aliases (e.g. a site ``url``)."""
    origin = remote_web_url(root) if root is not None else None
    return identities([origin, *extra])


@dataclass
class Fragments:
    """The frozen page texts that wiki fragments (``page.md#setup``, ``#setup``) are checked against.

    ``texts`` are the candidate texts being validated, never the working tree: a heading that exists only
    in a file on disk does not make a fragment valid. ``complete`` is False while a single page is being
    written and the other pages do not exist yet; fragments into those pages are then checked later,
    against the whole candidate wiki."""

    texts: Mapping[str, str]
    complete: bool = True
    _ids: dict[str, set[str]] = field(default_factory=dict, repr=False)

    def ids(self, page: str) -> set[str] | None:
        if page not in self.texts:
            return None
        if page not in self._ids:
            self._ids[page] = heading_ids(self.texts[page])
        return self._ids[page]


def fragment_problem(link: Link, file: str, wiki_dir: str, fragments: Fragments) -> str | None:
    """Why the fragment of a same-page or wiki-page link on page ``file`` is invalid, or None.

    A fragment must be a heading ID of the target page's frozen text, by the renderer's own rule
    (``render.heading_ids``). Links to other sites and source links are not judged here."""
    if link.html or link.external:
        return None
    frag = link.fragment
    if not frag:
        return None
    res = resolve_href(link.href, file, wiki_dir)
    if res.kind == "anchor":
        page = file
    elif res.kind == "wiki":
        page = res.inner or ""
    else:
        return None
    ids = fragments.ids(page)
    if ids is None:
        if not fragments.complete and page != file:
            return None  # that page is still being written: checked against the whole candidate wiki
        return f"fragment #{frag} cannot be checked: {page or wiki_dir} is not a page of this wiki: {link.href}"
    if frag not in ids:
        where = "this page" if page == file else page
        return f"no heading with id #{frag} on {where}: {link.href}"
    return None


@dataclass(frozen=True)
class PageEntry:
    file: str
    title: str
    parent: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "title": self.title, "parent": self.parent}


def check_index(
    data: object, root: Path | None = None, *, wiki_dir: str = "docs/wiki", policy: Policy = DEFAULT_POLICY
) -> tuple[list[Problem], list[PageEntry]]:
    """Schema check for a parsed index.json. `root` enables the commit-exists check.

    Page files whose path matches the secret policy (e.g. ``agent-transcript.md``) are problems
    and are left out of the returned entries, so no caller ever reads, renders or sends them.
    """
    probs: list[Problem] = []
    entries: list[PageEntry] = []

    def bad(msg: str) -> None:
        probs.append(Problem(INDEX, 0, msg))

    if not isinstance(data, dict):
        bad("top level must be an object")
        return probs, entries
    if not isinstance(data.get("repo"), str) or not data["repo"].strip():
        bad('"repo" must be a non-empty string')
    sha = data.get("indexed_commit")
    if not isinstance(sha, str) or not SHA_RE.match(sha):
        bad('"indexed_commit" must be a hex commit id')
    elif root is not None and not has_commit(root, sha):
        bad(f'"indexed_commit" {sha[:12]} is not a commit in this repository')
    at = data.get("indexed_at")
    try:
        if not isinstance(at, str):
            raise ValueError
        datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        bad('"indexed_at" must be an ISO 8601 timestamp')
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        bad('"pages" must be a non-empty list')
        return probs, entries

    seen: set[str] = set()
    for n, entry in enumerate(pages):
        if not isinstance(entry, dict):
            bad(f"pages[{n}] must be an object")
            continue
        file, title, parent = entry.get("file"), entry.get("title"), entry.get("parent")
        if not safe_page_file(file):
            bad(f"pages[{n}].file must be a plain .md file name inside the wiki directory, got {file!r}")
            continue
        assert isinstance(file, str)
        if policy.secret(f"{wiki_dir}/{file}"):
            bad(f"pages[{n}].file {file} matches the secret policy; repowiki will not read, render or send it")
            continue
        if file in seen:
            bad(f"pages[{n}].file {file} is listed twice")
            continue
        seen.add(file)
        if not isinstance(title, str) or not title.strip():
            bad(f"pages[{n}].title must be a non-empty string")
            title = file
        if parent is not None and not isinstance(parent, str):
            bad(f"pages[{n}].parent must be a page file name or null")
            parent = None
        entries.append(PageEntry(file, str(title), parent))

    files = {e.file for e in entries}
    by_file = {e.file: e for e in entries}
    for e in entries:
        if e.parent is None:
            continue
        if e.parent not in files:
            bad(f"{e.file}: parent {e.parent!r} is not a listed page")
        elif e.parent == e.file:
            bad(f"{e.file}: a page cannot be its own parent")
        else:
            node, hops = by_file.get(e.parent), 0
            while node is not None and node.parent and hops <= len(entries):
                if node.parent == e.file:
                    bad(f"{e.file}: parent chain forms a cycle")
                    break
                node, hops = by_file.get(node.parent), hops + 1
    return probs, entries


def check_page(
    file: str,
    text: str,
    *,
    wiki_dir: str,
    wiki_path: Path,
    snap: Snapshot | None,
    extra_pages: set[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
    min_citations: int = 0,
    repo_identities: tuple[RepoIdentity, ...] = (),
    page_texts: Mapping[str, str] | None = None,
    wiki_files: set[str] | None = None,
) -> list[Problem]:
    """Shape, relative links and citations of one page.

    Links into the wiki directory must exist on disk (or be in `extra_pages`,
    pages about to be written). Everything else must exist at the snapshot
    commit, and any ``#Lx-Ly`` range must lie inside the file at that commit.
    Web URLs into a registered identity are checked the same way (see ``links.resolve_href``).

    Fragments are checked against frozen texts: ``#x`` against this page's ``text``, ``page.md#x``
    against ``page_texts`` (the whole candidate wiki). Without ``page_texts`` (one page being written)
    fragments into other pages are left for the whole-wiki check.

    Only valid citations count toward ``min_citations``, and each distinct canonical source range
    counts once (a relative link and a pinned web URL to the same lines are one citation).
    """
    probs: list[Problem] = []
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not first.startswith("# "):
        probs.append(Problem(file, 1, "page must start with a '# ' title"))
    if "<details" not in text or "Relevant source files" not in text:
        probs.append(Problem(file, 0, "missing <details> 'Relevant source files' block"))

    for line, diagram in diagram_sources(text):
        why = diagram_problem(diagram)
        if why:
            probs.append(Problem(file, line, why))
    links = find_links(text)
    commit = snap.commit if snap else None
    fragments = Fragments({**(page_texts or {}), file: text}, complete=page_texts is not None)
    cited: set[SourceTarget] = set()
    link_probs: list[Problem] = []
    for link in links:
        msg = link_problem(
            link,
            file,
            wiki_dir=wiki_dir,
            wiki_path=wiki_path,
            snap=snap,
            extra_pages=extra_pages,
            policy=policy,
            removed=removed,
            repo_identities=repo_identities,
            fragments=fragments,
            wiki_files=wiki_files,
        )
        if msg:
            link_probs.append(Problem(file, link.line, msg))
            continue
        if link.html:
            continue  # raw HTML is escaped when rendered: never evidence
        res = resolve_href(link.href, file, wiki_dir, repo_identities=repo_identities, commit=commit)
        if res.kind == "source" and res.target and res.target.lines:
            cited.add(res.target)
    if len(cited) < min_citations:
        probs.append(
            Problem(
                file,
                0,
                f"has {len(cited)} line citation(s); at least {min_citations} required (e.g. [a.py:3-9](..#L3-L9)); "
                "only valid citations count, and each distinct source range counts once",
            )
        )
    return probs + link_probs


def link_problem(
    link: Link,
    file: str,
    *,
    wiki_dir: str,
    wiki_path: Path,
    snap: Snapshot | None,
    extra_pages: set[str] | None = None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
    repo_identities: tuple[RepoIdentity, ...] = (),
    fragments: Fragments | None = None,
    wiki_files: set[str] | None = None,
) -> str | None:
    """Why one link on page `file` is invalid, or None if it is fine.

    `extra_pages` are pages about to be written; `removed` are pages about to be deleted.
    Links to other sites and other repositories are fine (and never count as citations).
    `wiki_files` are the files of a frozen wiki (a committed one); when given, a link into the wiki
    directory is checked against them instead of the working tree.
    A source link's fragment must be a supported line fragment (``#L3`` or ``#L3-L9``); a page
    fragment must name a heading in `fragments` (see ``fragment_problem``; None skips that check).
    """
    res = resolve_href(
        link.href, file, wiki_dir, repo_identities=repo_identities, commit=snap.commit if snap else None
    )
    if link.html:
        if res.source_shaped:
            tag = "<img>" if link.image else "<a>"
            why = f"raw HTML {tag} pointing at source is not evidence (it renders as text)"
            return f"{why}; {HTML_SOURCE_HINT}: {link.href}"
        return None  # escaped when rendered
    if link.image:
        return image_problem(link, file, wiki_dir=wiki_dir, snap=snap, policy=policy)
    if res.kind == "external":
        return None
    if res.kind == "anchor":
        return fragment_problem(link, file, wiki_dir, fragments) if fragments else None
    if res.kind == "invalid":
        return res.problem
    if res.kind == "wiki":
        inner = res.inner or ""
        if policy.secret(f"{wiki_dir}/{inner}"):
            return f"links to a file excluded by the secret policy: {link.href}"
        exists = inner in wiki_files if wiki_files is not None else (wiki_path / inner).exists()
        if inner in (extra_pages or set()) or (exists and inner not in (removed or set())):
            return fragment_problem(link, file, wiki_dir, fragments) if fragments else None
        return f"broken link: {link.href}"
    assert res.target is not None
    target, lines = res.target.path, res.target.lines
    if policy.secret(target):
        return f"links to a file excluded by the secret policy: {link.href}"
    if lines is None and not link.external and link.fragment:
        return f"unsupported fragment on a source link (want #Lx or #Lx-Ly): {link.href}"
    if snap is None:
        return None
    at = snap.commit[:7]
    if snap.has_file(target):
        if lines is None:
            return None
        count = snap.line_count(target)
        a, b = lines
        if count is None:
            return f"cites lines in a binary file: {link.href}"
        if not 1 <= a <= b:
            return f"line range is empty or reversed: {link.href}"
        if b > count:
            return f"cites lines {a}-{b} but {target} has {count} lines at {at}: {link.href}"
        return None
    if target and snap.has_dir(target):
        return f"line range on a directory: {link.href}" if lines is not None else None
    return f"{target or '.'} does not exist at {at}: {link.href}"


def load_index(wiki: Path) -> object:
    raw = read_wiki_file(wiki, INDEX)
    if raw is None:
        raise FileNotFoundError(wiki / INDEX)
    return json.loads(raw)


def check_wiki(
    data: object,
    texts: dict[str, str],
    *,
    root: Path | None,
    wiki_dir: str,
    wiki: Path,
    snap: Snapshot | None,
    policy: Policy = DEFAULT_POLICY,
    removed: set[str] | None = None,
    min_citations: int = 0,
    repo_identities: tuple[RepoIdentity, ...] = (),
    wiki_files: set[str] | None = None,
) -> list[Problem]:
    """Every check for a whole wiki whose page texts are in `texts` (on disk or about to be written).
    `wiki_files` (a frozen wiki's files) replaces the working tree for links into the wiki directory."""
    probs, entries = check_index(data, root, wiki_dir=wiki_dir, policy=policy)
    names = {e.file for e in entries}
    for e in entries:
        text = texts.get(e.file)
        if text is None:
            probs.append(Problem(e.file, 0, "listed in index.json but missing"))
            continue
        probs += check_page(
            e.file,
            text,
            wiki_dir=wiki_dir,
            wiki_path=wiki,
            snap=snap,
            extra_pages=names,
            policy=policy,
            removed=removed,
            min_citations=min_citations,
            repo_identities=repo_identities,
            page_texts=texts,
            wiki_files=wiki_files,
        )
    return probs


def validate(
    root: Path,
    wiki_dir: str = "docs/wiki",
    *,
    policy: Policy = DEFAULT_POLICY,
    min_citations: int = 1,
    aliases: tuple[str, ...] = (),
) -> list[Problem]:
    """Validate the wiki in the working tree against the commit named in its index.

    Web URLs are source links when they point into the repository's ``origin`` web identity or one
    of ``aliases`` (registered web URLs of the same repository)."""
    try:
        wiki = wiki_path(root, wiki_dir)
    except UnsafePathError as exc:
        return [Problem(wiki_dir, 0, str(exc))]
    with wiki_lock(wiki):
        return _validate(root, wiki_dir, policy=policy, min_citations=min_citations, aliases=aliases)


def _validate(
    root: Path,
    wiki_dir: str = "docs/wiki",
    *,
    policy: Policy = DEFAULT_POLICY,
    min_citations: int = 1,
    aliases: tuple[str, ...] = (),
) -> list[Problem]:
    from .snapshot import read_frozen  # the one frozen validation reader (it builds on this module)

    frozen = read_frozen(
        root, wiki_dir, policy=policy, min_citations=min_citations, identities=repo_identities(root, aliases)
    )
    return frozen.problems
