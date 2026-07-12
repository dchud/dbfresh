# Concepts

## Values, not jobs

dbfresh checks the data a pipeline produced, not whether the pipeline "ran."
It runs from outside the systems it watches, on a schedule of its own (cron,
a systemd timer, CI), and asks each configured table or view a small set of
value-level questions: how many rows, how stale, how many nulls, does the
schema still match, does every row satisfy a predicate. A job that reports
success but silently writes zero rows, half the expected rows, or a stale
snapshot is caught by the data itself failing a check -- no dependency on the
job's own exit code or logging.

## The two primitives

Every check compiles down to one of exactly two primitives (§6.1):

- **metric + expectation.** A metric compiles to a single scalar-returning
  query (`row_count`, `null_rate`, `sum`/`avg`/`min`/`max`, `freshness`,
  `schema`); an expectation is a single operator compared against that
  scalar (`between`, `max`, `max_lag`, `vs_previous`, `unchanged`, ...). See
  [Check reference](checks.md) and
  [Expectations & durations](expectations.md).
- **assertion.** A boolean predicate (or a raw query) that must select zero
  rows: `assert: "amount >= 0"` compiles to
  `SELECT * FROM obj WHERE NOT (amount >= 0)`. Any returned rows are
  violations, shown as evidence in the digest, capped by the dialect's row
  limiting form. The persisted metric value for an assertion is the
  violation row count, so "how many bad rows over time" trends for free.

Stacking several checks on one table -- a schema check, a row-count check, a
freshness check, a handful of column-level checks -- is how dbfresh infers
whether an extract succeeded, without ever asking the extract job itself.

## Table-level vs. column-level tiers

A check is **table-level** when it names no column (`row_count`, `schema`,
whole-table assertions) and **column-level** when it names a `column` or
`key` (`null_rate`, `duplicate_count`, `sum`/`avg`/`min`/`max`,
`freshness`). The tier is derived from the check, never declared separately
-- it groups checks in the [Check reference](checks.md), the
[applicability matrix](reference/matrix.md), the [configurator](
authoring-checks.md), and the [TUI dashboard](tui.md), but it is not a
distinct config field.

## Definitions in git, observations in SQLite

Check *definitions* are YAML, reviewed like any other code change and
committed to a team repository (§12.4); dbfresh never writes a check
definition anywhere but the config file a human edited or `dbfresh add`
appended to. Check *observations* -- the scalar or fingerprint a run
actually saw, its status, and when it ran -- live in a local SQLite
observation store, separate from the config (§8). That split is what makes
`vs_previous` expectations, `dbfresh history`, and the TUI's status
dashboard possible without turning the YAML into a database, and why
renaming a file or moving a check between included files never loses its
history (§8.2, §12.4).

## One engine, two front ends

The batch CLI (`dbfresh run`) is the primary contract for scheduler-driven
alerting: progress bar, plain-text digest, `--json`, and exit codes. The
interactive Textual app (`dbfresh ui`) is a second consumer of the exact
same engine and store -- a status dashboard, the same configurator as
`dbfresh add`, and a history browser -- adding no check semantics of its
own. See [CLI reference](reference/cli.md) and [TUI guide](tui.md).
