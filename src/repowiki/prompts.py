"""Prompt templates.

Each prompt starts with a ``TASK:`` line and uses fixed section headings; the
offline fake backend (and anyone debugging a run) relies on that layout.
"""

from __future__ import annotations

from dataclasses import dataclass

from .survey import Survey


@dataclass
class PlannedPage:
    file: str
    title: str
    parent: str | None  # parent page file
    summary: str
    sources: list[str]


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


def plan_prompt(name: str, s: Survey, tree: str, min_pages: int, max_pages: int) -> str:
    readme = f"## README ({s.readme})\n{s.readme_text}" if s.readme else "## README\n(none)"
    listing = lambda items: "\n".join(f"- {i}" for i in items) or "(none found)"  # noqa: E731
    langs = ", ".join(f"{ext} ({n})" for ext, n in s.languages) or "(unknown)"
    return f"""TASK: plan
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

{readme}

## Manifests
{listing(s.manifests)}

## Entry points
{listing(s.entry_points)}

## Tests
{listing(s.tests[:60])}

## Languages
{langs}

## Files
{tree}
"""


def _outline(pages: list[PlannedPage]) -> str:
    return "\n".join(f"- {p.file}: {p.title}" + (f" (under {p.parent})" if p.parent else "") for p in pages)


def page_prompt(name: str, page: PlannedPage, pages: list[PlannedPage], excerpts: str, previous: str | None) -> str:
    prev = ""
    if previous:
        prev = (
            "\n## Previous version\nThe cited code changed since this was written. Keep what is still "
            f"true, fix what changed, re-check every line number.\n<<<PREVIOUS\n{previous}\nPREVIOUS>>>\n"
        )
    return f"""TASK: page
Write one page of the documentation wiki for the repository "{name}" as GitHub-flavoured
Markdown. Output ONLY the page, starting with "# {page.title}".

## Page
Title: {page.title}
File: {page.file}
Summary: {page.summary}

## Other pages
{_outline(pages)}

## Rules
{CITATION_RULES}
{prev}
## Source excerpts
{excerpts}
"""


def revise_prompt(name: str, page: PlannedPage, draft: str, excerpts: str) -> str:
    return f"""TASK: revise
You are the final editor of the page "{page.title}" in the documentation wiki for "{name}".
Improve the draft below: correct anything the source excerpts contradict, fix wrong line
numbers, tighten the prose and diagrams, keep every correct citation. Output ONLY the
finished page, starting with "# {page.title}".

## Rules
{CITATION_RULES}

## Draft
<<<DRAFT
{draft}
DRAFT>>>

## Source excerpts
{excerpts}
"""


def repair_prompt(name: str, page: PlannedPage, text: str, problems: list[str], excerpts: str) -> str:
    bullet = "\n".join(f"- {p}" for p in problems)
    return f"""TASK: repair
The page "{page.title}" of the wiki for "{name}" failed validation. Fix exactly these
problems (correct the path or line range from the excerpts, or remove the citation) and
output ONLY the corrected page, starting with "# {page.title}".

## Problems
{bullet}

## Rules
{CITATION_RULES}

## Page
<<<PAGE
{text}
PAGE>>>

## Source excerpts
{excerpts}
"""
