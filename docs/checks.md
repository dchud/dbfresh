# Check reference

Every check is one of the metrics below (compiled to a single scalar query
and compared via an expectation, §6.1) or an assertion. Object names are
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

### Assertions

`assert: "<predicate>"` compiles to
`SELECT * FROM obj WHERE NOT (<predicate>)`, capped by the dialect's row
limiting form (20 rows fetched, 10 shown in the digest); `assert_sql:` lets
you supply the whole violation-selecting query directly for anything the
predicate form can't express. The persisted value is the violation row
count.

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
- `describe_history` / `describe_detail` -- Databricks-only, table-only
  (see below); read Delta table metadata instead of querying a column at
  all, so no `column:` is needed.

Freshness can opt into the business calendar (`calendar: business`) instead
of wall-clock lag -- see [Calendar & scheduling](calendar.md).

## Applicability matrix

Which column-level checks are offered for which canonical column category
(`numeric`, `temporal`, `string`, `boolean`, `other`, §5.2) is generated
directly from the same mapping the [configurator](authoring-checks.md) uses
to propose checks (§11.2), so the matrix can never disagree with the
wizard's actual offers: see the
[generated applicability matrix](reference/matrix.md). Table-level checks
(`row_count`, `schema`, assertions) apply regardless of column category.

## Per-engine notes

- **Freshness DESCRIBE forms are Databricks-table-only.** `describe_history`
  / `describe_detail` read Delta table metadata; config validation rejects
  them on any other engine, and on a Databricks *view* (Delta metadata
  describes tables, not views) -- both fail validation without an
  engine-name check in the compiler, purely from the dialect's declared
  freshness capabilities (§5.3).
- **Row-limiting form.** Assertion evidence and the configurator's catalog
  probes are capped per dialect: `LIMIT n` (sqlite, Databricks, PostgreSQL,
  MySQL), `TOP n` (SQL Server / T-SQL), or `FETCH FIRST n ROWS ONLY` /
  a `ROWNUM` wrapper (Oracle, when supported).
- **Native type-name variances behind the canonical categories.** Schema
  fingerprints hash the *native* type name (`INTEGER`, `varchar(50)`,
  `timestamp_ntz`, ...) so drift detection is exact per engine, but every
  other engine-agnostic check (the applicability matrix, the configurator's
  offers) keys off the canonical `category` an adapter maps that native
  name to -- never off the native name itself (§5.2).
- **Oracle specifics**, should an Oracle adapter be added (§5.4): columns
  reflect via `ALL_TAB_COLUMNS` rather than the generic SQLAlchemy
  reflection path most engines get for free, and row limiting uses
  `FETCH FIRST … ROWS ONLY` (12c+) or a `ROWNUM` wrapper on older versions --
  both are Dialect-level variances, not changes to the compiler.
