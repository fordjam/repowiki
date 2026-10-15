# Roadmap

Planned work, roughly in priority order. None of this is implemented yet.

## Evidence-backed documentation

Validation today proves that citations point at real lines and that each page
has at least `min_citations` of them. It does not show which statements are
supported.

- Keep a claim-to-excerpt map for every page: which sentence cites which lines.
- Measure citation coverage per page and per section, and report paragraphs
  with no citation.
- Check that a retained claim's citation supports it. Repair only
  requires a new valid citation on each repaired claim, or its removal.
- Offer a strict mode that fails on uncited paragraphs, and mark unsupported
  statements in the static site.
- Record provenance in `index.json` or a sidecar file: backend, model, prompt
  version and repowiki version for each page.

## Semantic incremental updates

`update` re-writes pages whose cited files, or files under cited directories,
changed, and follows renames. It does not understand the code.

- Track symbols and import dependencies, so a page is refreshed when code it
  describes moves or a dependency's interface changes.
- Notice new subsystems that no page covers and propose new pages.
- Track freshness per page (the commit each page was last checked against)
  instead of one `indexed_commit` for the whole wiki, and show it in the site.

## Editable plans and smoother onboarding

- Let `generate` and `update` follow the committed steering file
  (`.repowiki/wiki.json`, already validated by `repowiki.steering`) instead of
  re-planning: exact page IDs, files, parents, notes and coverage.
- Show planned coverage before drafting: which directories and files no page
  will read.
- Extend `repowiki doctor` with a minimal real call per backend (opt-in, since
  it spends quota) and with known-good version ranges, so a CLI upgrade that
  changes output formats is caught before a long run.
