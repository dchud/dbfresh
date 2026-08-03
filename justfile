# dbfresh task recipes. Run `just` to list.

default:
    @just --list

# Run the test suite across all cores. Tests are independent (each gets its
# own tmp_path), and the Textual Pilot tests that dominate the runtime are
# the ones parallelism helps most. Run `uv run pytest <path>` directly for a
# single test -- serial output is easier to read when something fails.
test:
    uv run pytest -n auto

# Lint (ruff) and check formatting without modifying files.
lint:
    uv run ruff check .
    uv run ruff format --check .

# Auto-fix lint issues and format.
fmt:
    uv run ruff check --fix .
    uv run ruff format .

# Type-check src/dbfresh with mypy.
typecheck:
    uv run mypy

# Local equivalent of CI: lint, format check, type check, and tests. Run before pushing.
check: lint typecheck test

# Run the CLI (pass args after `--`, e.g. `just run -- --version`).
run *args:
    uv run dbfresh {{args}}

# Regenerate registry-derived reference pages, then build the docs site.
docs:
    uv run --group docs python -m dbfresh.docsgen
    uv run --group docs mkdocs build --strict
