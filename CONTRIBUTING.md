# Contributing to dbfresh

## Development setup

`dbfresh` uses [uv](https://docs.astral.sh/uv/) for environment and tooling and
[just](https://github.com/casey/just) for task recipes. Python 3.14 is required.

```bash
uv sync                 # create the environment from the lockfile
uv run pre-commit install   # enable the ruff pre-commit hooks (optional)
just                    # list available recipes
just check              # lint + format check + tests — the local CI equivalent
```

## Workflow

- Branch off `main` for each change; `main` is protected and lands via pull
  request.
- Run `just check` before pushing — CI runs the same recipe.
- Keep pull requests focused; start the description from the PR template and
  fill in its checklist.
- Commit messages: a concise subject line, body wrapped at ~72 columns, no
  automated trailers.

## Issue tracking

Work is tracked with [beads](https://github.com/steveyegge/beads) (`br`). The
committed `.beads/issues.jsonl` is the shared record; the local `.beads/*.db` is
gitignored. See `CLAUDE.md` for the full working agreement.
