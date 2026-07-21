# Check reference

Every check is one of the metrics below (compiled to a single scalar query
and compared via an expectation) or an assertion. Object names are
used verbatim, exactly as authored (`dbo.fct_sales`,
`main.gold.customer_360`) -- dbfresh never quotes or rewrites them. An
optional `where:` clause is appended to every metric query.

The full metric table -- name, tier, required field, and description -- is
generated from the code at build time: see the
[generated metric reference](reference/metrics.md). This page adds the SQL
shape and usage notes per check; it is never a second, hand-maintained copy
of that table.

## Table-level checks

A check is table-level when it names no `column`/`key`.

### `row_count`

`SELECT COUNT(*) FROM obj [WHERE ...]`. The most common stability check --
"did today's load land in the usual range" -- and the check the
[configurator](authoring-checks.md) always proposes with a `vs_previous`
volume-stability guard.

### `schema`

Not a SQL query: reads the object's columns via the adapter's `describe()`
and reduces them to a fingerprint -- a stable hash over the
order-insensitive set of `(column_name, data_type)` pairs. Column order and
nullability are excluded; `data_type` is the native type name, so
fingerprints are per-engine (fine, because checks are per-source). Its
expectation is `unchanged` (compare to the previous observation) or `equals`
(a pinned fingerprint) -- see
[Expectations & durations](expectations.md). On drift, the digest shows the
added, removed, and retyped columns.

`unchanged`'s baseline is the most recent observation that actually recorded
a fingerprint -- a SKIPPED (`skip_off_schedule`) or ERROR (unreachable
source) run persists with no fingerprint and is skipped past rather than
read as "no prior observation," which would otherwise let real drift from
right before a skip or an outage go undetected. The baseline is the last
*recorded* fingerprint regardless of that observation's own status, and
the semantic that follows from it is "detect a change," not "hold a
permanent alarm": once a change is detected it alarms (FAIL, or WARN under
`severity: warn`) exactly once, and the new shape becomes the baseline for
every run after that. Pin a fingerprint with `equals` instead when you want
an alarm that never self-clears.

### Assertions

`assert: "<predicate>"` compiles to
`SELECT * FROM obj WHERE NOT (<predicate>)`, capped by the dialect's row
limiting form (20 rows fetched, 10 shown in the digest); the persisted
value is the exact violation row count.

`assert_sql:` lets you supply the whole violation-selecting query directly
for anything the predicate form can't express. It runs exactly as
authored -- never rewritten to inject a row cap, which can corrupt the
query (a cap injected inside a CTE truncates the scan instead of the
returned rows; `SELECT DISTINCT` can become invalid syntax) -- and is
capped only at fetch time: at most 21 rows are pulled off the cursor, 10
shown in the digest. Below 20 violations the persisted value is the exact
count; at 21 or more fetched rows, the true count isn't known beyond "at
least 20," so the persisted/displayed value reads `"20+"` instead of a
literal (and meaningless) 21.

## Column-level checks

A check is column-level when it names a `column` (most metrics) or a `key`
(`duplicate_count`).

### `null_rate`

`SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END) * 1.0 / NULLIF(COUNT(*), 0)`.
The `* 1.0` float coercion is a dialect variance (some engines use an
explicit cast instead) -- the compiler always asks the dialect, never
branches on an engine name.

### `duplicate_count`

`COUNT(*) - COUNT(DISTINCT key) FROM obj WHERE key IS NOT NULL [AND ...]`.
Counts duplicates among non-null keys only: `COUNT(DISTINCT key)` already
ignores nulls, and the `key IS NOT NULL` guard stops a null-heavy key column
from being counted as duplicates against `COUNT(*)`. Composite keys are out
of scope for v1 -- one key column per check.

### `sum` / `avg` / `min` / `max`

`SELECT AGG(column) FROM obj`. Central tendency beyond `avg` (median,
stddev, percentiles) is deferred, to avoid engine-specific percentile SQL.

### `freshness`

`SELECT MAX(column) FROM obj`; the lag between that timestamp and now is
computed in Python, then compared via `max_lag:`. `freshness_source:`
selects the timestamp origin:

- `column` (default) -- `MAX(column)`; requires `column:`.
- `describe_history` -- Databricks-only, table-only (see below). The most
  recent *data*-changing operation from `DESCRIBE HISTORY`, filtered to
  `WRITE`/`MERGE`/`DELETE`/`UPDATE` so maintenance operations (`OPTIMIZE`,
  `VACUUM`) can't make a stale table look fresh. No `column:` needed.
- `describe_detail` -- Databricks-only, table-only (see below). The table's
  `lastModified` from `DESCRIBE DETAIL` -- a single cheap metadata read, but
  it advances on *any* commit, including `OPTIMIZE`/`VACUUM`, so a
  maintenance job can mask staleness. No `column:` needed.

Both DESCRIBE forms read Delta table metadata rather than a timestamp
column, so they suit a table with no reliable modified-at column. Prefer
`describe_history` when maintenance jobs run against the table (it tracks
real data changes); `describe_detail` is the lighter option when they don't.

```yaml
checks:
  # last real data change (WRITE/MERGE/DELETE/UPDATE); ignores OPTIMIZE/VACUUM
  - source: warehouse
    object: main.sales.orders
    metric: freshness
    freshness_source: describe_history
    expect: { max_lag: 26h }

  # the table's lastModified metadata (advances on any commit)
  - source: warehouse
    object: main.sales.orders
    metric: freshness
    freshness_source: describe_detail
    expect: { max_lag: 26h }
```

Freshness can opt into the business calendar (`calendar: business`) instead
of wall-clock lag -- see [Calendar & scheduling](calendar.md).

## Applicability matrix

Which column-level checks are offered for which canonical column category
(`numeric`, `temporal`, `string`, `boolean`, `other`) is generated
directly from the same mapping the [configurator](authoring-checks.md) uses
to propose checks, so the matrix can never disagree with the
wizard's actual offers: see the
[generated applicability matrix](reference/matrix.md). Table-level checks
(`row_count`, `schema`, assertions) apply regardless of column category.

## Per-engine notes

- **Freshness DESCRIBE forms are Databricks-table-only.** `describe_history`
  / `describe_detail` read Delta table metadata; config validation rejects
  them on any other engine, and on a Databricks *view* (Delta metadata
  describes tables, not views) -- both fail validation without an
  engine-name check in the compiler, purely from the dialect's declared
  freshness capabilities.
- **Row-limiting form.** Assertion evidence and the configurator's catalog
  probes are capped per dialect: `LIMIT n` (sqlite, Databricks, PostgreSQL,
  MySQL), `TOP n` (SQL Server / T-SQL), or `FETCH FIRST n ROWS ONLY` /
  a `ROWNUM` wrapper (Oracle, when supported).
- **Native type-name variances behind the canonical categories.** Schema
  fingerprints hash the *native* type name (`INTEGER`, `varchar(50)`,
  `timestamp_ntz`, ...) so drift detection is exact per engine, but every
  other engine-agnostic check (the applicability matrix, the configurator's
  offers) keys off the canonical `category` an adapter maps that native
  name to -- never off the native name itself.
- **Oracle specifics**, should an Oracle adapter be added: columns
  reflect via `ALL_TAB_COLUMNS` rather than the generic SQLAlchemy
  reflection path most engines get for free, and row limiting uses
  `FETCH FIRST … ROWS ONLY` (12c+) or a `ROWNUM` wrapper on older versions --
  both are Dialect-level variances, not changes to the compiler.
