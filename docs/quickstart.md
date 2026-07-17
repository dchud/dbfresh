# Quickstart

## Install

Once a release is published to PyPI:

```bash
uv tool install dbfresh
# or
pipx install dbfresh
```

Working from a checkout of this repository, install the dependencies and run
it through `uv` instead:

```bash
uv sync
uv run dbfresh --version
```

The rest of this page uses `uv run dbfresh ...`; drop the `uv run` prefix
once `dbfresh` is installed as a standalone tool.

**SQL Server** and **Databricks** sources need their database driver, which
ships as an optional extra rather than a core dependency -- so a sqlite-only
install isn't forced to build the native `pymssql` or Databricks driver.
Install with the matching extra:

```bash
uv tool install "dbfresh[sqlserver]"    # SQL Server (pymssql)
uv tool install "dbfresh[databricks]"   # Databricks
```

From a checkout, add `--extra sqlserver` (or `--extra databricks`) to
`uv sync`. Without the extra, adding such a source fails with a hint that
names the extra to install.

## A minimal config

A config is one YAML file: `sources:` (where to connect) and `checks:` (what
to validate). This example uses the built-in `sqlite` source, which works
with no external infrastructure -- the same source dbfresh uses for its own
test suite, and a legitimate choice for a real, lightweight source too.

```yaml title="config.yaml"
version: 1

sources:
  demo:
    type: sqlite
    database: ${DEMO_DB_PATH}

checks:
  - source: demo
    object: orders
    metric: row_count
    expect: { between: [1, 1000000] }

  - source: demo
    object: orders
    metric: null_rate
    column: customer_email
    expect: { max: 0.5 }
```

`${DEMO_DB_PATH}` is a secret/parameter interpolated from the environment
-- the same mechanism used for `${MSSQL_URL}` or
`${DATABRICKS_TOKEN}` against a real warehouse. Nothing connection-specific
is hard-coded into the committed config.

## Exporting secrets

Supply `${VAR}` values either as real environment variables, or via a
gitignored, per-user `.env` file next to the config:

```bash title=".env"
DEMO_DB_PATH=./demo.db
```

Every command that reads a config (`run`, `history`, `prune`, `add`, `ui`)
loads `.env` (from the config file's directory) automatically, before
parsing the config -- so a config committed to a team repo never carries a
connection string or credential.

See [Environment & secrets](environment.md) for the field-by-field
`${VAR}` reference per source type, and the copyable
`config.example.yaml` / `.env.example` pair at the repo root.

## First run

Seed a table to check against (any means works; here's the sqlite adapter
directly):

```bash
uv run python -c "
from dbfresh.adapters.sqlite import SqliteAdapter
a = SqliteAdapter('demo.db')
a.rows('CREATE TABLE orders (id INTEGER, customer_email TEXT)')
a.rows('''
    INSERT INTO orders (id, customer_email) VALUES
    (1, \"a@example.com\"), (2, \"b@example.com\"), (3, NULL)
''')
a.close()
"
```

Then run the checks:

```bash
uv run dbfresh run -c config.yaml
```

```text
DATA CHECK REPORT — 2026-07-12T21:45:00Z
2 checks · 2 passed · 0 failed · 0 warned · 0 skipped · 0 unreachable
```

Both checks pass: 3 rows is within `[1, 1000000]`, and a null rate of 1/3 is
within the `max: 0.5` bound. That's a first green check.

`--json` emits the same result as a stable, machine-readable contract instead
of the digest (and suppresses the progress bar):

```bash
uv run dbfresh run -c config.yaml --json
```

```json
{
  "status": "OK",
  "run_id": 2,
  "started_at": "2026-07-12T21:45:05Z",
  "finished_at": "2026-07-12T21:45:06Z",
  "counts": {
    "OK": 2,
    "WARN": 0,
    "FAIL": 0,
    "ERROR": 0,
    "SKIPPED": 0
  },
  "results": [
    {
      "check_id": "268724654079",
      "source": "demo",
      "object": "orders",
      "metric": "row_count",
      "label": null,
      "tier": "table",
      "status": "OK",
      "value": 3.0,
      "value_text": null,
      "expected": "between 1 and 1000000",
      "observed": "3",
      "error": null,
      "samples": null,
      "diff": null
    },
    {
      "check_id": "8cf0327f0ccc",
      "source": "demo",
      "object": "orders",
      "metric": "null_rate",
      "label": null,
      "tier": "column",
      "status": "OK",
      "value": 0.3333333333333333,
      "value_text": null,
      "expected": "max 0.5",
      "observed": "0.3333333333333333",
      "error": null,
      "samples": null,
      "diff": null
    }
  ]
}
```

## Reading the digest and exit codes

The digest's header line always shows the total and a breakdown by outcome;
a body block per non-`OK` check follows, with the expected vs. observed
value (or the error, or sample violation rows for a failed assertion). See
[Reporting](checks.md) and the generated [CLI reference](reference/cli.md)
for the full digest and JSON shapes.

The process exit code is the worst status across every check in the run --
what a scheduler (cron, systemd timer, CI) checks to decide whether to
alert:

| code | status       | meaning                    |
| ---- | ------------ | --------------------------- |
| 0    | OK / SKIPPED | all clear                  |
| 1    | WARN         | soft-bound violations only |
| 2    | FAIL         | value violations           |
| 3    | ERROR        | unreachable / query error  |

`run` also persists an observation per check to a local SQLite store
(`./dbfresh.db` by default, next to the config) unless `--no-store` is
given -- see [History & trends](history.md) for what that unlocks.

## Next steps

- [Concepts](concepts.md) for the mental model behind metrics, expectations,
  and tiers.
- [Check reference](checks.md) for every check type and the per-engine
  notes.
- [Calendar & scheduling](calendar.md) to make expectations weekend- and
  holiday-aware.
- [Authoring checks](authoring-checks.md) to generate a check bundle for a
  table instead of writing YAML by hand (`dbfresh add`).
