"""Prompt templates.

Each prompt starts with a ``TASK:`` line and uses fixed section headings; the
offline fake backend (and anyone debugging a run) relies on that layout.

Templates return origin-tagged sections, never a finished string: only the
outbound gateway (``outbound.seal``) serializes, filters and redacts them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .outbound import Section, template
from .survey import Survey


@dataclass
class PlannedPage:
    file: str
    title: str
    parent: str | None  # parent page file
    summary: str
    sources: list[str]
    note: str = ""  # extra context for an update, e.g. files renamed since the previous version


CITATION_RULES = """\
- Cite the code for every non-trivial claim with a Markdown link whose target is the
  repository-root path plus a line range, e.g. [cli.py:12-40](src/tool/cli.py#L12-L40).
  Use only line numbers shown in the excerpts below. Never invent paths or lines.
- End each major section with a line "Sources: " followed by its citations.
- Link to other wiki pages by their file name, e.g. [Architecture](2-architecture.md).
- Use ```mermaid fenced blocks for diagrams where they help (architecture, data flow,
  sequences). Keep node labels short and quote labels containing punctuation.
- Describe what the code does today. If something is not implemented, say so; do not guess.
- Do not include credentials, tokens, personal data or absolute local paths.
- Do not add a "Relevant source files" block; repowiki adds it."""


def _items(origin: str, items: list[str]) -> list[Section]:
    """A "- item" list, one section per path so each carries its own name."""
    if not items:
        return [template("(none found)")]
    out: list[Section] = []
    for n, item in enumerate(items):
        out.append(Section(origin, ("\n" if n else "") + f"- {item}", (item,)))
    return out


def plan_prompt(
    name: str, s: Survey, tree: list[tuple[str, tuple[str, ...]]], min_pages: int, max_pages: int
) -> list[Section]:
    """`tree` is the file listing as (line, paths named by that line)."""
    langs = ", ".join(f"{ext} ({n})" for ext, n in s.languages) or "(unknown)"
    out = [template(f"""TASK: plan
You are planning a documentation wiki for the git repository "{name}", in the style of
a code wiki: an overview, architecture, one page per major subsystem, then data flow,
operations, testing and a glossary where the code justifies them.

Return ONLY a JSON object, no prose, in this shape:
{{"pages": [{{"title": "Overview", "parent": null, "summary": "one sentence", "sources": ["README.md"]}}]}}

Rules:
- Between {min_pages} and {max_pages} pages. The first page is the Overview, with parent null.
- "parent" is null or the exact title of an earlier page (use it for subsystem sub-pages).
- "sources" lists 1-8 repository paths from the file list below that the page should
  read and cite. Prefer real source files over configuration.
- Titles are short (1-4 words) and unique.

""")]
    if s.readme:
        out.append(Section("readme", f"## README ({s.readme})\n{s.readme_text}", (s.readme,)))
    else:
        out.append(template("## README\n(none)"))
    out.append(template("\n\n## Manifests\n"))
    out += _items("path", s.manifests)
    out.append(template("\n\n## Entry points\n"))
    out += _items("path", s.entry_points)
    out.append(template("\n\n## Tests\n"))
    out += _items("path", s.tests[:60])
    out.append(template(f"\n\n## Languages\n{langs}\n\n## Files\n"))
    for n, (line, paths) in enumerate(tree):
        out.append(Section("path", ("\n" if n else "") + line, paths))
    out.append(template("\n"))
    return out


@dataclass(frozen=True)
class PageOrigin:
    """Where a page's own title, file name and summary come from, and the wiki path they describe."""

    origin: str = "planner_reply"  # or "previous_wiki" for an update
    wiki_dir: str | None = None  # set for previous_wiki: the page is the file wiki_dir/page.file

    def paths(self, page: PlannedPage) -> tuple[str, ...]:
        return (f"{self.wiki_dir.rstrip('/')}/{page.file}",) if self.wiki_dir else ()

    def field(self, page: PlannedPage, text: str) -> Section:
        return Section(self.origin, text, self.paths(page))


PLANNER = PageOrigin()


def _outline(pages: list[PlannedPage], where: PageOrigin) -> list[Section]:
    out: list[Section] = []
    for n, p in enumerate(pages):
        line = f"- {p.file}: {p.title}" + (f" (under {p.parent})" if p.parent else "")
        parent = [f"{where.wiki_dir.rstrip('/')}/{p.parent}"] if where.wiki_dir and p.parent else []
        out.append(Section("outline", ("\n" if n else "") + line, where.paths(p) + tuple(parent)))
    return out


def _excerpts(excerpts: str, sources: tuple[str, ...]) -> Section:
    return Section("source", excerpts, sources)


def page_prompt(
    name: str,
    page: PlannedPage,
    pages: list[PlannedPage],
    excerpts: str,
    previous: str | None,
    *,
    sources: tuple[str, ...] = (),
    where: PageOrigin = PLANNER,
) -> list[Section]:
    """`sources` are the files the excerpts quote; `where` tags the page fields and outline."""
    out = [
        template(f'TASK: page\nWrite one page of the documentation wiki for the repository "{name}" '
                 'as GitHub-flavoured\nMarkdown. Output ONLY the page, starting with "# '),
        where.field(page, page.title),
        template('".\n\n## Page\nTitle: '),
        where.field(page, page.title),
        template("\nFile: "),
        where.field(page, page.file),
        template("\nSummary: "),
        where.field(page, page.summary),
        template("\n\n## Other pages\n"),
        *_outline(pages, where),
        template(f"\n\n## Rules\n{CITATION_RULES}\n"),
    ]
    if previous:
        out += [
            template("\n## Previous version\nThe cited code changed since this was written. Keep what is still "
                     "true, fix what changed, re-check every line number.\n"),
            Section("path", page.note),
            template("<<<PREVIOUS\n"),
            Section("previous_wiki", previous, where.paths(page)),
            template("\nPREVIOUS>>>\n"),
        ]
    out += [template("\n## Source excerpts\n"), _excerpts(excerpts, sources), template("\n")]
    return out


def _title_header(page: PlannedPage, where: PageOrigin, before: str, after: str) -> list[Section]:
    return [template(before), where.field(page, page.title), template(after)]


def revise_prompt(
    name: str, page: PlannedPage, draft: str, excerpts: str, *,
    sources: tuple[str, ...] = (), where: PageOrigin = PLANNER,
) -> list[Section]:
    return [
        *_title_header(page, where, 'TASK: revise\nYou are the final editor of the page "',
                       f'" in the documentation wiki for "{name}".\n'
                       "Improve the draft below: correct anything the source excerpts contradict, fix wrong line\n"
                       "numbers, tighten the prose and diagrams, keep every correct citation. Output ONLY the\n"
                       'finished page, starting with "# '),
        where.field(page, page.title),
        template(f'".\n\n## Rules\n{CITATION_RULES}\n\n## Draft\n<<<DRAFT\n'),
        Section("draft", draft),
        template("\nDRAFT>>>\n\n## Source excerpts\n"),
        _excerpts(excerpts, sources),
        template("\n"),
    ]  # fmt: skip


def repair_prompt(
    name: str, page: PlannedPage, text: str, problems: list[str], excerpts: str, *,
    sources: tuple[str, ...] = (), where: PageOrigin = PLANNER,
) -> list[Section]:
    bullet = "\n".join(f"- {p}" for p in problems)
    return [
        *_title_header(page, where, 'TASK: repair\nThe page "',
                       f'" of the wiki for "{name}" failed validation. Fix exactly these\n'
                       "problems (correct the path or line range from the excerpts, or remove the citation) and\n"
                       'output ONLY the corrected page, starting with "# '),
        where.field(page, page.title),
        template('".\n\n## Problems\n'),
        Section("validation", bullet),
        template(f"\n\n## Rules\n{CITATION_RULES}\n\n## Page\n<<<PAGE\n"),
        Section("draft", text),
        template("\nPAGE>>>\n\n## Source excerpts\n"),
        _excerpts(excerpts, sources),
        template("\n"),
    ]  # fmt: skip
