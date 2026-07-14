# dbfresh

External, value-level freshness and constraint checks for SQL Server and Databricks data sources. It validates the data those pipelines produce — freshness, row-count ranges, aggregate bounds, null rates, uniqueness, arbitrary SQL assertions — not whether the jobs ran, and reports from outside the systems it watches via the CLI.

## Key Files

| Path               | Purpose                                                                              |
| ------------------ | ------------------------------------------------------------------------------------ |
| `src/dbfresh/`     | Core package (src-layout): CLI, engine, adapters, checks, store, calendar            |
| `src/dbfresh/cli.py` | CLI entrypoint; installed as the `dbfresh` console script                          |
| `docs/original-specification.md` | Original implementation specification — kept as a historical record, no longer the living source of truth |
| `tests/`           | pytest suite                                                                         |
| `pyproject.toml`   | Dependencies, `dbfresh` entry point, ruff and pytest configuration                   |

## Build & Test

```bash
uv sync                 # create/refresh the virtualenv from the lockfile
just test               # uv run pytest
just lint               # ruff check + ruff format --check (no writes)
just fmt                # ruff check --fix + ruff format (writes)
uv run dbfresh --version

# Always run this before pushing — the local equivalent of CI
just check              # lint + format check + tests
```

## Architecture

Python 3.14 CLI, packaged with `uv` in a src-layout (`src/dbfresh/`). Runtime deps are `rich` (progress + digest rendering) and `structlog` (logging); source drivers (`pymssql` for SQL Server, `databricks-sql-connector` for Databricks Unity Catalog) are added as their adapters land. Check definitions live in version-controlled YAML; per run, `dbfresh` opens one connection per source (sources in parallel, one connection per worker thread), compiles each check to a single dialect-adjusted SQL query, evaluates the scalar/rows against an expectation or a prior observation, persists the observation, and exits with the worst status.

Two abstractions carry the design. Every check reduces to one of **two primitives**: a metric compared to an expectation, or an assertion query that must return zero rows — builtins are sugar over these. Every source sits behind a small **adapter protocol** (`scalar`, `rows`, `describe`, `close`); adding a source type is one adapter plus one factory line. Definitions are config (git-tracked YAML); observations are data (local SQLite at `dbfresh.db`) — the two never mix. `docs/original-specification.md` preserves the original design and phased build plan as a historical record; the shipped code plus the [documentation site](https://dchud.github.io/dbfresh/) are current.

## Working Agreement

### Tone & language

- Flat, matter-of-fact tone in replies and in anything written to the repo — no feigned enthusiasm, no fawning, no filler.
- Clear, literal language — no idioms or euphemisms (avoid things like "belt-and-suspenders," "low-hanging fruit," "sunset," "north star"); state the literal technical claim instead.
- Scope claims to what a change actually delivers — don't extrapolate user-facing impact beyond what was implemented and verified.

### Documentation

- Never create or edit documentation other than the doc a task explicitly references.
- If asked to revise a ticket or document, edit that ticket or document directly — don't write a separate summary doc; summarize changes in your response instead.
- No unsolicited spec or design docs — don't write one unless explicitly asked.
- Plans are temporary: fine to write while working, but delete them from the repo once the work is complete — they don't get committed.
- Skip writing a plan at all when the ticket/issue already lists actionable steps; don't auto-generate a plan doc on top of one that already exists.
- If the project keeps a changelog, entries are terse and user-facing (a few lines); file-by-file enumeration, counts, and verification notes belong in the commit message or PR description, not the changelog.

### Keeping implementation artifacts out of the code

- No process labels in source, tests, or docs — no phase letters, no PR/issue/ticket numbers, no "currently"/"not yet" wording describing the state of the change itself.
- No revision-history narration inside artifacts — state the PR/issue/commit/doc content correctly the first time; don't narrate "my first description was wrong" or "to be precise" inside the artifact.

### Issue tracking (br / beads)

This project uses **br** (beads_rust) for issue tracking, if configured. Core commands:

```bash
br ready --json                              # unblocked work
br create "Title" -t bug|feature|task -p 0-4 --silent
br update <id> --status in_progress --json
br close <id> --reason "..." --json
br sync --flush-only                         # export DB to JSONL (never auto-commits)
```

- Never run `br edit` — it opens `$EDITOR` and blocks agents.
- Never pass `--slug` to `br create` — use br's default meaning-free IDs (`<prefix>-xxxx`); descriptive text goes in the title/description, not the ID. Prefer `br create --silent` over parsing JSON for the new ID.
- `br update --description` **replaces**, it doesn't append — always `br show <id>` first before updating a description.
- Mark a bead `in_progress` when starting work on it, at the same time as creating the feature branch, not later.
- Close the bead in the PR that resolves it — the closure rides in the committed `.beads/issues.jsonl` and takes effect on the default branch when the PR merges (like `Closes #NNN`); reopen it if the PR is abandoned. CI green is not the same as shipped.
- Titles are descriptive only — no version, priority, or milestone prefixes; that's metadata, not title content.

### Bead elevation

- Don't elevate every bead to a GitHub issue — "in scope for a release" is not the same as "needs an issue." Concrete implementation tickets benefit from elevation; sequencing markers, checklists, and decisions-to-record are fine bead-only. Ask before bulk-elevating.
- Before filing a GitHub issue for a bead, check that bead's comments for an existing elevation note — an umbrella issue's "elevated to N issues" summary is not a reliable per-bead map.
- A PR resolving an elevated bead includes `Closes #NNN` in its body so the GitHub issue auto-closes on merge.

### Git & pull requests

- New issue = new branch, created from `main` before starting work.
- `main` is protected; you cannot push directly to it. Land every change through a pull request and let the maintainer merge — do not merge without explicit approval, even after CI is green. Approving an approach is not approving a merge.
- Once implementation is done and local checks pass, go straight to commit → push → open PR without asking — pushing sooner starts CI sooner, and review happens in the PR view anyway. Merging is still gated on explicit permission.
- Start PR bodies from the repo's PR template if one exists, and fill in its checklist (mark items N/A where they don't apply) rather than writing freeform.
- Keep PR titles clean and descriptive — no bead/ticket ID suffix; put `Bead: <id>` at the bottom of the PR body instead.
- No AI commit trailers (no `Co-Authored-By:` / session-link lines on commits); a single "Generated with Claude Code" footer in the PR body is fine.
- Don't delete remote branches after merge — that's the maintainer's manual step. Post-merge cleanup is `git remote prune origin` (local refs only).
- Don't hard-wrap GitHub Markdown — one long line per paragraph and per bullet in bead descriptions, issue/PR bodies, and comments; GitHub collapses single line breaks anyway. Commit message bodies still wrap at ~72 columns.

### Scratch files

- Never write scratch files to the system `/tmp` — use a gitignored project-local `tmp/` directory instead, not ad-hoc dot-directories.

### Tooling defaults

- Prefer `rg` over `grep` for repo searches — respects `.gitignore` automatically, faster, cleaner output.
- Prefer `fd` over `find` for file/path searches — same gitignore awareness, simpler syntax.
- Prefer `dust` over `du` for "what's taking up space" inspection.
- Use `git diff` for unified inspection and `git dft` (difftastic) for structural/AST-aware diffs when reviewing changes.
