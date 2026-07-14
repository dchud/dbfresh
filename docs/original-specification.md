> **Historical document.** This is the original implementation specification and phased build plan for dbfresh, preserved as a record of where the project started. It is not maintained against the shipped code — the current design and usage are documented at the [documentation site](https://dchud.github.io/dbfresh/) and in the source. Details below may differ from what was built.

# dbfresh — Implementation Specification

**Status:** ready for implementation
**Audience:** engineering / agent implementation team
**Baseline:** the repository is bootstrapped — uv-managed package skeleton
(`src/dbfresh/`), CLI stub, one smoke test, tooling (ruff, pytest, just), and a
beads workspace. No engine code exists yet. This spec defines the complete
target system; §16 sequences it into epics, starting from the engine core.

---

## 1. Purpose and scope

`dbfresh` answers one question cheaply and from **outside** the systems it
watches: _are the values in these tables what they should be right now?_ It is a
data-**value** validator, not a job monitor. It never inspects whether an
extract/ETL job "ran"; it inspects the data those jobs produce. A silent
empty-load or a partial extract surfaces as a count, range, freshness, or
null-rate violation.

It is a CLI tool for unix-like (POSIX) hosts; WSL is the initial deployment
target, not a design constraint. Native Windows (Task Scheduler, integrated
auth) is a possible future target that no current work addresses — the design
keeps it reachable (exit-code scheduling, pure-Python drivers) and nothing
more. It supports two source types: **Microsoft SQL Server** (tables and
views) and **Databricks Unity Catalog** (Delta tables and views).

### In scope

- Two tiers of value checks: **table-level** (row-count ranges, schema/shape
  stability, whole-table assertions) and **column-level** (freshness, aggregate
  bounds via `sum`/`avg`/`min`/`max`, null-rate / completeness, uniqueness). A
  check is table-level when it targets no column and column-level when it names
  one; the distinction is organizational — it drives the configurator and the
  dashboard — not a separate config field.
- Arbitrary SQL assertions that must return zero rows.
- Stacking multiple checks on one table to infer extract success.
- A local **observation history** (SQLite) enabling "compare to previous run"
  and trend inspection.
- **Weekend- and holiday-aware** expectations for a weekday-heavy business.
- Two front-ends over one engine: a **batch CLI** (progress, copy-pasteable
  digest, machine-readable JSON, scheduler exit codes) and an **interactive
  Textual TUI** (status dashboard, configurator, history browser). The batch path
  stays the primary contract for scheduler-driven alerting; the TUI is an
  additional consumer of the same engine and store.
- Exit codes suitable for scheduler-driven alerting.
- A **team-shareable configuration** layout: one committed config (optionally
  split across files) that runs unchanged in every environment, with secrets
  and machine-local paths supplied outside git (§12).
- An **adapter/dialect contract** that keeps everything above the adapter
  layer engine-agnostic, so further engines (PostgreSQL, MySQL/MariaDB,
  Oracle, and their cloud-hosted variants) are additive work (§5).
- A published **mkdocs-material documentation site** (§14).

### Explicitly out of scope (for now)

- **Cross-source reconciliation** (comparing a SQL Server source to its
  Databricks target). Deliberately deferred; noted as the natural future
  extension because it is the only check that spans two connections.
- **Alerting integrations** (Slack/email/PagerDuty). The exit code + digest are
  consumed by the scheduler; notification is external.
- Anomaly detection / ML thresholds. Expectations are explicit or ratio-based.
- **Additional supported engines.** v1 ships and supports SQL Server and
  Databricks only. The contract that makes further engines cheap is in scope
  (§5) and is validated by a reference adapter (§16), but no third engine is a
  supported v1 target.
- **Native Windows support.** Deferred; no Windows-specific implementation
  work in v1.

---

## 2. Design principles

1. **Values are the signal.** Never check job status; check data. An unreachable
   source or a failing query is itself a reportable signal, never a silent pass.
2. **Two primitives.** Every check is either (a) a **metric** compared to an
   **expectation**, or (b) an **assertion** query that must return zero rows. A
   metric's observed value may be numeric, a timestamp (freshness), or a schema
   fingerprint (shape) — the expectation operator matches the type. Builtins are
   sugar over these two. This keeps the config small and the engine simple.
3. **Definitions in git, observations in SQLite.** Check definitions live in
   version-controlled YAML — diffable, reviewable, deployed with the pipelines.
   The YAML holds only definitions: secrets enter via `${VAR}` interpolation
   from the environment (optionally populated per-user by a gitignored `.env`,
   §4) and machine-local data (the store path) via flag or environment, so one
   committed config serves every team member and environment. Only _what each
   run observed_ goes in SQLite. Config is config; data is data.
4. **Fail safe and loud.** Connection failure, permission error, missing column,
   empty result — each maps to an explicit non-OK status, never to OK.
5. **Portable.** Any unix-like environment is a valid host; WSL is only the
   first deployment target. No dependency ties the tool to one OS or one
   scheduler: SQL auth, pure-Python drivers, and scheduling via exit codes —
   cron, systemd timers, and (later) Windows Task Scheduler are
   interchangeable consumers.
6. **Stable identity.** A check's history continuity must survive threshold
   tuning; identity is derived from _what_ is measured, not the pass/fail bound.
7. **Engine-agnostic core.** Engine specifics live only in an Adapter and the
   Dialect it carries (§5). The engine, check compiler, calendar, store,
   reporting, and both front-ends never branch on an engine name.
8. **Automate from metadata; propose, never assume.** Where an engine's
   catalog exposes metadata — column types, key constraints, table stats —
   dbfresh uses it to do work for the user: the configurator proposes a
   complete check bundle from a table name. Proposals are always materialized
   as explicit YAML the user reviews; catalog metadata never silently changes
   run-time behavior.

---

## 3. Architecture

```
src/dbfresh/
  connection.py      build SQLAlchemy engine URLs / connect args from source config
  checks.py          Check model, durations, expectations, SQL compilation
  calendar.py        business-day / holiday calendar
  store.py           SQLite observation store
  configurator.py    front-end-agnostic authoring: introspect, propose, emit YAML
  adapters/
    base.py          Adapter protocol, Dialect model, ObjectInfo/Column, SQLAlchemy-backed base + factory
    sqlserver.py     pymssql adapter + T-SQL dialect
    databricks.py    databricks-sql-connector adapter + Databricks dialect
    sqlite.py        SQLAlchemy sqlite adapter (primary test engine; also a real source)
  engine.py          run checks per source, evaluate, persist
  report.py          rich progress + plain-text digest + JSON
  tui/               Textual app: dashboard, menu, configure, history screens
  cli.py             entrypoint: run / history / add / prune / ui
config.example.yaml
docs/                mkdocs-material documentation sources (§14)
mkdocs.yml
pyproject.toml
tests/
```

**Data flow per run:** load & validate config (root file plus includes, §12.2)
→ open one connection per source → for each check, select the effective
expectation for today (weekday/holiday) → compile to SQL → execute (scalar /
timestamp / rows) → evaluate against expectation and/or prior observation →
produce `Result` → persist observation → render digest → exit with worst
status.

**Concurrency:** checks are grouped by source; each source runs on its own
connection in a worker thread (sources in parallel, one connection never shared
across threads). Per-check parallelism within a source is out of scope; at the
expected scale (tens to low hundreds of checks) serial-within-source is fine.

**Engine boundary.** Everything engine-specific lives in `adapters/`: an
Adapter (connection handling plus four methods) and the Dialect it carries
(enumerated SQL variances and capability sets). The contract is specified in
§5. Adding a source type = one adapter module (adapter + dialect) + one
factory registration; no other module changes.

**Front-ends.** The engine and store are headless. Two consumers sit on top: the
batch CLI (`run` → digest/JSON → exit code) and the Textual TUI (`ui`). Both read
the same store and invoke the same engine; neither changes check semantics.

---

## 4. Sources and connections

Secrets never appear in the YAML. Any `${VAR}` token is interpolated from the
environment at load time; a missing variable is a hard error (fail fast rather
than connect with an empty password). At startup, before interpolation, dbfresh
loads a `.env` file if present (via `python-dotenv`), resolved next to the root
config (override with `--env-file`); real environment variables take precedence
over `.env`, so CI and production inject secrets directly while each developer
keeps an uncommitted, per-user `.env`. `.env` must be gitignored. Each source may
declare an optional `timezone:` used to interpret naive timestamps it returns
(see §13); absent, naive timestamps are assumed UTC.

### 4.1 Connection layer

The source's `type:` field selects the adapter via the factory. Adapters are
backed by a SQLAlchemy `Engine` by default (§5.1): `connection.py` turns the
source config into the SQLAlchemy URL and connect-args for the engine — SQL
Server as `mssql+pymssql://…`, sqlite as `sqlite:///…`, and future engines as
their standard `postgresql+psycopg://` / `mysql+pymysql://` URLs. An adapter
whose engine has no adequate SQLAlchemy dialect keeps its native driver behind
the same Adapter interface; Databricks uses `databricks-sql-connector` directly
(§4.3).

### 4.2 SQL Server

- **Auth:** SQL authentication via a usql-style connection URL kept in an env
  var. Credentials are inline in the URL, so no Kerberos and no ODBC driver
  setup — on unix-like hosts (WSL included), Windows/integrated auth requires
  a full Kerberos stack (krb5.conf, kinit, SPN, ticket renewal) and is the
  hard path.
- **Driver:** `pymssql` (bundles FreeTDS; usually installs with no system
  packages). Swapping to `pyodbc` must be isolated to `adapters/sqlserver.py`.
- **URL parsing** (via the `connection.py` helper): accept scheme `sqlserver` /
  `mssql` / `ms`. Format: `sqlserver://user:pass@host:port/PATH?param=value`.
  Disambiguate the path segment:
  - if `?database=` is present, it is the database and the path segment is the
    **instance**;
  - otherwise the path segment is the **database** (dburl behavior).
    URL-decode user/password/database. Default port 1433. A named instance is
    addressed to pymssql as `server="host\\instance"` with the port omitted.
- **Future portability:** if the tool later runs as a native Windows process
  (out of scope for v1), the same URL can be swapped for a
  `Trusted_Connection=yes` form to use Windows auth; the adapter should accept
  a pre-built connection kwargs override to make this a config change, not a
  code change.

### 4.3 Databricks (Unity Catalog)

- **Auth/driver:** `databricks-sql-connector` against a **SQL warehouse
  endpoint** (serverless recommended — wakes on demand, auto-stops) with a
  personal access token. Config fields: `host`, `http_path`, `token`.
- **Freshness metadata — critical implementation note.** For the `freshness`
  metric, prefer a trusted timestamp column (`MAX(col)`), which the data owners
  have confirmed exists on the target tables. For tables lacking a good
  column, the metadata fallbacks must use one of:
  - `DESCRIBE HISTORY <table>` filtered to data operations
    (`WHERE operation IN ('WRITE','MERGE','DELETE','UPDATE')`, then `MAX(timestamp)`)
    — most precise; excludes `OPTIMIZE`/`VACUUM` noise; history retained ~30 days
    by default (`delta.logRetentionDuration`);
  - `DESCRIBE DETAIL <table>` → `lastModified` — simpler single value, includes
    structure changes.
    **Do NOT** use `information_schema.tables.last_altered` for data freshness — it
    tracks DDL only (schema/metadata/properties) and does not move on
    inserts/updates/deletes. Views cannot use either DESCRIBE form; they must use
    a timestamp column. The freshness check selects among these origins with a
    `freshness_source:` field — `column` (default; `MAX(col)`),
    `describe_history`, or `describe_detail`. The two DESCRIBE forms are a
    Databricks dialect capability (§5.3), rejected at validation for views and
    for any engine whose dialect does not declare them; see §6.2.

---

## 5. Adapters and dialects

### 5.1 Adapter contract

Each adapter exposes a `dialect` (a Dialect instance, §5.3) and four methods:

- `scalar(sql) -> Any` — run a query expected to return one value.
- `rows(sql) -> list[dict]` — run a query and fetch its rows (the compiler has
  already applied the dialect's row cap).
- `describe(object) -> ObjectInfo` — the object's metadata, normalized:
  - `columns: list[Column]` — always populated; each `Column` carries `name`,
    `type` (the engine's native type name, used verbatim in schema
    fingerprints), `nullable`, and `category` (§5.2).
  - `keys: list[list[str]] | None` — column-name lists of primary-key and
    unique constraints, when the engine and object expose constraint
    metadata; `None` otherwise.
  - `approx_row_count: int | None` — a cheap catalog estimate, when available.
  - `last_modified: datetime | None` — a cheap catalog last-modified, when
    available.
- `close()`.

**SQLAlchemy-backed base.** A shared base implements the four methods over a
SQLAlchemy `Engine`: `scalar` / `rows` execute the compiler's `text()` SQL
(verbatim identifiers preserved, §5.3), and `describe` is built on SQLAlchemy's
reflection `Inspector` — `get_columns` (name, native type, nullable),
`get_pk_constraint` and `get_unique_constraints` (keys) — normalized into
`ObjectInfo`. This yields columns, types, nullability, and keys uniformly across
every SQLAlchemy-supported engine, so most adapters only declare their Dialect
and any engine-specific extras. An adapter may override any method: Databricks
supplies its own `describe` where Unity Catalog reflection is thin and reads
`DESCRIBE DETAIL` for `last_modified`. Stats that reflection does not expose
(`approx_row_count`, `last_modified`) are filled per engine or left `None`.

Everything else is built on these. `columns` powers schema-stability checks,
freshness timestamp-column validation, and object-existence checks. The
optional fields exist for the configurator (§11): they inform proposals and
are never consumed at check run time — freshness always queries per its
`freshness_source:`, row counts are always `COUNT(*)`. Which optional fields
an engine can populate is declared as a dialect introspection capability
(§5.3); a capable engine may still return `None` for an object that lacks the
metadata (e.g. a view has no key constraints), and every consumer degrades
gracefully.

Reflection covers columns and keys for every SQLAlchemy engine; only
engine-specific extras are hand-written. SQL Server adds
`sys.dm_db_partition_stats` for `approx_row_count`; Databricks overrides
`describe` for Unity Catalog and reads `DESCRIBE DETAIL` for `last_modified`;
sqlite exposes columns and keys via reflection and no stats. Future engines get
columns and keys from reflection for free (PostgreSQL adding `pg_class.reltuples`
for the estimate; Oracle via its SQLAlchemy dialect).

### 5.2 Column type categories

Each adapter maps its native type names into a canonical category vocabulary;
the native name is preserved on the `Column` (it is what schema fingerprints
hash), the category is what everything engine-agnostic keys off:

| category   | native examples                                    |
| ---------- | -------------------------------------------------- |
| `numeric`  | `int`, `bigint`, `decimal`, `float`, `money`       |
| `temporal` | `date`, `datetime2`, `timestamp`, `timestamp_ntz`  |
| `string`   | `varchar`, `nvarchar`, `char`, `string`            |
| `boolean`  | `bit`, `boolean`                                   |
| `other`    | `binary`, `variant`, `geography`, anything unknown |

Unrecognized native types map to `other`, never to an error. The
configurator's offer and proposal logic (§11.2) keys off `category` only —
never off native type names — so authoring works unchanged for any engine.

### 5.3 Dialect contract

Metric SQL compiles on an ANSI baseline that runs unchanged on every target
engine: `COUNT(*)`, `SUM(CASE WHEN … THEN 1 ELSE 0 END)`, `AVG`/`MIN`/`MAX`,
`MAX(col)`, `COUNT(DISTINCT col)`. Only the enumerated variances below live in
the Dialect. The compiler obtains each variance by asking the check's dialect;
it must not branch on an engine name — no `if dialect == "tsql"` anywhere in
`checks.py` or `engine.py`.

A Dialect declares:

- **Row limiting** — how to cap assertion evidence fetches, exposed as
  `limit(sql, n) -> sql`: `LIMIT n` (Databricks, PostgreSQL, MySQL), `TOP n`
  (T-SQL), `FETCH FIRST n ROWS ONLY` (standard form; Oracle 12c+), or a
  `ROWNUM` wrapper (legacy Oracle).
- **Float coercion** — the form that makes the null-rate division float. The
  portable default multiplies by `1.0`
  (`SUM(...) * 1.0 / NULLIF(COUNT(*), 0)`); a dialect may override with an
  explicit cast such as `CAST(... AS FLOAT)`.
- **Identifier policy** — `verbatim` for all v1 engines: object names are
  author-qualified and interpolated as written, no automatic quoting (§13).
  The policy lives in the dialect so a future engine that needs quoting has a
  single declared place for it.
- **Freshness capabilities** — which `freshness_source:` options the engine
  supports. Baseline is `{column}`; the Databricks dialect adds
  `describe_history` and `describe_detail`. Config validation consults this
  set, so a metadata-based freshness check on an engine that lacks it fails
  validation without any engine-name test.
- **Introspection capabilities** — which optional `ObjectInfo` fields the
  engine can populate: `keys`, `stats` (`approx_row_count` /
  `last_modified`). The configurator uses this to distinguish "this engine
  cannot say" from "this object has none".

### 5.4 Extending to new source types

Everything above the adapter layer — engine, check compiler, expectations,
calendar, store, reporting, CLI, and TUI — is engine-agnostic. A new engine is
one module plus one factory registration. Worked sketch for PostgreSQL:

1. `adapters/postgres.py`: subclass the SQLAlchemy-backed base with a
   `postgresql+psycopg://user:pass@host:5432/db` engine URL. `scalar`, `rows`,
   and `describe` (columns + keys) are inherited from the base's reflection;
   only the category mapping for PostgreSQL's native type names and an optional
   `pg_class.reltuples` row estimate are engine-specific.
2. A Dialect instance: row limiting `LIMIT n`, default float coercion,
   `verbatim` identifiers, freshness capabilities `{column}`, introspection
   capabilities `{keys, stats}`.
3. One registration line in the adapter factory; `type: postgres` in config
   now resolves.

No change to `checks.py`, `engine.py`, `calendar.py`, `store.py`,
`report.py`, the CLI, or the TUI. MySQL/MariaDB is the same shape (`LIMIT`,
reflection); Oracle differs only in row limiting (`FETCH FIRST … ROWS ONLY`)
and its category mapping, both via its SQLAlchemy dialect. Cloud-hosted variants
(RDS, Azure SQL, Cloud SQL) are connection-string differences, not new adapters.
v1 supports SQL Server and Databricks; the implementation plan validates this
contract by building a reference PostgreSQL adapter (§16).

---

## 6. Check model

### 6.1 The two primitives

- **metric + expectation.** Compile to a single scalar-returning query; compare
  the scalar to the expectation. Freshness is a special metric whose scalar is a
  timestamp and whose comparison is a lag.
- **assertion.** A query (or a boolean `assert:` predicate compiled to
  `SELECT * FROM <obj> WHERE NOT (<predicate>)`) that must return zero rows. Any
  returned rows are the violations and are surfaced as evidence, capped via the
  dialect's row-limiting form (§5.3): 20 rows fetched, 10 shown in the digest.
  For assertions, the persisted metric value is the **violation row count**, so
  "how many bad rows over time" trends for free.

### 6.2 Builtin metrics and their SQL semantics

Object names are used verbatim as provided (fully qualified by the author, e.g.
`dbo.fct_sales`, `main.gold.customer_360`); no automatic quoting. An optional
`where:` clause is appended to every metric query.

**Tiers.** A check is **table-level** when it targets no column (`row_count`,
`schema`, whole-table assertions) and **column-level** when it names a `column`
or `key`. Tier is derived, not declared; it groups checks in the configurator and
the dashboard.

| metric                  | tier   | required   | SQL (ANSI baseline)                                                                     | scalar type |
| ----------------------- | ------ | ---------- | --------------------------------------------------------------------------------------- | ----------- |
| `row_count`             | table  | —          | `SELECT COUNT(*) FROM obj [WHERE ...]`                                                  | int         |
| `schema`                | table  | —          | column metadata via `describe(obj)` → fingerprint                                      | fingerprint |
| `null_rate`             | column | `column`   | `SELECT SUM(CASE WHEN col IS NULL THEN 1 ELSE 0 END)*1.0/NULLIF(COUNT(*),0) FROM obj`   | float 0–1   |
| `duplicate_count`       | column | `key`      | `SELECT COUNT(*) - COUNT(DISTINCT key) FROM obj WHERE key IS NOT NULL [AND ...]`        | int         |
| `sum`/`avg`/`min`/`max` | column | `column`   | `SELECT AGG(col) FROM obj`                                                              | number      |
| `freshness`             | column | `column` † | `SELECT MAX(col) FROM obj` → lag computed in Python                                     | timestamp   |

The float coercion in `null_rate` and the assertion row cap come from the
dialect (§5.3); everything else in the table is engine-independent. Composite
keys for `duplicate_count` are out of scope for v1 (single key column only);
note as a future extension.

† freshness `freshness_source:` selects the timestamp origin: `column`
(default) uses `MAX(col)` and requires `column:`; `describe_history` /
`describe_detail` (a Databricks dialect capability; tables only) read Delta
metadata (§4.3) and need no column. `duplicate_count` counts duplicates among
non-null keys only — `COUNT(DISTINCT key)` already ignores nulls, and the
`key IS NOT NULL` guard stops `COUNT(*)` from counting null-keyed rows as
duplicates. Central tendency beyond `avg` (median, stddev, percentiles) is
deferred for v1 to avoid engine-specific percentile SQL.

**Schema (shape) check.** `metric: schema` reads the object's columns via
`describe(obj)` and reduces them to a fingerprint: a stable hash over the
order-insensitive set of `(column_name, data_type)` pairs (column order and
nullability are excluded in v1; `data_type` is the native type name, so
fingerprints are per-engine, which is fine because checks are per-source). The
observed value is the fingerprint string; its expectation is `unchanged`
(compare to the previous observation) or `equals` (a pinned fingerprint) — see
§6.3. On drift, the digest shows the added / removed / retyped columns.

### 6.3 Expectations

An expectation is a single-operator mapping evaluated against the scalar:

| operator             | meaning                                                     |
| -------------------- | ----------------------------------------------------------- |
| `between: [lo, hi]`  | `lo <= v <= hi` (inclusive)                                 |
| `max` / `lte`        | `v <= x`                                                    |
| `min` / `gte`        | `v >= x`                                                    |
| `equals` / `eq`      | `v == x`                                                    |
| `lt` / `gt`          | strict                                                      |
| `max_lag: <dur>`     | freshness only; `now - max_ts <= dur`                       |
| `vs_previous: {...}` | numeric metrics only; history-based; see §8                 |
| `unchanged: true`    | schema only; fingerprint equals the previous observation's  |

Durations parse compound forms: `26h`, `2d`, `90m`, `45s`, `1h30m`. A `null`
scalar (e.g. empty table, `MAX` of no rows) fails the expectation unless the
check opts into `allow_empty: true`.

One expectation per check in v1: a check carries exactly one operator (a static
bound, `max_lag`, `vs_previous`, `unchanged`, or `equals`), never several. `{min:
x, max: y}` on one check is a validation error — use `between`. Composing a static
bound with `vs_previous` is a documented future extension.

`vs_previous` (§8.3) applies only to numeric metrics; it is a validation error on
`freshness` and `schema`. Freshness history comparison is out of scope for v1;
schema history comparison uses `unchanged`.

### 6.4 Severity

Each check has `severity: error | warn` (default `error`). A failing `warn`
check yields status `WARN` (exit ≤1) instead of `FAIL` (exit 2). Used for soft
bounds that should be seen but not page.

---

## 7. Temporal handling — weekends and holidays

A weekday-heavy business needs both weekend and holiday awareness. Two
independent, composable mechanisms; both must be implemented.

### 7.1 The business calendar (`calendar.py`)

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
  The concrete jurisdiction is a deployment-time configuration choice, not a
  code constant; organization-specific closures go in `extra:`.
- A **business day** is a `workdays` weekday that is not a holiday.
- The calendar exposes: `is_business_day(date)`, `previous_business_day(date)`,
  and `business_time_between(t0, t1)` (see §7.3).
- All weekday/holiday logic evaluates in the calendar `timezone`.

### 7.2 Per-weekday / holiday expectation overrides

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

### 7.3 Business-time freshness

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
  timestamps' calendar dates (both converted to the calendar timezone first).
  Example: data last written Fri 18:00, checked Mon
  07:00 — Sat and Sun are non-business, so business lag ≈ 61h − 48h = 13h, which
  passes a 26h threshold instead of tripping it.
- This definition is intentionally simple and fully testable. An alternative
  mode ("passes iff `max_ts` is at/after the previous expected load boundary")
  is deferred as more complex; note it as a possible future mode.

### 7.4 Skipping on non-business days

Global and per-check `skip_off_schedule: true` (alias `skip_on_holiday`) records
the affected checks as status `SKIPPED` (exit 0, excluded from failure counts)
rather than evaluating them, whenever the run date is **not a business day** — a
weekend (non-`workdays` day) or a holiday. Default `false`; most checks should
still run using `by_weekday` / `on_holiday` overrides, so `SKIPPED` is reserved
for checks that are meaningless off-schedule. Skipping requires a configured
`calendar:`.

---

## 8. Observation store (SQLite) and history-based expectations

### 8.1 Rationale and schema

Definitions stay in YAML; the store holds only observations, enabling
"today vs previous" comparisons and trend inspection. The store path resolves
with precedence `--store` flag → `DBFRESH_STORE` env var → `store.path` in
config → default `./dbfresh.db`. Relative config paths — the default included —
resolve against the root config's directory (§12.3), so each clone of the
config repo gets its own store file without committing a machine-specific
path.

```sql
CREATE TABLE run (
  run_id     INTEGER PRIMARY KEY,
  started_at TEXT NOT NULL,          -- ISO 8601, UTC
  finished_at TEXT,
  status     TEXT NOT NULL,          -- worst status of the run
  git_sha    TEXT                    -- config provenance; null if unavailable
);

CREATE TABLE observation (
  run_id    INTEGER NOT NULL REFERENCES run(run_id),
  check_id  TEXT    NOT NULL,        -- stable identity, see §8.2
  source    TEXT    NOT NULL,
  object    TEXT    NOT NULL,
  metric    TEXT,                    -- null for raw assertions
  label     TEXT    NOT NULL,
  value     REAL,                    -- numeric scalar, violation count, or freshness lag (seconds)
  value_text TEXT,                   -- non-numeric observed value (schema fingerprint)
  status    TEXT    NOT NULL,        -- OK|WARN|FAIL|ERROR|SKIPPED
  observed_at TEXT  NOT NULL,        -- ISO 8601, UTC
  weekday   INTEGER NOT NULL         -- 0=Mon..6=Sun, in calendar tz (UTC if no calendar)
);
CREATE INDEX ix_obs_checkid_time ON observation(check_id, observed_at);
```

Every check writes one observation per run, including OK and ERROR. Numeric
metrics populate `value`; freshness stores its computed lag in seconds in `value`;
the schema check stores its fingerprint in `value_text`. Retention is configurable
(`store.retain_days`, default 400 — enough for `last_same_weekday` plus a year of
trend); `dbfresh prune` enforces it. Timestamps are stored in UTC; `weekday` is
stored in the calendar timezone (UTC when no calendar is configured) so "same
weekday last week" queries are trivial.

`git_sha` records config provenance: at run start the engine resolves `HEAD` of
the git repository containing the root config file (best-effort; null when the
config is not in a repository or git is unavailable). Every observation is
thereby tied to the reviewed config commit that produced it.

### 8.2 Stable `check_id`

Continuity of history must survive threshold edits. Derivation:

- if the check declares an explicit `id:`, use it verbatim;
- else compute a deterministic hash over the **identity tuple**: `source`,
  `object`, `metric`, and the discriminating field (`column` / `key`; none for
  `schema` and `row_count`), or for assertions the normalized `assert` /
  `assert_sql` text. Normalization strips leading/trailing whitespace and
  collapses internal whitespace runs to a single space, preserving case. The hash
  is SHA-256 over the tuple joined by a delimiter, rendered as the first 12 hex
  characters. **The expectation is NOT part of the identity**, so tuning a
  threshold preserves history. Two checks with the same identity anywhere in the
  composed config (root plus includes, §12.2) is a validation error (ambiguous
  history); require an explicit `id:` to disambiguate intentional duplicates.

### 8.3 `vs_previous` expectations

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
  `weekday` equals today's and whose `observed_at` date is at least 6 calendar
  days before the current run date — the correct baseline for a weekday-heavy
  business (compare Monday to last Monday). The 6-day floor skips same-week reruns
  while tolerating a run that slips by a day.
- Ratio guards require baseline ≠ 0; if baseline is 0, fall back to delta guards
  if present, else treat per `on_missing`.
- First run / no baseline: behavior per `on_missing` (default `pass`).
- `vs_previous` requires the observation store; with `--no-store` nothing
  accumulates, so it is permanently on the `on_missing` path.
- v1 restricts each check to one expectation (§6.3): a check uses `vs_previous` or
  a static bound, not both. Composition is a documented future extension.

---

## 9. CLI surface

```
dbfresh run       [-c config.yaml] [--only SOURCE] [--json] [--no-progress]
                [--store PATH] [--no-store]
dbfresh history   OBJECT [--source S] [--metric M] [-n 30] [-c config.yaml] [--store PATH]
dbfresh add       [-c config.yaml]         # interactive wizard, appends YAML
dbfresh ui        [-c config.yaml] [--store PATH]   # interactive Textual app
dbfresh prune     [--store PATH]           # enforce retention
```

`--store` overrides the `DBFRESH_STORE` environment variable, which overrides
`store.path` in config (§8.1).

- `run`: the core command. Progress bar (via `rich`) unless `--json` or
  `--no-progress`. Persists observations unless `--no-store`.
- `history`: reads the store and prints a check's recent values, statuses, and a
  simple trend (e.g. sparkline or delta column). Read-only. `OBJECT` may match
  checks across sources or metrics; `--source` / `--metric` disambiguate, and an
  ambiguous selection lists the candidates rather than guessing.
- `add`: the interactive authoring wizard — the CLI surface of the
  configurator (§11). Introspects the source, proposes a check bundle for a
  named object, and appends well-formed YAML to the config. **Emits YAML**
  (definitions stay in git); it does not write checks into SQLite.
- `ui`: launches the interactive Textual app (§10.2) — status dashboard,
  configurator, and history browser over the same engine and store. Interactive
  only; the scheduler uses `run`.

### Exit codes (worst status across all checks)

| code | status       | meaning                    |
| ---- | ------------ | -------------------------- |
| 0    | OK / SKIPPED | all clear                  |
| 1    | WARN         | soft-bound violations only |
| 2    | FAIL         | value violations           |
| 3    | ERROR        | unreachable / query error  |

The run's exit code is the worst single status, and ERROR (3) outranks FAIL (2): a
run that both fails a value check and cannot reach a source exits 3. The digest and
JSON still list every non-OK check, so the FAIL is never hidden — only the exit
code collapses to the most severe.

---

## 10. Reporting

### 10.1 Batch CLI output

- **Progress:** live bar with M-of-N completion while checks run.
- **Digest:** plain text (no rich markup, so it survives copy-paste), suitable
  for pasting into a ticket or chat. Header line with local timestamp + tz and
  passed / failed / warned / skipped / unreachable counts, then one block per
  non-OK check: the qualified object + label, expected vs observed (or the
  error), and up to 10 sample violation rows for assertions. Example:

```
DATA CHECK REPORT — 2026-07-10 06:03 America/New_York
23 checks · 20 passed · 2 failed · 1 warned · 0 skipped · 0 unreachable

✗ warehouse.dbo.fct_sales · assert amount >= 0
    3 row(s) violate the constraint
      sale_id=88213  amount=-42.00
      sale_id=88240  amount=-15.50

✗ lakehouse.main.gold.customer_360 · null_rate(email)
    expected max 0.01   observed 0.124
```

- **JSON:** `--json` emits `{status, run_id, started_at, finished_at, counts,
  results: [...]}` for machine consumption and suppresses the progress bar. Each
  result is `{check_id, source, object, metric, label, tier, status, value,
  value_text, expected, observed, error, samples}` — `value` / `value_text` carry
  the observed scalar or fingerprint, `expected` is a rendered form of the
  expectation, `error` is null unless the status is ERROR, and `samples` holds up
  to the capped assertion rows (empty for metric checks). This object shape is a
  stable contract for downstream consumers.

### 10.2 Interactive TUI (Textual)

`dbfresh ui` launches a Textual application over the same engine and store. It
adds no check semantics; it is a second front-end.

- **Home — status dashboard.** A green/red tree grouped by the check tiers:
  source → object, with the object's table-level checks (row-count, schema,
  assertions) at the object node and its column-level checks nested under each
  column. Each node's status is the worst of its children, drawn from the latest
  observation per `check_id`. A node with no stored observation renders as
  "unknown" until the next run.
- **Menu.** Navigation to **Configure** (the configurator of §11 as a screen —
  introspect, propose a check bundle, append YAML), **Report** (the latest
  run's digest), and **History** (drill into a check's trend).
- **History drill-down.** Selecting a node opens its recent values, statuses, and
  trend — the interactive form of `dbfresh history`.
- **Run.** The app can trigger a `run` and refresh the dashboard from the new
  observations.
- **Testing.** Exercised with Textual's `App.run_test()` / `Pilot` harness
  (simulated key presses, assertions on rendered state).

The Configure screen and `dbfresh add` share the one front-end-agnostic
`configurator` module (§11); only the prompt / rendering layer differs.

---

## 11. Configurator

One front-end-agnostic module (`configurator.py`) with two surfaces:
`dbfresh add` (§9) and the TUI Configure screen (§10.2); only the prompt /
rendering layer differs. It emits YAML into the version-controlled config; it
never writes checks to SQLite.

### 11.1 Metadata-driven proposal flow

The design goal is minimal required input: the user names a source and an
object; the wizard introspects (`describe()` → `ObjectInfo`, §5.1) and
**proposes** a complete check bundle, which the user accepts, edits, or trims
check by check. Metadata proposes; the user confirms; the accepted result is
explicit YAML reviewed like any other config change — never silently applied
behavior. The proposed bundle:

- **`schema` with `unchanged: true`** — always.
- **`row_count` volume stability** — always: `vs_previous` with
  `baseline: previous` and ratio guards seeded at `0.5` / `2.0`; when a
  `calendar:` is configured the wizard offers `last_same_weekday` instead.
- **`freshness` on an auto-detected timestamp column.** Candidate selection
  over `temporal`-category columns: prefer conventional names
  (`modified_at`, `updated_at`, `loaded_at`, `load_ts`, `created_at`, and
  suffix variants `_at` / `_ts` / `_date`); if exactly one temporal column
  exists, use it; if several match, ask the user to pick. The
  `freshness_source:` is auto-picked from metadata and dialect capability:
  `column` whenever a candidate exists; for a Databricks **table** with no
  good candidate, propose `describe_history` (or `describe_detail`); on any
  other engine, or on a view, with no candidate — no freshness proposal.
- **`duplicate_count` (expect `{max: 0}`)** on each single-column primary-key
  or unique constraint found in `ObjectInfo.keys` (composite keys are out of
  scope, §6.2).
- **Type-appropriate column checks** keyed off the canonical category
  (§11.2), offered rather than preselected: `null_rate` on nullable columns
  the user marks as completeness-critical (`NOT NULL` columns are skipped —
  the engine already enforces them), and `avg` with `vs_previous` ratio
  guards on numeric measure columns.

Catalog hints (`approx_row_count`, `last_modified`) are shown next to the
proposal to help the user judge it; they never affect run-time evaluation
(§5.1). Every absent capability or missing piece of metadata simply removes
its proposal — no keys metadata means no `duplicate_count` proposal, and the
user can still add one manually. Boundaries: no foreign-key graph traversal,
no cross-object inference, no threshold learning; proposals use only the named
object's own catalog metadata.

### 11.2 Category → offer mapping

Beyond the proposed bundle, the wizard offers per-column checks keyed off
`category` (§5.2), never off native type names:

| category   | offered column-level checks                                  |
| ---------- | ------------------------------------------------------------ |
| `numeric`  | `null_rate`; `sum` / `avg` / `min` / `max`; `duplicate_count` (as key) |
| `temporal` | `freshness` (`freshness_source: column`); `null_rate`        |
| `string`   | `null_rate`; `duplicate_count` (as key)                      |
| `boolean`  | `null_rate`                                                  |
| `other`    | `null_rate`                                                  |

Table-level offers (`row_count`, `schema`, assertions) do not depend on column
categories. This mapping is the single source for the docs applicability
matrix (§14).

### 11.3 Safety and degradation

- Adding a **new source** runs a mandatory connection test before anything is
  written.
- Each named object is existence-checked via `describe()`.
- A failed connection or a missing object is surfaced and requires explicit
  confirmation before the block is written; for an already-configured source
  that is unreachable, the wizard degrades to manual entry and marks existence
  unverified. Manual entry is also the path when metadata is unavailable.
- When the config uses `include:` (§12.2), the wizard asks which file receives
  the new block (the root config or an included checks file); otherwise it
  appends to the root config.

---

## 12. Configuration reference

### 12.1 Consolidated example (root config)

```yaml
version: 1

include: # optional; see §12.2
  - checks/*.yaml

store: # optional; observation history. A bare string is shorthand for { path }.
  path: ./dbfresh.db
  retain_days: 400
calendar: # optional; enables §7 features
  timezone: America/New_York
  workdays: [mon, tue, wed, thu, fri]
  holidays: { country: US, subdivision: null, extra: [], remove: [] }

sources:
  warehouse:
    type: sqlserver
    url: ${MSSQL_URL} # sqlserver://reader:pw@host:1433/WarehouseDB
    timeout: 30
    timezone: America/New_York # interpret naive timestamps as this tz (default UTC)
  lakehouse:
    type: databricks
    host: ${DATABRICKS_HOST}
    http_path: ${DATABRICKS_HTTP_PATH}
    token: ${DATABRICKS_TOKEN}

defaults: # merged into every check when absent; supports
  severity: error #   severity, calendar, where, allow_empty, skip_off_schedule

checks:
  - source: warehouse
    object: dbo.fct_sales
    id: sales_amount_nonneg # optional stable id
    assert: "amount >= 0"

  - source: warehouse
    object: dbo.fct_sales
    metric: schema # table-level shape check
    expect: { unchanged: true } # fail if columns/types drift from last run

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
    freshness_source: column # or describe_history / describe_detail (Databricks tables)
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

### 12.2 Composition — splitting checks across files

A config is a single file, or a root file plus included check files:

- `include:` is a top-level list of path globs in the **root config only**,
  resolved relative to the directory of the file that declares them. Matched
  files load in lexicographic path order (deterministic, but order carries no
  semantics — checks are independent).
- Only the root config declares `include:`, `sources:`, `calendar:`, `store:`,
  and `defaults:`. An included file contributes only checks: either a mapping
  with a single `checks:` key or a bare YAML sequence of check blocks. Any
  other top-level key in an included file is a validation error.
- The composed check list is validated as one unit; a duplicate `check_id`
  (explicit or derived, §8.2) anywhere across the files is a validation error.
- A glob that matches no files is a validation error — a mistyped include must
  not silently drop checks.
- There is no implicit directory scan; the conventional layout is a `checks/`
  directory named by an explicit `include: ["checks/*.yaml"]`, typically one
  file per source or per domain.

### 12.3 Path resolution

Relative paths in config resolve relative to the config file that declares
them, never the process CWD: `include:` globs against the root config's
directory, `store.path` against the root config's directory. Paths given on
the command line (`--store`, `-c`) resolve against CWD as usual.

### 12.4 Team workflow — sharing configuration

The config repository is the team's shared, reviewed definition of what
healthy data looks like:

- **Portable definitions.** The YAML holds only definitions. Secrets enter
  through `${VAR}` interpolation, resolved from the environment or a gitignored
  per-user `.env` (§4); the store path is machine-local (flag, env var, or a
  relative path that resolves per-clone, §8.1). The same committed config runs
  against staging and prod by supplying different `${...}` values per
  environment — same definitions, different endpoints.
- **Per-check review.** Each check is a self-contained YAML block, so a pull
  request that adds or tunes one check is a few reviewable lines. Splitting
  checks across included files (per source or per domain) keeps ownership and
  review routing clear.
- **History survives refactors.** `check_id` (§8.2) derives from what is
  measured, not from where the block lives; moving a check between files or
  renaming a file preserves its observation history.
- **Provenance.** Each run records the config repo's `git_sha` (§8.1), tying
  every observation to the config commit that produced it.

---

## 13. Error handling and edge cases (required behavior)

- **Unreachable source / auth failure:** every check on that source → `ERROR`
  (exit 3), with the exception text in the digest. Never OK.
- **Query error** (bad column, permission, syntax): that check → `ERROR`.
- **Empty table / null scalar:** fails the expectation unless `allow_empty:true`.
- **`null_rate` / ratio with zero denominator:** `NULLIF` yields null → treat as
  `ERROR` for null_rate on an empty table; for `vs_previous`, zero baseline is
  handled per §8.3.
- **Timezone:** all weekday/holiday/business-day decisions use the calendar
  timezone; stored timestamps are UTC; freshness compares tz-aware values (naive
  DB timestamps are interpreted in the source's `timezone:`, default UTC).
- **No calendar configured:** `by_weekday` / `on_holiday` / `calendar: business` /
  `skip_off_schedule` used without a `calendar:` block is a config validation
  error.
- **Config validation runs before any connection** and reports all problems at
  once: unknown source refs, duplicate identities (including across included
  files), unknown metrics/operators, missing required fields, `{min, max}` on
  one check, `vs_previous` on `freshness` / `schema`, a DESCRIBE-based
  `freshness_source:` on a view or on an engine whose dialect lacks the
  capability (§5.3), an `include:` glob matching no files, and non-check keys
  in an included file.
- **Configurator safety:** per §11.3 — mandatory connection test for a new
  source, existence checks, explicit confirmation before writing unverified
  blocks, manual-entry degradation.
- **Schema check on a missing object / permission error:** `describe()` fails →
  that check is `ERROR`, never OK.
- **Trust boundary:** object names, `where:` clauses, and `assert:` predicates from
  config are interpolated verbatim into SQL (required for fully-qualified
  identifiers); config is trusted, version-controlled input, not user input, so
  this is not an injection surface. Do not "fix" it with parameterization that
  breaks qualified identifiers.

---

## 14. Documentation

User documentation is a v1 deliverable: an mkdocs-material site whose sources
live under `docs/` (with `mkdocs.yml` at the repo root), published as a
static site. This section specifies the site; the content is written during
implementation (§16). Required pages:

- **Quickstart.** Install (`uv tool install dbfresh` / `pipx install dbfresh`),
  a minimal config with one source and two checks, exporting the `${VAR}`
  secrets, a first `dbfresh run`, reading the digest and exit codes, and
  reaching a first green check.
- **Concepts.** Values-not-jobs; the two primitives (metric + expectation,
  zero-row assertion); table-level vs column-level tiers; definitions in git,
  observations in SQLite.
- **Check reference.** One page per check type across both tiers
  (`row_count`, `schema`, `null_rate`, `duplicate_count`,
  `sum`/`avg`/`min`/`max`, `freshness`, assertions), plus:
  - a **check × data-type applicability matrix** — which checks fit numeric /
    temporal / string / boolean columns — generated from the category → offer
    mapping the configurator uses (§11.2);
  - **per-engine notes** — freshness DESCRIBE forms are Databricks-table-only;
    each engine's row-limiting form; native type-name variances behind the
    canonical categories; Oracle specifics (`ALL_TAB_COLUMNS`,
    `FETCH FIRST … ROWS ONLY`).
- **Expectations & durations.** Every operator, the one-expectation-per-check
  rule, duration syntax, `allow_empty`.
- **Calendar & scheduling.** The business calendar, `by_weekday` /
  `on_holiday`, business-time freshness, `skip_off_schedule`, and running
  under cron / systemd timers via exit codes.
- **History & trends.** The observation store, `vs_previous`, baselines
  (`previous`, `last_same_weekday`), `dbfresh history`, retention and `prune`.
- **CLI reference.** All commands, flags, and exit codes.
- **TUI guide.** The dashboard, Configure, Report, and History screens.
- **Authoring checks.** The configurator: the proposal bundle from a table
  name, the timestamp-column and key-detection heuristics, and the
  manual-entry fallback (§11).
- **Configuration reference.** The full schema of §12, including composition
  and path resolution.
- **Team workflow.** Sharing configuration per §12.4.
- **Extending — adding a source type.** Developer page: the Adapter and
  Dialect contracts and the PostgreSQL worked sketch of §5.4.

**Single source of truth.** The check reference, expectation reference,
applicability matrix, and CLI reference must not drift from the code. The
metric registry, expectation-operator registry, category → offer mapping, and
the CLI parser are authoritative; a generation step runs as part of the docs
build (`just docs`), emitting these reference tables from the registries at
build time so they cannot go stale — the generated pages are build artifacts,
not committed (the generated output under `docs/` is gitignored). Prose pages
(Quickstart, Concepts, guides) are hand-written. The config schema documented in
§12 is authoritative for configuration semantics.

---

## 15. Testing requirements

The **sqlite** adapter is the primary test engine: a real adapter over an
in-memory (or temp-file) database gives real SQL execution and real reflection,
so the engine, schema-check, configurator, and composition suites all run
without a live warehouse. SQL Server and Databricks get contract-level unit tests
(SQL compilation, dialect capabilities, `describe` normalization against recorded
catalog fixtures) plus optional integration tests behind an env-gated marker,
runnable when a sandbox is available.

The team must deliver tests covering at least:

- **URL parsing:** dburl-style vs native-style, default port, URL-encoded
  password, scheme aliases, missing-database and bad-scheme errors.
- **Durations & expectations:** all operators, compound durations, boundary
  (inclusive) cases.
- **SQL compilation:** each builtin on the ANSI baseline; `where` clause;
  assertion predicate vs raw `assert_sql`; row limiting and float coercion
  obtained from a stub Dialect defined only in tests — proving the compiler
  consults the dialect and never an engine name.
- **Dialects & capabilities:** each shipped dialect's row-limiting form;
  freshness capability validation (a DESCRIBE-based `freshness_source:` on
  SQL Server or on a view fails validation); introspection capability
  declarations.
- **Type categories:** native → category mapping per adapter; unknown native
  types map to `other`.
- **Calendar:** business-day/holiday classification, `business_time_between`
  across weekends and holidays, `previous_business_day`, `last_same_weekday`
  selection.
- **Store:** `check_id` stability across expectation edits; observation
  round-trip incl. `value_text` and freshness lag (seconds); `vs_previous`
  baseline selection incl. missing-baseline paths; `git_sha` capture.
- **Config composition:** include-glob resolution relative to the root
  config; lexicographic load order; duplicate `check_id` across files
  rejected; non-check keys in an included file rejected; empty-glob error;
  relative `store.path` resolved against the root config regardless of CWD.
- **Engine end-to-end:** full compile→execute→evaluate against the real
  **sqlite** adapter over an in-memory database (SQLAlchemy engine with
  `check_same_thread=False` because sources run in worker threads), covering
  OK/WARN/FAIL/ERROR/SKIPPED and exit-code aggregation — real SQL execution and
  reflection, not a mock.
- **Schema check & introspection:** fingerprint stability across reordered
  columns; drift on add / remove / retype; `unchanged` vs `equals`; `describe()`
  reflection normalization (columns, keys, stats) across the shipped adapters,
  including `None` degradation on objects without the metadata.
- **Configurator:** proposal bundle from a table name — against the real sqlite
  adapter for genuine columns/keys, and a fake adapter for capability-absence
  and Databricks-only paths (schema + row_count/vs_previous always; freshness on
  the detected timestamp column; duplicate_count from key metadata); timestamp
  heuristics (conventional name preferred, sole temporal column, ambiguous →
  ask); Databricks-table fallback to `describe_history` vs no proposal
  elsewhere; degradation when keys/stats are absent; category → offer mapping
  per category; new-source connection test; object-existence check;
  manual-fallback path; the emitted YAML re-parses and runs.
- **Docs lockstep:** every registered metric and operator appears in the
  generated reference pages; the staleness check fails when a registry
  changes without regeneration.
- **TUI:** dashboard status aggregation (worst-of-children), navigation, and
  history drill-down via Textual's `App.run_test()` / `Pilot`.

Property-based tests are encouraged for the calendar arithmetic and duration
parsing.

---

## 16. Implementation plan

The plan is organized as epics that an implementation team can sequence
directly. Each epic states its scope and testable acceptance criteria;
dependencies, the critical path, and parallelization follow.

### E1 — Engine core: adapters, dialects, compile/evaluate loop

Scope: the SQLAlchemy-backed adapter base (engine construction from source
config, `scalar`/`rows` over `text()`, reflection-based `describe()`), the
Adapter protocol, `ObjectInfo`/`Column` models with type categories, the Dialect
model (variances + capability sets), and the adapter factory; the **sqlite**,
SQL Server, and Databricks adapters and dialects, including `describe()`
population of columns, keys, and stats where each engine exposes them;
single-file YAML config loading with `.env` (dotenv) plus `${VAR}` interpolation
and full up-front validation; the two primitives; builtin metrics `row_count`,
`null_rate`, `duplicate_count`, `sum`/`avg`/`min`/`max`, `freshness`
(`freshness_source:` with capability validation); static-bound and `max_lag`
expectations; severity; per-source threaded execution; rich progress, digest,
JSON output; exit codes. The repo's existing package skeleton, CLI stub, and
tooling are the starting point, not part of this deliverable.

Accept:

- `pytest` green, including engine end-to-end against the real sqlite adapter
  over an in-memory database.
- A check compiles correctly against a stub Dialect defined only in tests;
  `checks.py` and `engine.py` contain no engine-name conditionals.
- A DESCRIBE-based freshness check on a SQL Server source fails config
  validation via the capability set, with no engine-name test in validation.
- Digest renders as in §10.1; exit-code aggregation is ERROR > FAIL > WARN >
  OK.

### E2 — Schema (shape) check

Scope: fingerprint computation over `describe()` columns (order-insensitive
set of `(column_name, data_type)` pairs, SHA-256); the `equals` (pinned)
expectation; drift rendering (added / removed / retyped) in the digest. The
`unchanged` operator is wired once E3's store lands.

Accept: fingerprint is stable across column reordering; add / remove / retype
each change the fingerprint and render in the digest; `unchanged` compares to
the previous stored observation (requires E3).

### E3 — Observation store and history

Scope: `store.py` with the `run` / `observation` schema (incl. `value_text`);
`git_sha` capture from the root config's repository; stable `check_id`
(§8.2); persist every observation; `--store` / `DBFRESH_STORE` /
`store.path` precedence and `--no-store`; `dbfresh history`; `dbfresh prune`
and `retain_days` retention.

Accept: a run writes one observation per check; editing a threshold does not
change `check_id`; `git_sha` is recorded when the config lives in a git repo
and null otherwise; the store-path precedence order is honored; `history`
prints recent values; `prune` enforces retention.

### E4 — Calendar

Scope: `calendar.py` (`holidays` package integration), `by_weekday` /
`on_holiday` expectation selection, `calendar: business` freshness,
`skip_off_schedule` → `SKIPPED`.

Accept: a Monday run selects the Monday expectation; Friday-evening data
reads as fresh on Monday under `business`; a configured holiday is treated as
non-business; `skip_off_schedule` without a `calendar:` block fails
validation.

### E5 — History-based expectations (`vs_previous`)

Scope: `vs_previous` with `baseline: previous | last_same_weekday`, ratio and
delta guards, `on_missing`.

Accept: a 3× row-count swing vs the correct baseline fails;
`last_same_weekday` honors the 6-day floor and the calendar-timezone weekday;
the first run passes under default `on_missing`; zero-baseline falls back to
delta guards when present.

### E6 — Config composition

Scope: `include:` globs, included-file constraints (checks only), path
resolution per §12.3, cross-file duplicate `check_id` detection, deterministic
load order.

Accept: a root + `checks/*.yaml` layout runs identically to the equivalent
single file; moving a check between files preserves its `check_id` and
history; each validation error case in §12.2 is reported.

### E7 — Configurator and `dbfresh add`

Scope: the front-end-agnostic `configurator` module — introspection over
`ObjectInfo`, the metadata-driven proposal bundle (§11.1), timestamp-column
and key-detection heuristics, category → offer mapping (§11.2), YAML
emission, mandatory connection test for a new source, object-existence
checks, manual fallback, include-aware target-file selection — plus the
`dbfresh add` wizard on top of it.

Accept: naming a table against a fake adapter with full metadata yields the
§11.1 bundle (schema, row_count vs_previous, freshness on the detected
column, duplicate_count from keys); each proposal disappears cleanly when its
metadata is absent; offers key only off categories (a fake adapter with novel
native type names still gets correct offers); the emitted YAML re-parses and
runs; adding a new source tests the connection; a missing object is flagged.

### E8 — Interactive TUI

Scope: the Textual app (`dbfresh ui`): status dashboard (source → object →
column, green/red from the latest observations), menu, Configure screen
reusing the E7 module, history drill-down, in-app run trigger.

Accept: the dashboard reflects the last run's statuses; Configure appends a
valid block; History shows a check's trend; covered by `App.run_test()`.

### E9 — Documentation

Scope: the mkdocs-material site of §14, including the build-time
reference-generation step (`just docs`) that emits the registry-derived pages as
uncommitted build artifacts.

Accept: the site builds without warnings; `just docs` regenerates the
registry-derived reference pages from the code at build time (a newly registered
metric or operator appears without hand-editing); every page listed in §14
exists and covers its specified content.

### E10 — Extensibility validation: reference PostgreSQL adapter

Scope: `adapters/postgres.py` per the §5.4 sketch (adapter + dialect + one
factory line) with contract-level unit tests (no live PostgreSQL required; a
live integration test may sit behind an env-gated marker); the "Extending"
docs page checked against the actual diff.

Accept: the diff touches only the new module, the factory registration,
tests, and docs — `checks.py`, `engine.py`, `calendar.py`, `store.py`,
`report.py`, the CLI, and the TUI are unchanged; dialect tests cover `LIMIT`
row limiting and category mapping from reflected PostgreSQL type names.
PostgreSQL remains an unsupported reference target in v1 (§1); this epic
exists to prove the contract's cost.

### E11 — Repository scaffolding, CI, and packaging

Scope: repository hygiene so every later epic lands under green CI. `README.md`
and an MIT `LICENSE.md` (present); a GitHub Actions workflow running the local CI
recipe (`just check` — ruff lint, format check, pytest) on push and pull request
against Python 3.14; a `CONTRIBUTING.md` (uv setup, `just check`, branch-and-PR
flow); a pull-request template and issue templates; a pre-commit config running
ruff; packaging metadata and a release workflow that builds and publishes the
wheel/sdist (the Quickstart's `uv tool install` / `pipx install` assume a PyPI
release); optional `CODEOWNERS` and a Dependabot config. Branch protection on
`main` is a maintainer setting, noted for the team.

Accept: CI runs `just check` green on a pull request and is required for merge;
`uv build` produces a valid wheel and sdist; `CONTRIBUTING.md` reproduces the
local check flow; the pre-commit hook rejects an unformatted commit.

### Dependencies, critical path, parallelization

| epic | depends on                                            |
| ---- | ----------------------------------------------------- |
| E1   | —                                                     |
| E2   | E1 (fingerprint + `equals`); E3 (`unchanged`)         |
| E3   | E1                                                    |
| E4   | E1                                                    |
| E5   | E3, E4                                                |
| E6   | E1                                                    |
| E7   | E1 (E6 recommended first, for target-file selection)  |
| E8   | E3, E7                                                |
| E9   | E1 for the first pages; final acceptance after E8     |
| E10  | E1                                                    |
| E11  | — (land first; gates every merge under green CI)      |

**Critical path:** E1 → E3 → E8, with E7 (also required by E8) runnable in
parallel with E3 once E1 lands. The batch monitor — the primary value — is
complete at E1 + E2 + E3 + E4 + E5.

**Parallelization:**

- E11 (repo scaffolding + CI) has no code dependencies and lands first, so E1
  onward merge under green CI.
- Once E1 lands, E2, E3, E4, E6, and E10 proceed in parallel; they share only
  the contracts E1 froze.
- E9 runs alongside the whole plan: Quickstart and Concepts after E1,
  reference generation extended with each epic that grows a registry, the TUI
  guide after E8.
- E5 starts as soon as E3 and E4 both land.
- E7 needs only E1; scheduling it after E6 lets `add` target included files
  from the start.
- Running E10 in parallel with E2–E5 surfaces adapter/dialect contract gaps
  while they are still cheap to fix; the same applies to any further adapter
  built later.
