# dbfresh — Implementation Specification

**Status:** ready for implementation
**Audience:** engineering / agent implementation team
**Baseline:** a working scaffold exists (Phase 0, below). This spec defines the
complete target system and the increments to reach it.

---

## 1. Purpose and scope

`dbfresh` answers one question cheaply and from **outside** the systems it
watches: _are the values in these tables what they should be right now?_ It is a
data-**value** validator, not a job monitor. It never inspects whether an
extract/ETL job "ran"; it inspects the data those jobs produce. A silent
empty-load or a partial extract surfaces as a count, range, freshness, or
null-rate violation.

It runs from the CLI under WSL today and is portable to a native Windows host
(Task Scheduler) later with no code change. It targets two source types:
**Microsoft SQL Server** (tables and views) and **Databricks Unity Catalog**
(Delta tables and views).

### In scope

- Per-table value checks: freshness, row-count ranges, aggregate bounds,
  null-rate / completeness, uniqueness, arbitrary SQL assertions.
- Stacking multiple checks on one table to infer extract success.
- A local **observation history** (SQLite) enabling "compare to previous run"
  and trend inspection.
- **Weekend- and holiday-aware** expectations for a weekday-heavy business.
- A copy-pasteable issues digest and machine-readable JSON output.
- Exit codes suitable for scheduler-driven alerting.

### Explicitly out of scope (for now)

- **Cross-source reconciliation** (comparing a SQL Server source to its
  Databricks target). Deliberately deferred; noted as the natural future
  extension because it is the only check that spans two connections.
- **Alerting integrations** (Slack/email/PagerDuty). The exit code + digest are
  consumed by the scheduler; notification is external.
- Anomaly detection / ML thresholds. Expectations are explicit or ratio-based.

---

## 2. Design principles

1. **Values are the signal.** Never check job status; check data. An unreachable
   source or a failing query is itself a reportable signal, never a silent pass.
2. **Two primitives.** Every check is either (a) a **metric** compared to an
   **expectation**, or (b) an **assertion** query that must return zero rows.
   Builtins are sugar over these two. This keeps the config small and the engine
   simple.
3. **Definitions in git, observations in SQLite.** Check definitions live in
   version-controlled YAML — diffable, reviewable, deployed with the pipelines.
   Only _what each run observed_ goes in SQLite. Config is config; data is data.
4. **Fail safe and loud.** Connection failure, permission error, missing column,
   empty result — each maps to an explicit non-OK status, never to OK.
5. **Portable.** No dependency that ties the tool to Linux or Windows. SQL auth,
   pure-Python drivers, standard-library scheduling via exit codes.
6. **Stable identity.** A check's history continuity must survive threshold
   tuning; identity is derived from _what_ is measured, not the pass/fail bound.

---

## 3. Architecture

```
dbfresh/
  connection.py      parse usql/go-mssqldb URL -> SqlServerParams
  checks.py          Check model, durations, expectations, SQL compilation
  calendar.py        business-day / holiday calendar  (NEW, Phase 2)
  store.py           SQLite observation store          (NEW, Phase 1)
  adapters/
    base.py          Adapter protocol + factory
    sqlserver.py     pymssql adapter
    databricks.py    databricks-sql-connector adapter
  engine.py          run checks per source, evaluate, persist
  report.py          rich progress + plain-text digest + JSON
  cli.py             entrypoint: run / add / history
config.example.yaml
pyproject.toml
tests/
```

**Data flow per run:** load & validate config → open one connection per source
→ for each check, select the effective expectation for today (weekday/holiday) →
compile to SQL → execute (scalar / timestamp / rows) → evaluate against
expectation and/or prior observation → produce `Result` → persist observation →
render digest → exit with worst status.

**Concurrency:** checks are grouped by source; each source runs on its own
connection in a worker thread (sources in parallel, one connection never shared
across threads). Per-check parallelism within a source is out of scope; at the
expected scale (tens to low hundreds of checks) serial-within-source is fine.

**Adapter contract** (`adapters/base.py`): each adapter exposes a `dialect`
string and three methods — `scalar(sql) -> Any`, `rows(sql) -> list[dict]`,
`close()`. Everything else is built on these. Adding a source type = one new
adapter + one line in the factory.

---

## 4. Sources and connections

Secrets never appear in the YAML. Any `${VAR}` token is interpolated from the
environment at load time; a missing variable is a hard error (fail fast rather
than connect with an empty password).

### 4.1 SQL Server

- **Auth:** SQL authentication via a usql-style connection URL kept in an env
  var. Credentials are inline in the URL, so no Kerberos and no ODBC driver
  setup — important because Windows/integrated auth from WSL requires a full
  Kerberos stack (krb5.conf, kinit, SPN, ticket renewal) and is the hard path.
- **Driver:** `pymssql` (bundles FreeTDS; usually installs with no system
  packages under WSL). Swapping to `pyodbc` must be isolated to
  `adapters/sqlserver.py`.
- **URL parsing** (`connection.py`): accept scheme `sqlserver` / `mssql` / `ms`.
  Format: `sqlserver://user:pass@host:port/PATH?param=value`. Disambiguate the
  path segment:
  - if `?database=` is present, it is the database and the path segment is the
    **instance**;
  - otherwise the path segment is the **database** (dburl behavior).
    URL-decode user/password/database. Default port 1433. A named instance is
    addressed to pymssql as `server="host\\instance"` with the port omitted.
- **Future portability:** when the tool later runs as a native Windows process,
  the same URL can be swapped for a `Trusted_Connection=yes` form to use Windows
  auth; the adapter should accept a pre-built connection kwargs override to make
  this a config change, not a code change.

### 4.2 Databricks (Unity Catalog)

- **Auth/driver:** `databricks-sql-connector` against a **SQL warehouse
  endpoint** (serverless recommended — wakes on demand, auto-stops) with a
  personal access token. Config fields: `host`, `http_path`, `token`.
- **Freshness metadata — critical implementation note.** For the `freshness`
  metric, prefer a trusted timestamp column (`MAX(col)`), which the data owners
  have confirmed exists on the target tables. For tables/views lacking a good
  column, the metadata fallbacks must use one of:
  - `DESCRIBE HISTORY <table>` filtered to data operations
    (`WHERE operation IN ('WRITE','MERGE','DELETE','UPDATE')`, then `MAX(timestamp)`)
    — most precise; excludes `OPTIMIZE`/`VACUUM` noise; history retained ~30 days
    by default (`delta.logRetentionDuration`);
  - `DESCRIBE DETAIL <table>` → `lastModified` — simpler single value, includes
    structure changes.
    **Do NOT** use `information_schema.tables.last_altered` for data freshness — it
    tracks DDL only (schema/metadata/properties) and does not move on
    inserts/updates/deletes. Views cannot use either DESCRIBE form; they must use a
    timestamp column.

---

## 5. Check model

### 5.1 The two primitives

- **metric + expectation.** Compile to a single scalar-returning query; compare
  the scalar to the expectation. Freshness is a special metric whose scalar is a
  timestamp and whose comparison is a lag.
- **assertion.** A query (or a boolean `assert:` predicate compiled to
  `SELECT * FROM <obj> WHERE NOT (<predicate>)`) that must return zero rows. Any
  returned rows are the violations and are surfaced (capped, e.g. 20 fetched /
  10 shown) as evidence. For assertions, the persisted metric value is the
  **violation row count**, so "how many bad rows over time" trends for free.

### 5.2 Builtin metrics and their SQL semantics

Object names are used verbatim as provided (fully qualified by the author, e.g.
`dbo.fct_sales`, `main.gold.customer_360`); no automatic quoting. An optional
`where:` clause is appended to every metric query.

| metric                  | required fields | SQL (dialect-adjusted)                                                                           | scalar type |
| ----------------------- | --------------- | ------------------------------------------------------------------------------------------------ | ----------- |
| `row_count`             | —               | `SELECT COUNT(*) FROM obj [WHERE ...]`                                                           | int         |
| `null_rate`             | `column`        | `SELECT CAST(SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END) AS FLOAT)/NULLIF(COUNT(*),0) FROM obj` | float 0–1   |
| `duplicate_count`       | `key`           | `SELECT COUNT(*) - COUNT(DISTINCT key) FROM obj`                                                 | int         |
| `sum`/`avg`/`min`/`max` | `column`        | `SELECT AGG(col) FROM obj`                                                                       | number      |
| `freshness`             | `column`        | `SELECT MAX(col) FROM obj` → lag computed in Python                                              | timestamp   |

Dialect differences the compiler must handle: assertion row-capping uses `TOP n`
for `tsql` and `LIMIT n` for `databricks`. Composite keys for `duplicate_count`
are out of scope for v1 (single key column only); note as a future extension.

### 5.3 Expectations

An expectation is a single-operator mapping evaluated against the scalar:

| operator             | meaning                               |
| -------------------- | ------------------------------------- |
| `between: [lo, hi]`  | `lo <= v <= hi` (inclusive)           |
| `max` / `lte`        | `v <= x`                              |
| `min` / `gte`        | `v >= x`                              |
| `equals` / `eq`      | `v == x`                              |
| `lt` / `gt`          | strict                                |
| `max_lag: <dur>`     | freshness only; `now - max_ts <= dur` |
| `vs_previous: {...}` | history-based; see §7                 |

Durations parse compound forms: `26h`, `2d`, `90m`, `45s`, `1h30m`. A `null`
scalar (e.g. empty table, `MAX` of no rows) fails the expectation unless the
check opts into `allow_empty: true`.

### 5.4 Severity

Each check has `severity: error | warn` (default `error`). A failing `warn`
check yields status `WARN` (exit ≤1) instead of `FAIL` (exit 2). Used for soft
bounds that should be seen but not page.

---

## 6. Temporal handling — weekends and holidays

A weekday-heavy business needs both weekend and holiday awareness. Two
independent, composable mechanisms; both must be implemented.

### 6.1 The business calendar (`calendar.py`)

A single calendar is defined once at the top level and referenced by checks:

```yaml
calendar:
  timezone: America/New_York
  workdays: [mon, tue, wed, thu, fri] # default; the rest are non-business
  holidays:
    country: US # via the `holidays` package
    subdivision: null # e.g. state code, optional
    extra: ["2026-11-27"] # explicit additional dates
    remove: [] # dates to treat as workdays anyway
```

- Holiday dates come from the `holidays` package (`holidays.country_holidays`)
  for the configured country/subdivision, unioned with `extra`, minus `remove`.
- A **business day** is a `workdays` weekday that is not a holiday.
- The calendar exposes: `is_business_day(date)`, `previous_business_day(date)`,
  and `business_time_between(t0, t1)` (see §6.3).
- All weekday/holiday logic evaluates in the calendar `timezone`.

### 6.2 Per-weekday / holiday expectation overrides

A check may override its expectation based on the **weekday of the run**:

```yaml
- source: warehouse
  object: dbo.fct_sales
  metric: row_count
  expect: { between: [10000, 500000] } # default (Tue–Fri)
  by_weekday:
    mon: { between: [0, 500000] } # Monday reflects a quiet weekend
    sat: { max: 100 }
    sun: { max: 100 }
  on_holiday: { max: 100 } # optional; used when today is a holiday
```

Selection precedence for the effective expectation:
`on_holiday` (if today is a holiday and the key is present) → `by_weekday[today]`
(if present) → base `expect`. Selection uses the run's current date in the
calendar timezone.

### 6.3 Business-time freshness

Freshness may opt into the business calendar:

```yaml
- metric: freshness
  column: modified_at
  expect: { max_lag: 26h }
  calendar: business # default is wall-clock
```

- **Wall-clock (default):** `lag = now - max_ts`.
- **Business:** `lag = business_time_between(max_ts, now)`, defined as wall-clock
  elapsed **minus 24h for each whole non-business date strictly between** the two
  timestamps' calendar dates. Example: data last written Fri 18:00, checked Mon
  07:00 — Sat and Sun are non-business, so business lag ≈ 61h − 48h = 13h, which
  passes a 26h threshold instead of tripping it.
- This definition is intentionally simple and fully testable. An alternative
  ("passes iff `max_ts` is at/after the previous expected load boundary") was
  considered and rejected for v1 as more complex; note it as a possible future
  mode.

### 6.4 Skipping on non-business days

Global and per-check `skip_on_holiday: true` (and implicit skip when the run
lands on a non-workday) causes affected checks to be recorded as status
`SKIPPED` (exit 0, excluded from failure counts) rather than evaluated. Default
`false`; most checks should still run, using overrides, so `SKIPPED` is for
checks that are meaningless off-schedule.

---

## 7. Observation store (SQLite) and history-based expectations

### 7.1 Rationale and schema

Definitions stay in YAML; the store holds only observations, enabling
"today vs previous" comparisons and trend inspection. Default path `./dbfresh.db`
(configurable via `--store` / `store:` in config).

```sql
CREATE TABLE run (
  run_id     INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,          -- ISO 8601, UTC
  finished_at TEXT,
  status     TEXT NOT NULL,          -- worst status of the run
  git_sha    TEXT                    -- optional: config provenance
);

CREATE TABLE observation (
  run_id    INTEGER NOT NULL REFERENCES run(run_id),
  check_id  TEXT    NOT NULL,        -- stable identity, see §7.2
  source    TEXT    NOT NULL,
  object    TEXT    NOT NULL,
  metric    TEXT,                    -- null for raw assertions
  label     TEXT    NOT NULL,
  value     REAL,                    -- scalar metric, or violation count
  status    TEXT    NOT NULL,        -- OK|WARN|FAIL|ERROR|SKIPPED
  observed_at TEXT  NOT NULL,        -- ISO 8601, UTC
  weekday   INTEGER NOT NULL         -- 0=Mon..6=Sun, in calendar tz
);
CREATE INDEX ix_obs_checkid_time ON observation(check_id, observed_at);
```

Every check writes one observation per run, including OK and ERROR. Retention is
configurable (`store.retain_days`, default unlimited); a `dbfresh prune` command
may enforce it. Timestamps stored in UTC; `weekday` stored in calendar tz so
"same weekday last week" queries are trivial.

### 7.2 Stable `check_id`

Continuity of history must survive threshold edits. Derivation:

- if the check declares an explicit `id:`, use it verbatim;
- else compute a deterministic hash over the **identity tuple**: `source`,
  `object`, `metric`, and the discriminating field (`column` / `key`), or for
  assertions the normalized `assert` / `assert_sql` text. **The expectation is
  NOT part of the identity.**
  Two checks with the same identity in one config is a validation error (ambiguous
  history); require an explicit `id:` to disambiguate intentional duplicates.

### 7.3 `vs_previous` expectations

Compares the current scalar to a prior observation of the same `check_id`:

```yaml
metric: row_count
expect:
  vs_previous:
    baseline: previous # previous | last_same_weekday
    min_ratio: 0.5 # current/baseline within [0.5, 2.0]
    max_ratio: 2.0
    # optional absolute guards instead of / in addition to ratios:
    # min_delta / max_delta
    on_missing: pass # pass | warn | skip   (no baseline available)
```

- `baseline: previous` selects the most recent prior observation with status in
  {OK, WARN, FAIL} (ERROR/SKIPPED excluded). Because runs are daily, this is the
  "about one day later" comparison.
- `baseline: last_same_weekday` selects the most recent prior observation whose
  `weekday` equals today's and whose `observed_at` is ≥ ~6 days earlier — the
  correct baseline for a weekday-heavy business (compare Monday to last Monday).
- Ratio guards require baseline ≠ 0; if baseline is 0, fall back to delta guards
  if present, else treat per `on_missing`.
- First run / no baseline: behavior per `on_missing` (default `pass`).
- `vs_previous` may combine with a static `expect` on the same check by nesting
  under a list; v1 may restrict to one expectation per check and add
  composition later — implementer's choice, but document it.

---

## 8. CLI surface

```
dbfresh run       [-c config.yaml] [--only SOURCE] [--json] [--no-progress]
                [--store PATH] [--no-store]
dbfresh history   OBJECT [--metric M] [-n 30] [-c config.yaml] [--store PATH]
dbfresh add       [-c config.yaml]         # interactive wizard, appends YAML
dbfresh prune     [--store PATH]           # enforce retention (optional, low pri)
```

- `run`: the core command. Progress bar (via `rich`) unless `--json` or
  `--no-progress`. Persists observations unless `--no-store`.
- `history`: reads the store and prints a check's recent values, statuses, and a
  simple trend (e.g. sparkline or delta column). Read-only.
- `add`: prompts source → object → check type → fields → expectation and appends
  a well-formed block to the config. **Emits YAML** (definitions stay in git);
  it does not write checks into SQLite.

### Exit codes (worst status across all checks)

| code | status       | meaning                    |
| ---- | ------------ | -------------------------- |
| 0    | OK / SKIPPED | all clear                  |
| 1    | WARN         | soft-bound violations only |
| 2    | FAIL         | value violations           |
| 3    | ERROR        | unreachable / query error  |

---

## 9. Reporting

- **Progress:** live bar with M-of-N completion while checks run.
- **Digest:** plain text (no rich markup, so it survives copy-paste), suitable
  for pasting into a ticket or chat. Header line with local timestamp + tz and
  pass/fail/unreachable counts, then one block per non-OK check: the qualified
  object + label, expected vs observed (or the error), and up to 10 sample
  violation rows for assertions. Example:

```
DATA CHECK REPORT — 2026-07-10 06:03 America/New_York
23 checks · 21 passed · 2 failed · 0 unreachable

✗ warehouse.dbo.fct_sales · assert amount >= 0
    3 row(s) violate the constraint
      sale_id=88213  amount=-42.00
      sale_id=88240  amount=-15.50

✗ lakehouse.main.gold.customer_360 · null_rate(email)
    expected max 0.01   observed 0.124
```

- **JSON:** `--json` emits `{status, run_id, results: [...]}` for machine
  consumption; suppresses the progress bar.

---

## 10. Configuration reference (consolidated)

```yaml
version: 1

store: ./dbfresh.db # optional; observation history
calendar: # optional; enables §6 features
  timezone: America/New_York
  workdays: [mon, tue, wed, thu, fri]
  holidays: { country: US, subdivision: null, extra: [], remove: [] }

sources:
  warehouse:
    type: sqlserver
    url: ${MSSQL_URL} # sqlserver://reader:pw@host:1433/WarehouseDB
    timeout: 30
  lakehouse:
    type: databricks
    host: ${DATABRICKS_HOST}
    http_path: ${DATABRICKS_HTTP_PATH}
    token: ${DATABRICKS_TOKEN}

defaults: # merged into every check when absent
  severity: error

checks:
  - source: warehouse
    object: dbo.fct_sales
    id: sales_amount_nonneg # optional stable id
    assert: "amount >= 0"

  - source: warehouse
    object: dbo.fct_sales
    metric: row_count
    expect: { between: [10000, 500000] }
    by_weekday:
      mon: { between: [0, 500000] }
      sat: { max: 100 }
      sun: { max: 100 }
    on_holiday: { max: 100 }

  - source: warehouse
    object: dbo.fct_sales
    metric: freshness
    column: modified_at
    expect: { max_lag: 26h }
    calendar: business

  - source: lakehouse
    object: main.gold.customer_360
    metric: null_rate
    column: email
    expect: { max: 0.01 }

  - source: lakehouse
    object: main.gold.customer_360
    metric: row_count
    expect:
      vs_previous:
        {
          baseline: last_same_weekday,
          min_ratio: 0.5,
          max_ratio: 2.0,
          on_missing: pass,
        }
```

---

## 11. Error handling and edge cases (required behavior)

- **Unreachable source / auth failure:** every check on that source → `ERROR`
  (exit 3), with the exception text in the digest. Never OK.
- **Query error** (bad column, permission, syntax): that check → `ERROR`.
- **Empty table / null scalar:** fails the expectation unless `allow_empty:true`.
- **`null_rate` / ratio with zero denominator:** `NULLIF` yields null → treat as
  `ERROR` for null_rate on an empty table; for `vs_previous`, zero baseline is
  handled per §7.3.
- **Timezone:** all weekday/holiday/business-day decisions use the calendar
  timezone; stored timestamps are UTC; freshness compares tz-aware values
  (naive DB timestamps are assumed UTC — document and make configurable).
- **No calendar configured:** `by_weekday`/`on_holiday`/`calendar: business`
  used without a `calendar:` block is a config validation error.
- **Config validation runs before any connection** and reports all problems at
  once (unknown source refs, duplicate identities, unknown metrics/operators,
  missing required fields).

---

## 12. Testing requirements

The team must deliver tests covering at least:

- **URL parsing:** dburl-style vs native-style, default port, URL-encoded
  password, scheme aliases, missing-database and bad-scheme errors.
- **Durations & expectations:** all operators, compound durations, boundary
  (inclusive) cases.
- **SQL compilation:** each builtin, dialect differences (`TOP` vs `LIMIT`),
  `where` clause, assertion predicate vs raw `assert_sql`.
- **Calendar:** business-day/holiday classification, `business_time_between`
  across weekends and holidays, `previous_business_day`, `last_same_weekday`
  selection.
- **Store:** `check_id` stability across expectation edits; observation
  round-trip; `vs_previous` baseline selection incl. missing-baseline paths.
- **Engine end-to-end:** full compile→execute→evaluate against an in-memory
  **sqlite** stand-in adapter (open with `check_same_thread=False` because
  sources run in worker threads), covering OK/WARN/FAIL/ERROR/SKIPPED and
  exit-code aggregation.

Property-based tests are encouraged for the calendar arithmetic and duration
parsing.

---

## 13. Build plan (phased, with acceptance criteria)

**Phase 0 — baseline scaffold (DONE).** Package skeleton; connection parser;
SQL Server + Databricks adapters; two primitives; builtins `row_count`,
`null_rate`, `duplicate_count`, `sum/avg/min/max`, `freshness`; YAML config with
env interpolation; rich progress + digest + JSON; exit codes; sqlite-backed
engine tests. _Accept:_ `pytest` green; digest renders as in §9.

**Phase 1 — observation store.** Implement `store.py`, `run`/`observation`
schema, stable `check_id` (§7.2), persist every observation, `--store` /
`--no-store`, and `dbfresh history`. _Accept:_ a run writes one observation per
check; editing a threshold does not change `check_id`; `history` prints recent
values.

**Phase 2 — calendar (weekends + holidays).** Implement `calendar.py`
(`holidays` package integration), `by_weekday` / `on_holiday` expectation
selection, `calendar: business` freshness, `skip_on_holiday` → `SKIPPED`.
_Accept:_ Monday run selects the Monday expectation; Friday-evening data reads
as fresh on Monday under `business`; a configured holiday is treated as
non-business.

**Phase 3 — history-based expectations.** Implement `vs_previous` with
`baseline: previous | last_same_weekday`, ratio and delta guards, `on_missing`.
_Accept:_ a 3× row-count swing vs the correct baseline fails; first run passes
under default `on_missing`.

**Phase 4 — configurator & polish.** `dbfresh add` wizard emitting YAML; `dbfresh
prune`; docs. _Accept:_ wizard appends a valid block that `run` executes; prune
enforces retention.

Phases 1–3 are the core value and should ship together if possible; Phase 4 is
convenience.

---

## 14. Open decisions for the team / product owner

1. **Naive DB timestamp timezone:** assume UTC (current plan) or make it a
   per-source setting? Affects freshness correctness for local-time columns.
2. **One expectation per check vs composition** (static `expect` _and_
   `vs_previous` on the same check): v1 restricts to one unless the team opts to
   support a list. Pick one and document.
3. **Holiday library vs static list:** default to the `holidays` package, but
   confirm the country/subdivision(s) in play and whether an org-specific
   closure calendar (e.g. company holidays) should be a first-class `extra`
   source.
4. **Retention default:** unlimited vs a sane cap (e.g. 400 days, enough for
   `last_same_weekday` plus a year of trend).
