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
pip install git+https://github.com/fordjam/repowiki
# or, with pipx:  pipx install git+https://github.com/fordjam/repowiki
# or, from a checkout:  pip install .
```

You also need at least one backend CLI on your `PATH`, signed in: `claude`,
`opencode`, `codex` or `ollama`. `repowiki doctor` checks each one without
sending a prompt: that it is installed (or where `REPOWIKI_<NAME>_BIN` points),
its version, that it supports the isolation flags repowiki relies on, that the
isolation is actually effective on that binary, and that it is signed in (for
Ollama, with `--model ollama:MODEL`: the same loopback and `/api/show` checks a
real run makes).

Advertised flags are not enough. For a backend to be used, `doctor` must
*establish* the exact binary: every required flag and capability is present
(for Codex, every feature repowiki disables must be one that codex knows), and
effective checks with positive controls pass: a synthetic hostile home
directory (instruction files, a plugin, MCP servers, permissive settings) is
shown to load without repowiki's isolation and to be absent with it. A binary
that passes is recorded (resolved path, size, SHA-256 digest and version) in
`$XDG_STATE_HOME/repowiki/compatibility` (or `~/.local/state/...`, owner-only),
and every call rechecks the binary against that record before a prompt is
sent: an unknown, replaced or upgraded binary is refused until you run
`repowiki doctor` again. Inspecting configuration is not enough either: a
backend must also show *text-only behaviour*. `doctor` proves that only with
the provider canary, which you opt in to per backend with
`REPOWIKI_PROVIDER_CANARY=opencode`: it sends one synthetic prompt (no
repository content) asking for a tool call, first without isolation (the
hostile plugin and MCP server must start and a session must be saved, or
nothing is proven) and then exactly as repowiki sends prompts (nothing may
start, no tool may run, no inherited instruction may surface, no session may
be saved). Today opencode can be established this way (its `debug config` and
`debug agent` commands show the effective configuration); Ollama needs no tool
checks; Claude Code and Codex have no such checks yet and stay disabled.

```sh
repowiki doctor                      # every backend
repowiki doctor --model claude:haiku # just the one you plan to use
```

## Quickstart

Generation, planning, previews, and updates require an explicitly selected,
committed privacy policy and an operator approval outside the checkout, including
when using `fake`. Complete [Repository approval](#repository-approval) first.
The commands below use the approved offline fake backend, which produces
placeholder prose. Subscription CLI execution is currently blocked pending
endpoint-aware adapters; an explicitly approved Ollama model can also be used.

```sh
cd ~/src/any-project                 # any git repository with at least one commit

repowiki generate --model fake --preview   # what would be sent where; sends nothing
repowiki generate --model fake --plan-only # the outline and cost estimate (one fake call)
repowiki generate --model fake --preview   # now lists every page's files and bytes too
repowiki generate --model fake --expect-preview <digest>  # send exactly what was previewed, or nothing
repowiki generate --model fake             # plan, write, validate -> docs/wiki/
repowiki validate                    # re-check at any time (good in CI)
git add docs/wiki && git commit -m "docs: add wiki"

# later, after the code has moved on
repowiki update --model fake --dry-run # which pages cite changed files, and what would be sent?
repowiki update --model fake           # re-write just those pages

repowiki site -o site                # static HTML in ./site
python3 -m http.server -d site       # browse at http://localhost:8000
```

To try the whole pipeline offline without spending any quota, use
`--model fake`. The pages contain placeholder text but real citations.

`--preview` and `update --dry-run` are exact for every stage that is ready
and inert. A ready stage (the plan call; each page's draft once the outline
is checkpointed) shows the provider, the sealed prompt's UTF-8 byte count and
sha256, and the origins it carries (template text, file names, README, source
excerpts, previous pages ...). Stages whose prompt depends on a reply not
received yet (pages before the outline exists, the final pass, a repair) are
listed as deferred with the reason, and only their tokens are estimated.
Previews never build or verify a backend (no Ollama server is contacted),
never recover an interrupted publish (a dry run reports it instead) and write
nothing, checkpoints included; repeating one prints the same bytes. The
printed preview digest can be passed back with `--expect-preview`: the run
prepares the same stages first and sends nothing if any of them changed
(source, configuration, previous pages or checkpoints).

### Choosing models

A model is a spec string `backend[:model]`. Only the first `:` separates the two
parts, so `ollama:qwen3:8b` works.

```sh
repowiki generate --model fake                       # offline placeholder prose
repowiki generate --model ollama:qwen3:8b             # an explicitly approved local model
```

The **cheap** model plans the outline, drafts every page and repairs pages that
fail validation. The **strong** model gives each page a final editing pass. If
you set only one of them, it does every step. If you set neither, repowiki uses
`claude` with that CLI's default model, which currently refuses execution because
its endpoint cannot be bound. Select an approved fake or Ollama model explicitly.

You can also put these settings in `.repowiki.toml` at the repository root
(see [`examples/repowiki.toml`](examples/repowiki.toml)):

```toml
wiki_dir = "docs/wiki"
max_pages = 12
min_citations = 1            # line citations every page must contain
exclude = ["internal/*"]     # extra secret-policy patterns (never sent, never cited)
budget_tokens = 400000       # stop before a run's prompts exceed ~this many tokens

[models]
cheap = "fake"
strong = "fake"

[privacy]
visibility = "private"
allowlist = ["src/*", "tests/*"]
context_classes = ["source"]
prompt_exclude = []
```

`repowiki site` reads the same file, so a custom `wiki_dir` or `exclude` applies
there too.

### Effort presets

`--effort` (or `effort = "..."` in `.repowiki.toml`) picks a named set of
ceilings. Presets are versioned; pin one as `standard@1` to refuse a future
change of its values.

| preset     | pages (at most) | source excerpts per page prompt | run cap (estimated input tokens) | passes |
|------------|-----------------|---------------------------------|----------------------------------|--------|
| `draft`    | 12              | 32 KiB (32,768 UTF-8 bytes)     | 50,000                           | one pass with the cheap model |
| `standard` | 20              | 64 KiB (65,536 UTF-8 bytes)     | 150,000                          | approved final pass: the strong model, else the one configured model |
| `deep`     | 30              | 128 KiB (131,072 UTF-8 bytes)   | 400,000                          | approved final pass; refuses without a separate strong model |

The context ceiling is measured in UTF-8 bytes of the source excerpts (file headers included), so
multibyte text gets fewer characters; `context_bytes` sets the same byte ceiling without a preset.
The preset is applied first. Explicit settings (in the file or as flags such as
`--max-pages` and `--budget`) can only tighten it: a value above the preset's
ceiling is refused. The approval receipt's limits stay the upper bound, so a
preset above them is refused too, and a `deep` strong model must be in the
receipt like any other model. A steering file's exact page count is never
trimmed: a preset whose page ceiling is below it is refused. An unknown preset
name or version is refused.

A preset sets limits only. `deep` allows more pages, context and tokens; it is
not a quality guarantee. `--preview` and `update --dry-run` print the effective
settings before the stages.

### Steering the page plan

A committed `.repowiki/wiki.json` describes the exact pages you want: notes about
the repository, then an ordered list of pages with stable IDs. It is validated as
a whole (`repowiki.steering.load_steering`, or `Settings.load_steering`, which
also applies your `exclude` globs); any problem is an error and nothing is
partly accepted. Generation does not consume it yet.

<!-- steering-example -->
```json
{
  "version": 1,
  "repo_notes": ["A small command-line tool that converts recipes between units."],
  "pages": [
    {
      "id": "overview",
      "file": "overview.md",
      "title": "Overview",
      "purpose": "What the tool does and how the parts fit together.",
      "notes": [{"text": "Start from the command-line entry point.", "author": "maintainer"}],
      "sources": [{"file": "src/units/cli.py"}, {"directory": "src/units"}],
      "coverage": [{"directory": "src/units"}]
    },
    {
      "id": "conversion",
      "file": "conversion.md",
      "title": "Unit conversion",
      "purpose": "How quantities are parsed and converted.",
      "parent": "overview",
      "coverage": [{"file": "src/units/convert.py"}]
    }
  ]
}
```

- `version` (`1`), `repo_notes` and a nonempty `pages` list are required; unknown
  keys, duplicate JSON keys and symlinked files are refused.
- Each page needs `id` (lowercase letters, digits, `-` or `_`, at most 64), `file`
  (a plain `.md` name inside the wiki directory), a nonempty `title` and `purpose`.
  `parent` names another page; self-parents, unknown parents and cycles are refused.
- IDs, files (case-insensitively), titles and purposes must be unique. Titles and
  purposes are compared after Unicode NFKC, trimming and collapsing whitespace,
  and case folding.
- A note is a string or `{"text": ..., "author": ...}`; the optional author does
  not count as a note. At most 30 pages, at most 100 notes across the whole file
  (repository and page notes together), at most 10,000 characters per note.
- `sources` and `coverage` entries are `{"file": path}` (exactly that file) or
  `{"directory": path}` (every file below it). Paths are repository-relative,
  without `.`, `..`, `//`, backslashes or `.git`. A declaration the secret policy
  excludes is refused; a declared directory never admits excluded descendants.
- Notes are written for the model that will draft the pages, so keep secrets and
  private details out of them.

### Repository approval

Select the policy independently of the source being documented:

```sh
export REPOWIKI_POLICY_COMMIT=<reviewed-policy-commit>
repowiki generate --ref <source-commit> --preview
# Alternatively pass --policy-commit <reviewed-policy-commit>.
```

The selected commit must contain `.repowiki.toml` with a `[privacy]` table.
A `site` build fails, and builds nothing, when `REPOWIKI_POLICY_COMMIT` is set
but resolves in none of the site's repositories. It never falls back to another
policy.
The working configuration must match that committed file exactly. Missing or
unknown visibility means private. Empty `allowlist` or absent `source` context
class supplies no source context. Globs are repository-relative; hard secret
denials always win. README, instruction files, and previous wiki text require
their respective `readme`, `instructions`, and `previous_wiki` context classes.
Updates that need previous wiki text refuse without that approval. Prompt
exclusions do not prohibit authorized local citation validation.

An operator must install a receipt in `$XDG_STATE_HOME/repowiki/approvals`, or
`~/.local/state/repowiki/approvals` when that variable is unset. Both `repowiki`
and `approvals` directories must be owned by that operator with mode `0700`;
the receipt must be a regular file with mode `0600`. Keep this state outside
checkouts and Git administration. Repository declarations cannot grant approval.
Use an atomic rename to install a complete receipt; partial JSON is refused.

`repowiki.approval.receipt_path(root)` computes the filename from the canonical
clone root and Git administration path. The JSON receipt has exactly these keys:

- `version`: `1`.
- `root`, `git_dir`: canonical paths for the registered clone and its Git administration.
- `policy_commit`: the full selected policy commit.
- `config_digest`: SHA-256 of the committed configuration bytes.
- `privacy`: the normalized privacy table, including all four keys shown above.
- `models`: explicitly approved model specs.
- `endpoints`: a mapping from each approved spec to its endpoint identifier:
  `builtin` for fake, or `OLLAMA_HOST` (default `http://127.0.0.1:11434`) for Ollama.
  Subscription CLIs currently refuse execution because their actual service
  endpoint cannot be bound through the available adapter interface. They need
  an endpoint-aware adapter before approval can enable them.
- `refs`: explicitly allowed source ref strings. Approving `HEAD` allows it to advance;
  approve a full commit instead to pin the source.
- `operations`: selected values from `generate`, `plan`, `preview`, and `update`.
- `limits`: finite ceilings for `max_pages`, `context_chars`, `timeout`, `jobs`,
  `retries`, `backoff`, and `budget_tokens`. Only retries and backoff may be zero.

If no token budget is requested, the approved token ceiling is enforced. Model,
endpoint, ref, budget, or permissive privacy changes require matching approval.
Receipt and policy identities are checked before runners are constructed, at
direct execution entry points, and before model calls; they also namespace
plan and page checkpoints. New policy approval does not rewrite the source
commit recorded in the wiki index. Register another clone or worktree explicitly;
copying repository files does not transfer approval.

Synthetic tests use `tests/conftest.py::register`: they commit a synthetic policy,
select that commit, and install a fake-only receipt in isolated temporary state.
Tests exercise the production approval reader rather than bypassing it. Never
copy real receipts, account details, or private repository content into fixtures.
The legacy site-config test has a narrowly scoped fixture that registers its
known `wiki_dir` change with a fresh committed policy and receipt; other config
changes are not automatically approved.

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
  only a binary `repowiki doctor` has established (above) is run: where a CLI
  offers an inspection command, doctor checks what it does with them.
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
    endpoint must return the accepted local-model profile: a `details` object
    with non-empty string `format` (`gguf`), `family`, `parameter_size` and
    `quantization_level` and a non-empty list of `families`, a non-empty
    `model_info` object, and `remote_host`/`remote_model` absent or empty. An
    empty, malformed or unknown report (another format, say) is refused. This
    is a narrow contract about what the local server reports, not proof of the
    model's identity; it was written against a synthetic fixture shaped like
    Ollama 0.12's report for a pulled GGUF model. The checks run again before
    every prompt (nothing is cached), so a server that starts reporting a
    remote model, or an `OLLAMA_HOST` that changes mid-run, is refused before
    the next prompt is sent. The `ollama` process gets exactly the verified
    endpoint. `--allow-remote` skips these checks.
- **Minimal environment.** Backends get `PATH`, `HOME`, locale, temp and XDG
  directories, proxy and CA settings, plus the variables their own sign-in
  needs (for example `ANTHROPIC_*`, `CODEX_HOME`, or `<PROVIDER>_API_KEY` for
  the opencode provider you picked). Nothing else from your shell, unless you
  list names in `REPOWIKI_PASS_ENV`.
- **One outbound gateway.** Every prompt is assembled from sections tagged with
  their origin (repowiki's own text, file names, the README, source excerpts,
  the previous wiki, planner replies, drafts, validation messages) and sealed
  under the current approval before any backend sees it. Sealing refuses a
  section without a known origin, settings without an approved privacy
  policy, and a section naming a file the policy keeps out of prompts; it
  then redacts the complete prompt and replaces any name of an excluded file,
  wherever it appears, with `[excluded path]`. Backends accept only a sealed
  prompt sent through the gateway (retries included) and refuse plain
  strings. The `fake` backend records a complete trace of every call.
- **Secret policy, by file name.** Files matching the policy are never read
  into a prompt, never accepted as a citation, never linked or exported by the
  site, and never used as wiki pages (a page like `agent-transcript.md` listed
  in `index.json` fails validation, and `site` and `update` refuse to run). The
  patterns (case-insensitive): `.env*`, `*.env`, `*.pem`, `*.key` and other key
  or keystore files, `id_*` SSH keys, `.npmrc`, `.pypirc`, `.netrc` and similar
  credential files, `secrets*`, `.secrets*`, `credentials*`, `*session*`,
  databases, caches and pickles (`*.db`, `*.sqlite*`, `*.pickle`, `*.pkl`,
  `*.joblib`, `*.rdb`, `*.cache`), Terraform state and variables, AI assistant
  transcripts and session folders (`.claude/`, `.codex/`, `.opencode/`,
  `.specstory/`, `transcripts/`, `*transcript*.jsonl|json|md|txt|log|html`),
  credential folders such as `.ssh/` and `.aws/`, and the folders `data/`,
  `databases/`, `sessions/`, `checkpoints/` and `caches/`. Add globs with
  `exclude`. An allowlist cannot lift these. The full list is in
  [`src/repowiki/policy.py`](src/repowiki/policy.py).
- **Prompt-only defaults.** `README*`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`,
  `SKILL.md`, `plans/`, `reports/` and the wiki directory stay usable for
  local validation and rendering but are never sent to a model unless the
  approved configuration admits them: the `readme`, `instructions` and
  `previous_wiki` context classes, and an allowlist pattern naming `plans/` or
  `reports/` explicitly.
- **Redaction, by content.** Credential-shaped strings (common API-token
  formats, bearer tokens, private-key blocks, and quoted literals of any length
  assigned to names like `api_key`, `secret`, `token` or `password`; calls
  such as `get_password()` are left alone) are replaced with
  `[redacted by repowiki]` in every prompt, model replies, rendered pages and
  exported source pages. Line numbers do not change.
- **Filesystem.** repowiki writes only inside the wiki directory (a relative
  path in the repository, not through a symlink that leads out of it), only to
  plain page names, never through symlinks, and deletes a page dropped from
  the index only if it carries the marker repowiki writes into every page.
  Checkpoints live in `.git/repowiki/`.
- **Publishing is one durable transaction.** New files are staged and the
  transaction record (the exact sha256 of every target before and after) is
  kept in the worktree's Git administration (`<git dir>/repowiki-txn/`), never
  in the checkout; files and directories are fsynced in order, and rewriting
  the record as "committed" is the commit point. A run killed at any point is
  finished or undone by the next run, which checks every hash first and
  changes nothing if the evidence does not match (the record is kept for
  you to inspect). A journal file in the checkout is never trusted. The Git
  administration directory must be on the same filesystem as the wiki.

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
- `--preview` labels its totals: ready stages as exact UTF-8 bytes and
  estimated input tokens, deferred stages (the pages before the outline exists,
  final passes, repairs) as a guess that only the run cap bounds. It never
  shows a price: repowiki does not know what a provider charges.
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
   passes is it published, as one transaction. Pages, `index.json`, and exact-byte
   page ownership are staged and journaled in this worktree's Git administration.
   Existing pages can be replaced or deleted only when their SHA-256 matches the
   authoritative local ownership record. An unowned collision or a manually edited
   replacement target refuses the whole publication; a dropped unowned or edited
   page is kept with a warning. The generated marker and checkout hash files grant
   no authority. `index.json` is transaction metadata, outside page adoption.
   Originals are kept until the durable commit point. Errors before that point
   roll back; an interrupted operation is recovered by the next `generate`,
   `update`, `validate` or `site`. Recovery restores the complete old state or
   finishes the complete committed state, including ownership, and keeps evidence
   when hashes do not match. Publication, adoption and recovery share the wiki lock.

### Adopting a reviewed page offline

Legacy pages, colliding handwritten pages, and intentionally edited generated pages
require individual review before repowiki can replace or delete them. Adoption sends
no prompts, contacts no provider, and changes no page bytes or index entries:

```sh
repowiki adopt 1-overview.md --repo ./example
# Review this page on disk. The command prints its exact-byte sha256 and wiki base.
repowiki adopt 1-overview.md --repo ./example --reviewed-sha256 REVIEWED_SHA256 --expect-base REVIEWED_BASE
```

Use `--wiki-dir handbook` for a different wiki. Supply both printed digests only after
reviewing the candidate's actual bytes. A changed candidate or wiki base refuses:
inspect a fresh candidate and review again. A fresh review of already owned bytes
is unchanged. Ownership is scoped to this canonical worktree and wiki in local Git
administration; clones and other worktrees must adopt their pages separately.
There is no automatic adoption, blanket force, or receipt import. Secret-pattern
names, paths, symlinks, and nonregular files cannot be adopted. Git administration
must remain operator-controlled; an ownership record is local authority, never an
instruction to execute.

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

- **Built sites are offline: a reader's browser fetches nothing but the site
  itself.** Mermaid diagrams render from the Mermaid build vendored into the
  package (`assets/vendor/`, an audited Mermaid 12.0.0 copy whose bytes are
  verified against the SHA-256 recorded beside it on every build; ship its
  licence when you redistribute). `--mermaid path/to/mermaid.min.js` swaps in
  a different local file; a remote mermaid URL is refused, and the diagram
  script never injects a `<style>` element or `style` attribute, so a
  `style-src 'self'` policy holds. Markdown images must be PNG, JPEG, WebP or
  GIF files of at most 5 MiB inside the repository — checked by content as
  well as name, copied into the site under `_assets/`, and refused (failing
  the build, keeping the last good site) when missing, oversized, or of
  another type such as SVG. Remote, protocol-relative and `data:` images are
  validation errors. Each image is checked to its end
  (PNG chunks and pixel data, JPEG scans, GIF blocks, WebP frame headers),
  so a truncated or corrupt file never replaces a good one. Diagrams that
  would load something themselves (an image node such as `A@{ img: ... }`, a
  resource tag in an HTML label, or CSS with `url()`, `image-set()`,
  `@import` or `@font-face`) are validation errors too, however they are
  encoded; URLs in labels and `click` links stay allowed. Every page also
  carries a Content-Security-Policy (`default-src 'self'`), so the browser
  itself refuses anything from elsewhere. Raw HTML stays escaped, so it can
  never fetch anything either.
- **Narrow portal hooks** let an embedding portal finish the site without
  opening it up: `build_site(..., portal=repowiki.site.PortalHooks(...))`
  takes `branding` (escaped before it is rendered), and a same-origin
  `logout_action` plus `token_endpoint` for a structured logout — the control
  in the header POSTs to the action with a `X-CSRF-Token` header fetched from
  the token endpoint at click time, so no session token is ever baked into
  the HTML. Refuse-listed URLs (anything not a root-relative path) fail the
  build. Without a portal the site is standalone: no branding, no logout.
  Every build also ships a bounded, repo-scoped page index that powers the
  header search box, links for navigating between repositories, and theme
  persistence: the reader's choice is stored and re-announced as the
  `repowiki:theme-change` event (with `{theme, generation}`) on the next
  visit, so diagrams re-render in the chosen theme.
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
- **Only validated wikis are built.** Every requested wiki is read once (the
  `ref` is resolved to a commit first, so a branch that moves mid-build cannot
  change what was opened; a working-tree wiki is read under the wiki lock) and
  fully validated, exactly as `repowiki validate` checks it, before anything is
  rendered; the site renders those same captured texts. A missing repository
  or `index.json`, a duplicate repository name, or any validation error in any
  wiki fails the whole build and leaves the previous site untouched.
  `--validated` (as in `repowiki site --validated --ref main`) states that
  intent; it is always on and cannot be turned off.
- Raw HTML in pages is limited to a few attribute-free tags (`details`,
  `summary`, `kbd`, ...). Anything else is escaped. Links must be relative,
  `#anchors`, `http(s)` or `mailto`; any other scheme renders as text, and
  images additionally follow the offline rule above.
- **Publication is recoverable.** The output is a symlink into a store beside
  it (`.repowiki-site/` for an output named `site`), which holds complete
  immutable releases under `releases/<content digest>/` and a `current`
  pointer. A build renders into staging, fsyncs every file, names the release,
  and then replaces the pointer atomically — a crash before or after that
  leaves the old or the new complete release being served, never a partial
  one, and repeated publishes of the same content reuse the same release.
  Releases are the unit of versioning: a served release keeps its own assets.
  Old releases are kept for at least 30 days (the current one always).
  Publishing a second site to the same output while one is running is
  refused. An older real-directory site is left alone until you convert it
  once with `repowiki site --migrate` (a locked, journaled conversion that
  recovery can finish after an interruption; a directory that is not a
  repowiki site is never migrated).

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

The default test suite is hermetic. Installed backend CLIs are hidden from `PATH`,
`HOME` and the XDG directories are empty temporary ones, inherited credential
variables (API keys, tokens, provider prefixes) are removed, and network
connections other than loopback are refused. Tests that run a real backend binary are marked
`integration` and are skipped unless you name the binary explicitly. Nothing is
looked up on `PATH`:

```sh
REPOWIKI_INTEGRATION_OPENCODE=/path/to/opencode pytest -m integration
```

The opencode integration test installs a synthetic hostile `HOME` with
permissive permissions, an instruction file and a plugin canary. It checks that
opencode loads them without repowiki's isolation, and that with the isolation
every agent opencode lists has no tools and every permission denied. The binary
gets no credentials and runs without network access: under `sandbox-exec` on
macOS or `unshare --net` on Linux, after a child-process canary confirms that
connections and name lookups are refused. Where that is not available, the test
is skipped.

## Licence

MIT. See [LICENSE](LICENSE).
