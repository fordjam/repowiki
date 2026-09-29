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
  files (or files under cited directories) changed.
- **No API keys.** Backends are the subscription CLIs you're already signed in
  to (Claude Code, opencode, Codex) or a local Ollama model. You can use a cheap
  model for the bulk of the work and a strong model for the final pass.
- **You can see what leaves your machine.** Backends are started with their
  tools switched off, files matching the secret policy are never put in a
  prompt, credential-shaped strings are redacted, and `--preview` lists every
  file and byte count each call would send, to which provider, before anything
  is sent. See [Privacy boundary](#privacy-boundary) for exactly what is and is
  not enforced.

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
it is signed in (for Ollama, with `--model ollama:MODEL`: the same loopback and
`/api/show` checks a real run makes).

```sh
repowiki doctor                      # every backend
repowiki doctor --model claude:haiku # just the one you plan to use
```

## Quickstart

```sh
cd ~/src/any-project                 # any git repository with at least one commit

repowiki generate --preview          # what would be sent where; sends nothing
repowiki generate --plan-only        # the page outline and a cost estimate (one model call)
repowiki generate --preview          # now lists every page's files and bytes too
repowiki generate                    # plan, write, validate -> docs/wiki/
repowiki validate                    # re-check at any time (good in CI)
git add docs/wiki && git commit -m "docs: add wiki"

# later, after the code has moved on
repowiki update --dry-run            # which pages cite changed files, and what would be sent?
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
min_citations = 1            # line citations every page must contain
exclude = ["internal/*"]     # extra secret-policy patterns (never sent, never cited)
budget_tokens = 400000       # stop before a run's prompts exceed ~this many tokens

[models]
cheap = "claude:haiku"
strong = "claude:opus"
```

`repowiki site` reads the same file, so a custom `wiki_dir` or `exclude` applies
there too.

## Backends

| Backend  | Spec example                | What runs | Cost notes |
|----------|-----------------------------|-----------|------------|
| Claude Code | `claude`, `claude:haiku` | `claude -p --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands --no-session-persistence --model M`, prompt on stdin | Counts against your Claude plan (or whatever auth the CLI uses). |
| opencode | `opencode:provider/model`   | `opencode run --pure --agent repowiki-<random> -m provider/model` with a generated config (below) | Uses providers you signed in to with `opencode auth login` (or `<PROVIDER>_API_KEY`). |
| Codex    | `codex`, `codex:MODEL`      | `codex exec --sandbox read-only --ephemeral --ignore-user-config --ignore-rules --disable shell_tool ... -m M -` | Counts against your ChatGPT plan quota. |
| Ollama   | `ollama:MODEL`              | `ollama run MODEL` against a loopback server that reports the model as local | Free. Quality and speed depend on your hardware. |
| fake     | `fake`                      | Built in | Free. Placeholder prose with valid citations. Used by the tests and CI. |

To point at a binary that isn't on `PATH`, set `REPOWIKI_CLAUDE_BIN`,
`REPOWIKI_OPENCODE_BIN`, `REPOWIKI_CODEX_BIN` or `REPOWIKI_OLLAMA_BIN`.

A generate run makes 1 planning call, then 1 call per page (2 when the strong
model is different), plus 1 repair call for each page that fails validation.
Calls that time out or exit non-zero are retried twice with exponential backoff
(`retries`, `backoff`); a missing binary or a refused setup fails at once.

## Privacy boundary

What a backend receives from repowiki is the prompt text; `--preview` lists
the repository files (and byte counts) each prompt is built from. This section
lists what repowiki enforces around that, and what it does not.

**Enforced by repowiki** (each has a regression test):

- **Backend flags.** Each call is a new process started in an empty temporary
  directory with the flags below. repowiki checks the flags it passes, and
  `repowiki doctor` checks that your CLI version still accepts them; it cannot
  check what a CLI does with them.
  - Claude Code: `--tools ""` (no built-in tools), an empty MCP config with
    `--strict-mcp-config`, `--setting-sources ""` (no user, project or local
    settings, so no hooks or plugins from them), `--disable-slash-commands`,
    `--no-session-persistence`, and `CLAUDE.md` files and auto-memory disabled
    through environment variables.
  - opencode: each call runs a private agent whose name is random per call,
    with every permission denied and every tool off, set through
    `OPENCODE_CONFIG_CONTENT`; the built-in agents are locked the same way, and
    global permissions are denied too. The config home is an empty temporary
    directory, so your global `opencode.json` (its agents, MCP servers, plugins,
    instructions and custom providers) is not loaded; project config, Claude
    Code skills and default plugins are off; `--pure` skips external plugins;
    sessions go to an in-memory database. A test runs the real
    `opencode debug agent` against hostile global, project and inherited
    configs and checks that every permission resolves to deny.
  - Codex: `--sandbox read-only`, `--ephemeral`, `--ignore-user-config` (your
    `config.toml`, including MCP servers, is not loaded; pass the model in the
    spec), `--ignore-rules`, web search configured off, and the shell, unified
    exec, apps, plugins, hooks, browser, computer-use, multi-agent, image and
    view-image features disabled.
  - Ollama: `ollama run` has no tools. `OLLAMA_HOST` is resolved once to
    `scheme://host:port` (a path, query, credentials or another scheme is
    refused); the host must be a loopback IP address or `localhost` resolving
    only to loopback addresses (a name such as `127.remote.example` is
    refused); the model must not have a cloud tag; and `/api/show` on that same
    endpoint must list the model with no remote host. The `ollama` process gets
    exactly that endpoint. `--allow-remote` skips these checks.
- **Minimal environment.** Backends get `PATH`, `HOME`, locale, temp and XDG
  directories, proxy and CA settings, plus the variables their own sign-in
  needs (for example `ANTHROPIC_*`, `CODEX_HOME`, or `<PROVIDER>_API_KEY` for
  the opencode provider you picked). Nothing else from your shell, unless you
  list names in `REPOWIKI_PASS_ENV`.
- **Secret policy, by file name.** Files matching the policy are never read
  into a prompt, never accepted as a citation, never linked or exported by the
  site, and never used as wiki pages (a page like `agent-transcript.md` listed
  in `index.json` fails validation, and `site` and `update` refuse to run). The
  patterns: `.env*`, `*.pem`, `*.key` and other key or keystore files, `id_*`
  SSH keys, `.npmrc`, `.pypirc`, `.netrc` and similar credential files,
  `secrets*`, `credentials*`, Terraform state and variables, AI assistant
  transcripts and session folders (`.claude/`, `.codex/`, `.opencode/`,
  `.specstory/`, `transcripts/`, `*transcript*.jsonl|json|md|txt|log|html`),
  and credential folders such as `.ssh/` and `.aws/`. Add globs with
  `exclude`. The full list is in [`src/repowiki/policy.py`](src/repowiki/policy.py).
- **Redaction, by content.** Credential-shaped strings (common API-token
  formats, bearer tokens, private-key blocks, and quoted literals assigned to
  names like `api_key`, `secret`, `token` or `password`) are replaced with
  `[redacted by repowiki]` in source excerpts, the README sent to the planner,
  previous page versions sent by `update`, model replies, rendered pages and
  exported source pages. Line numbers do not change.
- **Filesystem.** repowiki writes only inside the wiki directory (a relative
  path in the repository, not through a symlink that leads out of it), only to
  plain page names, never through symlinks, and deletes a page dropped from
  the index only if it carries the marker repowiki writes into every page.
  Checkpoints live in `.git/repowiki/`.

**Not enforced (residual risks):**

- What a provider does with a prompt once it has it.
- Secrets that match neither a file pattern nor a redaction pattern, for
  example a password in an ordinary source file that is not a quoted literal
  assigned to a recognisable name. Review `--preview` and add `exclude` globs.
- What a backend CLI does beyond the flags above. In particular, Codex's
  `read-only` sandbox still allows reading any file your user account can
  read, so if a Codex version added a tool that repowiki does not know to
  disable, that tool could read outside the temporary directory (it could not
  write files or, by default, reach the network). A future opencode or Claude
  Code release could likewise change what these flags mean; rerun
  `repowiki doctor` after upgrading.
- With `--allow-remote`, Ollama prompts may go to a remote server or Ollama's
  cloud; the Ollama server itself is trusted to report `/api/show` honestly.
- opencode sign-ins stored by `opencode auth login` are used; providers or
  plugins defined only in your global opencode config are not available.

## Cost, budgets and resuming

- `--plan-only` makes the planning call and prints the outline and an estimate
  for the pages: calls and approximate prompt tokens (characters / 4), plus an
  estimated scenario in which every page needs a repair call. Retries are not
  included in either figure (the `--budget` meter does count them). Prompt
  sizes come from the real prompts; the final-pass and repair sizes assume a
  typical draft.
- `--budget TOKENS` (or `budget_tokens`) stops a run before drafting when the
  estimate is over budget, and stops it mid-run before any call that would
  exceed it. Subscription CLIs do not report per-call usage, so this counts the
  prompts repowiki sends, not the provider's billing.
- Every finished outline and page is checkpointed in `.git/repowiki/checkpoints`,
  keyed by the models and the exact prompt. If a run fails or hits the budget,
  run the same command again: finished pages are reused and only the rest are
  written. `--fresh` ignores checkpoints; `checkpoints = false` turns them off.

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
the page cites and the files whose lines its prompt contained, and ends with
an HTML comment, `<!-- generated by repowiki -->`. Citations are ordinary relative Markdown links with a line
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
   files with line numbers, up to `context_chars` (60,000 by default). Blob
   sizes are checked first and no more than the remaining budget is ever read
   from a file. The model is told to cite only lines it can see.
3. **Relinking.** The model writes repo-root paths (`src/a.py#L3-L9`). repowiki
   rewrites them into links relative to the page (`../../src/a.py#L3-L9`) and
   rebuilds the "Relevant source files" block from the links the page actually
   contains plus the files the model was shown (not files the budget cut).
4. **Validation** (`repowiki validate`, and automatically at the end of
   `generate` and `update`):
   - `index.json` parses and matches the schema. `indexed_commit` is a commit
     in the repository, `indexed_at` is ISO 8601, page files are plain `.md`
     names, and parents exist and do not form cycles.
   - Every page listed in the index exists (and is not a symlink), starts
     with `# `, has the sources block, and has at least `min_citations` line
     citations (default 1; `--min-citations 0` turns this off).
   - Every relative link resolves. Links to wiki pages must exist on disk.
     Links to anything else must exist at `indexed_commit`.
   - Every `#Lx` or `#Lx-Ly` range falls inside the cited file at
     `indexed_commit`, is not reversed, and does not point into a binary file
     or a directory.
   - Links inside code blocks are ignored. Links that escape the repository,
     and links to files covered by the secret policy, are errors.
5. **Repair.** If a page fails validation, the cheap model gets the list of
   problems and one attempt to fix them. Any link that still fails after that
   is turned into plain text (inline links, reference-style links and their
   definitions alike), and repowiki prints a warning. A page that still fails,
   for example one with too few citations, fails the run.
6. **Publish.** The complete new wiki is validated in memory first. Only if it
   passes is it published, as one transaction: new pages and `index.json` are
   staged inside the wiki directory, a journal records what existed, and each
   original is moved aside before its replacement moves in. Any error rolls
   every change back; if the process dies mid-publish, the next `generate`,
   `update`, `validate` or `site` rolls it back. A failed run leaves the
   previous wiki and its `indexed_commit` exactly as they were.

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
  {"name": "Example Lib", "path": "~/src/example-lib", "branch": "main", "ref": "main", "export_sources": true}
]}
```

`url` must be a plain `http(s)://` URL; anything else is rejected. `wiki_dir`
defaults to the repository's own `.repowiki.toml`.

- Mermaid code blocks render as diagrams. By default, mermaid loads from the
  jsDelivr CDN in the reader's browser. To build a fully offline site, pass
  `--mermaid path/to/mermaid.min.js`: the file is copied into the site, and you
  must ship mermaid's licence with it.
- `#L` citations render as chips (`cli.py L6-9`). When a repository has a web
  `url` (detected from `origin` when you pass paths), chips link to the host's
  blob view at `indexed_commit`, so the lines stay correct as the code changes.
  GitHub and GitLab URL styles are supported. Without a `url`, citations are
  plain text unless you opt in with `--export-sources` (or `"export_sources":
  true`): then repowiki renders each cited file at `indexed_commit` as a local
  page with line anchors, copies every `LICENSE`, `NOTICE`, `COPYING`,
  `AUTHORS` or `COPYRIGHT` file found at the root or above an exported file,
  links them from each source page, and warns if there are none. Publishing
  someone else's code is your responsibility; check its licence first.
- The **staleness badge** shows how many commits are on `main` (or the
  configured `branch`, or `HEAD` if that is missing) since `indexed_commit`.
  Commits that only touch the wiki directory are not counted. 0 is neutral,
  1 to 50 is amber, and more than 50 is red.
- By default the site reads each wiki from the working tree. Set `ref`, or pass
  `--ref main`, to publish only what is committed on a branch.
- Raw HTML in pages is limited to a few attribute-free tags (`details`,
  `summary`, `kbd`, ...). Anything else is escaped. Links and images must be
  relative, `#anchors`, `http(s)` or `mailto`; any other scheme renders as text.
- The site is built in a temporary directory next to the output and swapped in
  when it is complete; the previous site is deleted only after that.

## Limits

- **Quality depends on the model.** Validation proves that citations point at
  real lines. It cannot prove the prose about those lines is right. Read the
  pages before you publish them.
- **Citations are counted, not weighed.** A page needs `min_citations` line
  citations, but repowiki does not check that every claim has one, or that
  a citation supports the sentence it follows.
- **Context is per page.** Each page sees its planned files, up to about
  60,000 characters. repowiki is not a retrieval system, and very large files
  are truncated.
- **`update` follows citations.** A page is re-written when a file it cites,
  or a file under a directory it cites, changes; renamed files are followed.
  New subsystems that no page cites are not picked up, and freshness is tracked
  for the whole wiki, not per page; run `generate` again to re-plan.
- **Token counts are estimates** (characters / 4), not provider billing.
- **Heading anchors are not checked.** Same-page `#anchor` links and fragments
  on page links are not validated.
- **Images are not copied.** Images in the wiki directory are not copied into
  the static site.
- **No search.** The static site has no search yet.
- Only GitHub-style and GitLab-style web links are supported.

See [ROADMAP.md](ROADMAP.md) for what is planned.

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
ruff check src tests && mypy src
```

## Licence

MIT. See [LICENSE](LICENSE).
