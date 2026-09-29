"""Offline answers for repowiki's own prompts (the ``fake`` backend).

It reads the file list or excerpts embedded in a prompt and returns a plan or
a page with real, in-range citations. Useful for tests, CI and trying the
site renderer without spending any model quota. The prose is placeholder text.
"""

from __future__ import annotations

import json
import re

EXCERPT_RE = re.compile(r"^=== (?P<path>.+?) \(lines 1-(?P<shown>\d+) of (?P<total>\d+)\) ===$", re.M)


def _section(prompt: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", prompt, re.S | re.M)
    return m.group(1) if m else ""


def _between(prompt: str, tag: str) -> str | None:
    m = re.search(rf"<<<{tag}\n(.*?)\n{tag}>>>", prompt, re.S)
    return m.group(1) if m else None


def answer(prompt: str) -> str:
    task = prompt.split("\n", 1)[0].removeprefix("TASK:").strip()
    if task == "plan":
        return _plan(prompt)
    if task == "revise":
        return _between(prompt, "DRAFT") or _page(prompt)
    if task == "repair":
        return _between(prompt, "PAGE") or _page(prompt)
    return _page(prompt)


def _plan(prompt: str) -> str:
    from .generate import heuristic_outline

    files = [re.sub(r" \([^)]*\)$", "", ln).strip() for ln in _section(prompt, "Files").splitlines()]
    files = [f for f in files if f and not f.endswith("more files")]
    readme = re.search(r"^## README \((.+?)\)$", prompt, re.M)
    entries = [ln[2:] for ln in _section(prompt, "Entry points").splitlines() if ln.startswith("- ")]
    tests = [ln[2:] for ln in _section(prompt, "Tests").splitlines() if ln.startswith("- ")]
    limit = re.search(r"Between (\d+) and (\d+) pages", prompt)
    max_pages = int(limit.group(2)) if limit else 8
    outline = heuristic_outline(files, readme.group(1) if readme else None, entries, tests, max_pages)
    return "```json\n" + json.dumps({"pages": outline}, indent=2) + "\n```"


def _page(prompt: str) -> str:
    title = re.search(r"^Title: (.+)$", prompt, re.M)
    name = re.search(r'repository "([^"]+)"', prompt)
    heading = title.group(1) if title else "Page"
    lines = [f"# {heading}", ""]
    lines.append(f"This page describes {heading.lower()} in {name.group(1) if name else 'the repository'}.")
    lines.append("")
    cited = []
    for m in EXCERPT_RE.finditer(prompt):
        path, shown = m.group("path"), int(m.group("shown"))
        if shown < 1:
            continue
        end = min(shown, 20)
        cited.append(f"[{path}:1-{end}]({path}#L1-L{end})")
        lines += [f"## {path}", "", f"`{path}` has {m.group('total')} lines.", "", f"Sources: {cited[-1]}", ""]
    if cited:
        lines += ["## Diagram", "", "```mermaid", "graph TD"]
        paths = [m.group("path") for m in EXCERPT_RE.finditer(prompt)][:5]
        lines += [f'  P["{heading}"] --> F{i}["{path}"]' for i, path in enumerate(paths)]
        lines += ["```", ""]
    return "\n".join(lines)
