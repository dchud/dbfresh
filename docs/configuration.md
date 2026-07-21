# Configuration reference

## Consolidated example

```yaml
version: 1

include: # optional; see Composition below
  - checks/*.yaml

store: # optional; observation history. A bare string is shorthand for { path }.
  path: ./dbfresh.db
  retain_days: 400
calendar: # optional; enables calendar features -- see Calendar & scheduling
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

## Top-level keys

| key | where | purpose |
| --- | --- | --- |
| `version` | root only | schema version (`1`) |
| `include` | root only | path globs for extra checks files (see Composition) |
| `store` | root only | observation-store path and retention |
| `calendar` | root only | business calendar (see Calendar & scheduling) |
| `sources` | root only | named source connections |
| `defaults` | root only | fields merged into checks that omit them |
| `checks` | root + included | the check list |

A per-check value always overrides the corresponding `defaults:` entry,
including an explicit falsy value (`allow_empty: false` on a check wins over
a `defaults: {allow_empty: true}`).

## Source types

`sources.<name>.type` selects the adapter; every other key under a source
is passed through as that adapter's constructor parameters (`url`, `host`,
`token`, `database`, ...), so `${VAR}` interpolation works uniformly across
all of them. v1 targets two production source types, **SQL Server** and
**Databricks** (Unity Catalog) -- both ship with working connection
adapters. **sqlite** is a fully working adapter today: it's dbfresh's own
primary test engine, and legitimate to point at a real file-based database
too (see the [Quickstart](quickstart.md)). **PostgreSQL** ships only as a
reference adapter proving the [extending](extending.md) contract --
it is explicitly not a supported v1 target, even though it is registered
and functional.

## Path resolution

Relative paths in config resolve relative to the config file that declares
them, **never** the process's current directory:

- `include:` globs resolve against the root config's directory.
- `store.path` resolves against the root config's directory.

Paths given on the command line (`--store`, `-c`) resolve against the
current directory as usual, like any other CLI argument.

### Locating the config file

Every command that reads a config looks for it in this order:

- `-c PATH`, resolved against the current directory.
- `DBFRESH_CONFIG`, if set.
- The nearest `config.yaml` walking up from the current directory,
  stopping at the enclosing git repository root -- or, outside a
  repository, the home directory or the filesystem root. `config.yaml` is
  a generic name, so the walk-up never crosses that boundary and silently
  picks up an unrelated file further up.
- `config.yaml` in the current directory otherwise.

A config found by walking up, or given via `DBFRESH_CONFIG`, is named by
its full path in any error, so a load failure always points at the exact
file in question. This means running a command from a subdirectory of a
config repository uses that repository's `config.yaml` instead of the
empty-config fallback.

## Composition -- splitting checks across files

A config is either a single file, or a root file plus included checks
files:

- `include:` is a top-level list of path globs, declared **only** in the
  root config, resolved relative to that root config's directory. Matched
  files load in lexicographic path order -- deterministic, but load order
  carries no semantics, since checks are independent of each other.
- Only the root config may declare `include:`, `sources:`, `calendar:`,
  `store:`, and `defaults:`. An included file contributes only checks:
  either a mapping with a single `checks:` key, or a bare YAML sequence of
  check blocks. Any other top-level key in an included file is a validation
  error.
- The composed check list (root plus every included file) is validated as
  one unit: a duplicate `check_id` anywhere across the files -- explicit or
  derived -- is a validation error, since it would make observation history
  ambiguous.
- A glob that matches no files is a validation error, so a mistyped
  `include:` entry can never silently drop checks.
- There is no implicit directory scan. The conventional layout is a
  `checks/` directory named by an explicit `include: ["checks/*.yaml"]`,
  typically one file per source or per domain.

## `${VAR}` secret interpolation

Any string value anywhere in the config (`sources:` params, `where:`
clauses, anything) may contain `${VAR}` tokens, resolved against the process
environment at load time. A referenced variable that isn't set is a hard
config-load error -- there is no silent empty-string fallback. Every command
that parses a config loads a gitignored, per-user `.env` file (from the
config's directory) before parsing it, so `${VAR}` values can live outside
both the committed config and the shell's persistent environment (see
[Quickstart](quickstart.md)). See [Environment & secrets](environment.md)
for the field-by-field `${VAR}` table per source type.

## `check_id` and identity

Every check has a stable identity, used as its observation-history
key: an explicit `id:` if given, else a hash of `source`, `object`,
`metric`, and whichever field discriminates that metric (`column`, `key`,
or nothing for `schema`/`row_count`), or the normalized assertion text for
an `assert`/`assert_sql` check. The expectation is deliberately **not**
part of that identity, so tuning a threshold never breaks history --
editing `expect: {max: 500000}` to `expect: {max: 600000}` on the same
check keeps its trend intact. Two checks that resolve to the same identity
anywhere in the composed config is a validation error; give one of them an
explicit `id:` to disambiguate an intentional duplicate.
