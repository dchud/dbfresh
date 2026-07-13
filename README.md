# dbfresh

External, value-level freshness and constraint checks for SQL Server and
Databricks data sources, plus a built-in sqlite adapter for local testing and
lightweight sources. `dbfresh` validates the data those pipelines produce --
row-count ranges, aggregate bounds, freshness, null rates, uniqueness,
arbitrary SQL assertions -- not whether the jobs ran. It runs from outside
the systems it watches and reports through the CLI's exit codes plus a
copy-pasteable digest.

**Full documentation: <https://dchud.github.io/dbfresh/>**

## Install

Once a release is published to PyPI (still pending):

```bash
uv tool install dbfresh
# or
pipx install dbfresh
```

From a checkout of this repository, for now:

```bash
uv sync
uv run dbfresh --version
```

## Minimal example

A config is one YAML file: `sources:` (where to connect) and `checks:` (what
to validate).

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

Export the `${VAR}` secrets the config references -- real environment
variables, or a gitignored `.env` next to the config (loaded automatically
by every command that reads the config):

```bash title=".env"
DEMO_DB_PATH=./demo.db
```

Run the checks:

```bash
uv run dbfresh run -c config.yaml
```

```text
DATA CHECK REPORT — 2026-07-12T21:45:00Z
2 checks · 2 passed · 0 failed · 0 warned · 0 skipped · 0 unreachable
```

`--json` emits the same result as a stable, machine-readable contract instead
of the digest. The process exit code is the worst status across every check
-- what a scheduler (cron, systemd timer, CI) checks to decide whether to
alert:

| code | status       | meaning                    |
| ---- | ------------ | --------------------------- |
| 0    | OK / SKIPPED | all clear                  |
| 1    | WARN         | soft-bound violations only |
| 2    | FAIL         | value violations           |
| 3    | ERROR        | unreachable / query error  |

## Three surfaces, one engine

- **Batch CLI** -- `dbfresh run` / `history` / `prune` / `add`: scheduler-driven
  checks, observation history, retention enforcement, and check authoring.
- **TUI** -- `dbfresh ui`: an interactive Textual dashboard over the same
  config, engine, and observation store as the CLI.
- **Configurator** -- `dbfresh add` (also the TUI's Configure screen): a
  metadata-driven wizard that introspects a source object and proposes a
  check bundle instead of hand-written YAML.

## More

- Documentation site (quickstart, concepts, check reference, calendar and
  scheduling, CLI reference, TUI guide, and more):
  <https://dchud.github.io/dbfresh/>
- Full specification: [`dbfresh.md`](dbfresh.md)
- Scope and tooling: [`AGENTS.md`](AGENTS.md)
- Build/test commands and working agreement: [`CLAUDE.md`](CLAUDE.md)

## License

MIT -- see [`LICENSE.md`](LICENSE.md).
