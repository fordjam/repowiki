# Contributing

Thanks for helping. repowiki is small on purpose: one runtime dependency
(`markdown-it-py`), the standard library, and git.

## Set up

```sh
python3 -m venv .venv && . .venv/bin/activate   # Python 3.11+
pip install -e '.[dev]'
pytest
ruff check src tests && ruff format --check src tests && mypy src
```

## Ground rules

- **Tests stay offline.** Use the `fake` backend (`repowiki.runners.FakeRunner`),
  or a stand-in CLI script as in `tests/test_runners.py`. Never call a real model
  or the network from a test. Build fixtures in `tmp_path`; `tests/conftest.py`
  has a synthetic repository and an isolated git configuration.
- **Read code from git, not the working tree.** Anything a page cites must come
  from `Snapshot` (one pinned commit).
- **Validation is the contract.** If you change the page format, update
  `validate.py`, the README section on validation and the tests together.
- **Type hints everywhere**, and keep modules focused:

  | Module        | Job |
  |---------------|-----|
  | `git.py`      | read-only git plumbing and the pinned `Snapshot` |
  | `survey.py`   | what the planner sees (README, tree, entry points, tests) |
  | `prompts.py`  | prompt templates |
  | `runners.py`  | backend adapters (`claude`, `opencode`, `codex`, `ollama`, `fake`) |
  | `generate.py` | plan, write, relink, repair, save |
  | `update.py`   | incremental refresh |
  | `validate.py` | index schema, page shape, links, line ranges |
  | `render.py`, `site.py` | Markdown to HTML and the static site |

## Adding a backend

Subclass `CLIRunner` in `runners.py`, implement `command()` (argv and stdin) and,
if the CLI mixes logs with the answer, `output()`. Register it in `BACKENDS`,
add a test with a stand-in script, and add a row to the README table.

## Privacy

Examples, fixtures and docs use fictional names, `example.com` addresses and
`/home/user` paths. Do not commit real repository names, personal paths, e-mail
addresses or credentials. Generated wikis are told not to include them either,
but review what a model writes before you publish it.

## Pull requests

Keep them focused, include tests, and describe user-visible changes in the PR
description.
