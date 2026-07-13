# Extending -- adding a source type

Everything above the adapter layer -- the engine, the check compiler,
expectations, the calendar, the observation store, reporting, the CLI, and
the TUI -- is engine-agnostic. Adding a new database engine is one adapter
module plus one factory registration line; nothing else in the codebase
changes. `adapters/postgres.py` is a real, working instance of this: a
reference PostgreSQL adapter built to prove the contract, not a supported
v1 source (PostgreSQL is explicitly out of scope for v1 -- see
[Configuration reference](configuration.md)).

## The Adapter contract

Every adapter exposes a `dialect` (a `Dialect` instance, below) and four
methods:

- `scalar(sql) -> Any` -- run a query expected to return one value.
- `rows(sql) -> list[dict]` -- run a query and fetch its rows (the compiler
  has already applied the dialect's row cap before this is called).
- `describe(object) -> ObjectInfo` -- normalized object metadata: `columns`
  (name, native type, nullability, canonical category -- always populated),
  `keys` (primary-key / unique constraint column lists, or `None` when the
  engine/object doesn't expose them), `approx_row_count` and
  `last_modified` (cheap catalog estimates, or `None`).
- `close()`.

**`SqlAlchemyAdapter`**, the shared base every shipped adapter subclasses,
implements all four methods over a plain SQLAlchemy `Engine`: `scalar` /
`rows` execute the compiler's `text()` SQL verbatim, and `describe` is
built entirely on SQLAlchemy's reflection `Inspector`
(`get_columns`, `get_pk_constraint`, `get_unique_constraints`). That means
columns, native types, nullability, and keys come for free on any
SQLAlchemy-supported engine -- a new adapter only needs to declare its
`Dialect` and whatever engine-specific extras reflection can't give it: a
catalog row-count estimate, a category-mapping refinement, or -- when an
engine's metadata reflection is too thin to trust, as Databricks's is for
Unity Catalog -- an overridden `describe` entirely.

## The Dialect contract

Metric SQL compiles on an ANSI baseline that runs unchanged on every
target engine (`COUNT(*)`, `SUM(CASE WHEN ... THEN 1 ELSE 0 END)`,
`AVG`/`MIN`/`MAX`, `COUNT(DISTINCT col)`). The compiler asks the check's
`Dialect` for each variance; it never branches on an engine name. A
`Dialect` declares:

- **Row limiting** -- `limit(sql, n) -> sql`: `LIMIT n` (the default;
  Databricks, PostgreSQL, MySQL), `TOP n` (SQL Server / T-SQL), or
  `FETCH FIRST n ROWS ONLY` / a `ROWNUM` wrapper (Oracle).
- **Float coercion** -- `float_ratio(numerator, denominator) -> sql`: the
  portable default multiplies by `1.0`
  (`SUM(...) * 1.0 / NULLIF(COUNT(*), 0)`); a dialect may override with an
  explicit cast such as `CAST(... AS FLOAT)`.
- **Identifier policy** -- `verbatim` for every v1 engine: object names are
  author-qualified and interpolated exactly as written, with no automatic
  quoting. The policy lives on the dialect so a future engine that needs
  quoting has one declared place for it.
- **Freshness capabilities** (`freshness_sources`) -- which
  `freshness_source:` values the engine supports; the baseline is
  `{column}`, and the Databricks dialect adds `describe_history` and
  `describe_detail`. Config validation consults this set directly, so a
  metadata-based freshness check on an engine that lacks it fails
  validation with no engine-name test anywhere.
- **Introspection capabilities** (`introspection_capabilities`) -- which
  optional `ObjectInfo` fields the engine can populate (`keys`, `stats`).
  The [configurator](authoring-checks.md) uses this to distinguish "this
  engine cannot say" from "this object genuinely has none."

## Worked example: the reference PostgreSQL adapter

`src/dbfresh/adapters/postgres.py` is a worked example, built end to end:

1. **The adapter.** `PostgresAdapter` subclasses `SqlAlchemyAdapter` with a
   `postgresql+psycopg://user:pass@host:port/db` engine URL built via
   SQLAlchemy's `URL.create`. `scalar`, `rows`, and the columns/keys half of
   `describe` are inherited from the base's reflection untouched.
   `describe` is overridden only to layer on two PostgreSQL-specific
   extras: a category-mapping refinement (`refine_category`, for native
   type names the base's generic `isinstance` checks don't already resolve
   -- `MONEY` is the one case in practice, since it doesn't subclass
   SQLAlchemy's generic `Numeric`) and a `pg_class.reltuples` row-count
   estimate (`_reltuples_estimate`), degrading to `None` for an unanalyzed
   or nonexistent relation rather than returning a misleading number.
2. **The dialect.** `PostgresDialect` overrides nothing but `name` and its
   two capability sets: row limiting (`LIMIT n`) and float coercion both
   come from the `Dialect` base unchanged; `freshness_sources = {column}`
   and `introspection_capabilities = {keys, stats}` are declared instead of
   inherited, since PostgreSQL genuinely supports both.
3. **One factory registration.** `adapters/factory.py`'s `_ADAPTERS` dict
   maps `"postgres"` to `PostgresAdapter` -- the only place a new source
   type has to be wired in for `type: postgres` in config to resolve.

`psycopg` (the PostgreSQL driver) is an optional dependency (the
`postgres` extra in `pyproject.toml`), not a core runtime one; nothing at
module level imports it, so `adapters/postgres.py` imports cleanly even
without the driver installed -- only actually constructing a
`PostgresAdapter` needs it.

No change was required anywhere in `checks.py`, `engine.py`,
`calendar.py`, `store.py`, `report.py`, the CLI, or the TUI to add this
adapter -- proving the contract's central claim: a new source engine costs
one module plus one registration line, not a change to the engine.

## Shape of other future engines

- **MySQL/MariaDB** is the same shape as PostgreSQL: `LIMIT` row limiting,
  full SQLAlchemy reflection, no `describe` override needed beyond a
  category-mapping refinement if any native type name needs one.
- **Oracle** differs only in row limiting (`FETCH FIRST … ROWS ONLY` on
  12c+, a `ROWNUM` wrapper on older versions) and its category mapping
  (via its own SQLAlchemy dialect) -- both are `Dialect`-level variances,
  not compiler changes. Column reflection uses `ALL_TAB_COLUMNS` rather
  than the fully generic path most engines get for free.
- Cloud-hosted variants of any of the above (RDS, Azure SQL, Cloud SQL) are
  connection-string differences, not new adapters at all.
