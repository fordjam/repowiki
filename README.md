# repowiki

Generate a cited, validated Markdown wiki for any git repository, using the
coding-assistant CLI you already have, and render it as a static site.

repowiki is inspired by Devin's DeepWiki: an overview, architecture and subsystem
pages, diagrams, and a citation on every claim that links to the exact source
lines. repowiki is a separate project. It runs on your machine, writes plain
Markdown into your repository (`docs/wiki/`), and checks every citation before
it finishes.

## Why

- **The docs live in the repo.** Pages are Markdown next to the code. They are
  reviewed in pull requests and read on any git host, with no service involved.
- **Citations are checked.** Every relative link has to resolve. Every cited file
  and line range has to exist at the commit the wiki was indexed from. Validation
  catches invented paths and line numbers before a reader does.
- **Updates cost little.** `repowiki update` re-writes only the pages whose cited
  files changed.
- **No API keys.** Backends are the subscription CLIs you're already signed in
  to (Claude Code, opencode, Codex) or a local Ollama model. You can use a cheap
  model for the bulk of the work and a strong model for the final pass.

## Install

Requires Python 3.11+ and git.

```sh
pip install git+https://github.com/OWNER/repowiki
# or, with pipx:  pipx install git+https://github.com/OWNER/repowiki
# or, from a checkout:  pip install .
```

You also need at least one backend CLI on your `PATH`, signed in: `claude`,
`opencode`, `codex` or `ollama`. `repowiki doctor` checks each one without
sending a prompt: that it is installed (or where `REPOWIKI_<NAME>_BIN` points),
its version, that it supports the isolation flags repowiki relies on, and that
it is signed in (for Ollama: that the server is local and reachable).

```sh
repowiki doctor                      # every backend
repowiki doctor --model claude:haiku # just the one you plan to use
```

## Quickstart

```sh
cd ~/src/any-project                 # any git repository with at least one commit

repowiki generate --plan-only        # preview the page outline (one model call)
repowiki generate                    # plan, write, validate -> docs/wiki/
repowiki validate                    # re-check at any time (good in CI)
git add docs/wiki && git commit -m "docs: add wiki"

# later, after the code has moved on
repowiki update --dry-run            # which pages cite changed files?
repowiki update                      # re-write just those pages

repowiki site -o site                # static HTML in ./site
python3 -m http.server -d site       # browse at http://localhost:8000
```

To try the whole pipeline offline without spending any quota, use
`--model fake`. The pages contain placeholder text but real citations.

### Choosing models

A model is a spec string `backend[:model]`. Only the first `:` separates the two
parts, so `ollama:qwen3:8b` works.

```sh
repowiki generate --model claude:sonnet                       # one model for everything
repowiki generate --cheap claude:haiku --strong claude:opus   # draft cheap, finish strong
repowiki generate --cheap ollama:qwen3:8b --strong codex      # local drafts, hosted final pass
```

The **cheap** model plans the outline, drafts every page and repairs pages that
fail validation. The **strong** model gives each page a final editing pass. If
you set only one of them, it does every step. If you set neither, repowiki uses
`claude` with that CLI's default model.

You can also put these settings in `.repowiki.toml` at the repository root
(see [`examples/repowiki.toml`](examples/repowiki.toml)):

```toml
wiki_dir = "docs/wiki"
max_pages = 12

[models]
cheap = "claude:haiku"
strong = "claude:opus"
```

## Backends

| Backend  | Spec example                | What runs                                         | Cost notes |
|----------|-----------------------------|---------------------------------------------------|------------|
| Claude Code | `claude`, `claude:haiku` | `claude -p --model M`, prompt on stdin             | Counts against your Claude plan (or whatever auth the CLI uses). A small model is a good fit for drafts and a larger one for the final pass. |
| opencode | `opencode:provider/model`   | `opencode run -m provider/model`                   | Uses any provider opencode is configured for, including low-cost coding plans and free tiers. Often the cheapest hosted option for bulk drafting. |
| Codex    | `codex`, `codex:MODEL`      | `codex exec -m M -` in a read-only sandbox        | Counts against your ChatGPT plan quota. |
| Ollama   | `ollama:MODEL`              | `ollama run MODEL`                                 | Free and local. Quality and speed depend on your hardware. Best for drafts of small repositories. |
| fake     | `fake`                      | Built in                                           | Free. Placeholder prose with valid citations. Used by the tests and CI. |

Every call is a separate process with the whole context in the prompt, run in an
empty temporary directory, so backends do not browse or change your repository.
To point at a binary that isn't on `PATH`, set `REPOWIKI_CLAUDE_BIN`,
`REPOWIKI_OPENCODE_BIN`, `REPOWIKI_CODEX_BIN` or `REPOWIKI_OLLAMA_BIN`.

A generate run makes 1 planning call, then 1 call per page (2 when the strong
model is different), plus 1 repair call for each page that fails validation.

## What gets written

```
docs/wiki/
  index.json          {"repo", "indexed_commit", "indexed_at", "pages": [{"file", "title", "parent"}]}
  1-overview.md
  2-architecture.md
  ...
```

Each page starts with `# Title`, followed by a
`<details><summary>Relevant source files</summary>` block that lists the files
the page cites. Citations are ordinary relative Markdown links with a line
range, so they also work when you browse the repository on a git host:

```markdown
The CLI parses arguments and hands off to the engine
([cli.py:6-9](../../src/example/cli.py#L6-L9)).
```

`parent` holds another page's file name, or `null` for a top-level page.

## How citations and validation work

1. **Pinned snapshot.** repowiki reads every file from git at one commit (by
   default `HEAD`), never from the working tree, and records that commit as
   `indexed_commit`. Uncommitted edits do not leak into the wiki, and line
   numbers refer to a fixed version of the code.
2. **Line-numbered excerpts.** Each page prompt contains the planned source
   files with line numbers. The model is told to cite only lines it can see.
3. **Relinking.** The model writes repo-root paths (`src/a.py#L3-L9`). repowiki
   rewrites them into links relative to the page (`../../src/a.py#L3-L9`) and
   rebuilds the "Relevant source files" block from the links the page actually
   contains.
4. **Validation** (`repowiki validate`, and automatically at the end of
   `generate` and `update`):
   - `index.json` parses and matches the schema. `indexed_commit` is a commit
     in the repository, `indexed_at` is ISO 8601, page files are plain `.md`
     names, and parents exist and do not form cycles.
   - Every page listed in the index exists, starts with `# `, and has the
     sources block.
   - Every relative link resolves. Links to wiki pages must exist on disk.
     Links to anything else must exist at `indexed_commit`.
   - Every `#Lx` or `#Lx-Ly` range falls inside the cited file at
     `indexed_commit`, is not reversed, and does not point into a binary file
     or a directory.
   - Links inside code blocks are ignored. Links that escape the repository
     are errors.
5. **Repair.** If a page fails validation, the model gets the list of problems
   and one attempt to fix them. Any link that still fails after that is turned
   into plain text, and repowiki prints a warning. The wiki on disk always
   passes validation.

## The static site

```sh
repowiki site                                    # the repository you are in
repowiki site ../app ../lib -o site              # several repositories
repowiki site --repos repos.json -o site         # from a config file
```

`repos.json` (see [`examples/repos.json`](examples/repos.json)) is a list of
repositories. Relative paths resolve from the file's own directory:

```json
{"repos": [
  {"name": "Example App", "path": "../example-app", "url": "https://github.com/example/example-app"},
  {"name": "Example Lib", "path": "~/src/example-lib", "branch": "main", "ref": "main"}
]}
```

- Mermaid code blocks render as diagrams. By default, mermaid loads from the
  jsDelivr CDN in the reader's browser. To build a fully offline site, pass
  `--mermaid path/to/mermaid.min.js`: the file is copied into the site, and you
  must ship mermaid's licence with it.
- `#L` citations render as chips (`cli.py L6-9`). When a repository has a web
  `url` (detected from `origin` when you pass paths), chips link to the host's
  blob view at `indexed_commit`, so the lines stay correct as the code changes.
  GitHub and GitLab URL styles are supported. Without a `url`, repowiki renders
  the cited files at `indexed_commit` as local pages with line anchors.
- The **staleness badge** shows how many commits are on `main` (or the
  configured `branch`, or `HEAD` if that is missing) since `indexed_commit`.
  Commits that only touch the wiki directory are not counted. 0 is neutral,
  1 to 50 is amber, and more than 50 is red.
- By default the site reads each wiki from the working tree. Set `ref`, or pass
  `--ref main`, to publish only what is committed on a branch.
- Raw HTML in pages is limited to a few attribute-free tags (`details`,
  `summary`, `kbd`, ...). Anything else is escaped.

## Limits

- **Quality depends on the model.** Validation proves that citations point at
  real lines. It cannot prove the prose about those lines is right. Read the
  pages before you publish them.
- **Context is per page.** Each page sees its planned files, up to about
  60,000 characters. repowiki is not a retrieval system, and very large files
  are truncated.
- **`update` follows citations.** A page is re-written only when a file it
  cites changes. New subsystems that no page cites are not picked up; run
  `generate` again to re-plan.
- **Heading anchors are not checked.** Same-page `#anchor` links and fragments
  on page links are not validated.
- **Images are not copied.** Images in the wiki directory are not copied into
  the static site.
- **No search.** The static site has no search yet.
- Only GitHub-style and GitLab-style web links are supported.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
pytest
```

## Licence

MIT. See [LICENSE](LICENSE).
