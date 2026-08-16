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

# check_sets: named, parameterized check batteries -- see Named check
# batteries under check_sets below.
check_sets:
  standard:
    with:
      rows: { vs_previous: { baseline: previous, min_ratio: 0.8, max_ratio: 1.2 } }
      max_lag: 26h
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: row_count
        expect: "{{ rows }}"
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: "{{ max_lag }}" }
        calendar: business

# tables: groups checks that share a source/object, stating both once
# instead of repeating them on every check nested under the entry.
tables:
  - source: warehouse
    object: dbo.fct_sales
    checks:
      - id: sales_amount_nonneg # optional stable id
        assert: "amount >= 0"

      - metric: schema # table-level shape check
        expect: { unchanged: true } # fail if columns/types drift from last run

      - metric: row_count
        expect: { between: [10000, 500000] }
        by_weekday:
          mon: { between: [0, 500000] }
          sat: { max: 100 }
          sun: { max: 100 }
        on_holiday: { max: 100 }

      - metric: freshness
        column: modified_at
        freshness_source: column # or describe_history / describe_detail (Databricks tables)
        expect: { max_lag: 26h }
        calendar: business

  # use: pulls in the standard battery; with: overrides ts_column, which
  # has no set-level default. rows and max_lag fall through to the set's
  # own defaults.
  - source: warehouse
    object: dbo.dim_customer
    use: standard
    with: { ts_column: modified_at }

  # skip: drops the freshness item, so ts_column is never needed here.
  - source: warehouse
    object: dbo.ref_currency
    use: standard
    skip: [freshness]
    with: { rows: { between: [1, 500] } }

checks:
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
| `tables` | root + included | checks grouped by shared source/object (see Grouping checks under tables) |
| `check_sets` | root + included | named, parameterized check batteries a table pulls in via `use:` (see Named check batteries under check_sets) |

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
  a generic name, so the walk-up stops at that boundary rather than
  silently picking up an unrelated file further up.
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
  either a mapping with `checks:`/`tables:`/`check_sets:`, or a bare YAML
  sequence of check blocks. Any other top-level key in an included file
  is a validation error.
- `check_sets:` composes the same way `checks:`/`tables:` does: a set
  defined in one file can be `use:`d by a table in any other, root or
  included. A set name defined in more than one file is a validation
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

## Grouping checks under `tables:`

Checks that share a `source:` and `object:` can be grouped under a
`tables:` entry instead of repeating both on every check:

```yaml
tables:
  - source: warehouse
    object: dbo.fct_sales
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: row_count
        expect: { between: [10000, 500000] }
      - assert: "amount >= 0"
```

Each block under `checks:` there is exactly the check block it would be
under a flat `checks:` list, minus `source:` and `object:` -- a nested
check declaring either of those itself is a validation error, naming the
table entry, since the entry already states them once for every check
under it. A table entry with no `checks:` at all is valid and
contributes nothing.

`tables:` and a flat `checks:` list may coexist in the same file, in the
same config, or split across root and included files -- `tables:` is
allowed anywhere `checks:` is, root or included. A grouped check is
otherwise indistinguishable from a flat one: `defaults:` merging,
`check_id` derivation, and every validation rule apply exactly the same
way. Restructuring an existing flat config under `tables:` never changes
a check's `check_id` (see `check_id` and identity, above), so it never
orphans a stored observation.

## Named check batteries under `check_sets:`

Tables that share a shape -- the same handful of checks, differing only
in a column name or a threshold -- can pull in a named battery instead of
repeating the check bodies too:

```yaml
check_sets:
  standard:
    with: # parameter defaults; a table's own with: wins
      rows: { vs_previous: { baseline: previous, min_ratio: 0.8, max_ratio: 1.2 } }
      max_lag: 26h
    checks:
      - metric: schema
        expect: { unchanged: true }
      - metric: row_count
        expect: "{{ rows }}"
      - metric: freshness
        column: "{{ ts_column }}"
        expect: { max_lag: "{{ max_lag }}" }
        calendar: business

tables:
  - source: warehouse
    object: dbo.fct_sales
    use: standard
    with: { ts_column: modified_at }
    checks: # custom checks, alongside the expanded set
      - assert: "amount >= 0"

  - source: warehouse
    object: dbo.ref_currency
    use: standard
    skip: [freshness]
    with: { rows: { between: [1, 500] } }
```

A `check_sets:` entry is always a mapping: `checks:` (required) is a list
of check blocks exactly like the ones under a `tables:` entry, with
`{{ name }}` placeholders standing in for values the table supplies; and
`with:` (optional) gives those placeholders their defaults. An unknown key
on a set is a validation error.

A table pulls a set in with `use: <name>` (one set name; `use:` does not
take a list). Its own `with:` overrides the set's defaults **key by key,
shallow** -- a key a table supplies replaces that key's default value
outright, never merged into it, since deep-merging two expectations (a
`vs_previous` default and a `between` override, say) would produce
nonsense. A placeholder with no value from either the set's `with:` or the
table's is a validation error naming the table, the set, and the
parameter. A `with:` key -- on the set or the table -- matching no
placeholder anywhere in the set is a validation error too, naming the
table, the set, and the key: it is almost always a misspelling. That check
runs against the set's *full* placeholder set, ignoring `skip:`, so a
set-level default used only by a skipped item never becomes an error for
every table that skips it.

Substitution has one mechanism, applied uniformly to every value in a
set's `checks:`: a placeholder occupying an **entire scalar node** is
replaced by the parameter's full value with its type preserved -- a
mapping, a list, a number, or a string -- which is what lets
`expect: "{{ rows }}"` carry a whole expectation mapping. A placeholder
**embedded in a longer string** interpolates as text instead, and requires
a scalar parameter; a mapping or list parameter used that way is a
validation error.

`skip:` names metrics and drops every set item carrying that metric from
the expansion. Skipping a metric the set does not define is a validation
error, not a silent no-op. A set item with no `metric:` (an `assert:`
item) cannot be skipped by name, since `skip:` only ever matches on
`metric:`. A table's own `with:` may still supply a parameter used only
by an item it skips, without error.

Checks expanded from a set come before the table's own inline `checks:`.
An expanded check is, once resolved, indistinguishable from one written
by hand: `defaults:` merging, `check_id` derivation, and every validation
rule apply exactly the same way, so factoring an existing config under
`check_sets:` never changes a check's `check_id`. `check_sets:` composes
across files the same way `checks:`/`tables:` do (see Composition,
above): a set defined in one file can be used by a table in any other.

Out of scope, deliberately: per-item threshold overrides (a set's
parameters already cover that), `use:` taking a list of sets, a set
referencing another set, conditionals, and computed expressions inside a
placeholder.

## Validating a config

```bash
dbfresh config validate [-c config.yaml]
```

`dbfresh` never writes a config file, so every check starts as a
hand-typed or pasted YAML block. `config validate` loads the config
exactly as `run` does, but collects every problem it finds instead of
stopping at the first: a malformed check (a missing required field, an
invalid expectation), an unknown check-block field, a check referencing
an undefined source, a duplicate `check_id` -- explicit or derived -- and
an undefined `${VAR}` reference. Each problem is attributed to the file it
came from; a duplicate `check_id` spanning two files is listed under both.

A clean config prints one line and exits `0`:

```text
config.yaml: no problems found
```

A config with problems lists a total, grouped by file, and exits `3`:

```text
2 problems found in 1 file:

config.yaml (2 problems):
  - demo.orders: invalid expectation: object of type 'int' has no len()
  - demo.customers/row_count: unknown check field(s): ['colum']
```

A problem spanning two files -- a duplicate `check_id` -- is listed under
each, and the header says so, since the bullet count then exceeds the
problem total:

```text
1 problem found in 2 files. A problem involving two files is listed under each.

checks/a.yaml (1 problem):
  - duplicate check_id 'dup': demo.orders/row_count and demo.customers/row_count collide -- add an explicit id: to one of them to disambiguate

config.yaml (1 problem):
  - duplicate check_id 'dup': demo.orders/row_count and demo.customers/row_count collide -- add an explicit id: to one of them to disambiguate
```

A problem that blocks resolving the check set at all -- a missing or
unreadable file, invalid YAML, or an `include:` glob matching no files --
can't be collected past; it still raises immediately and prints as a
single `config error: ...` line on stderr, exactly as every other
config-reading command reports it, with the same exit code (`3`).

## Migrating a file to `tables:`

```bash
dbfresh config migrate [-c config.yaml]
```

Converting an existing flat `checks:` list into `tables:` entries by hand
is mechanical. `config migrate` does it for one file: it groups every
check the file defines -- both a flat `checks:` list and any existing
`tables:` entries -- into one `tables:` block, one entry per distinct
`source:`/`object:` pair, and prints that block to stdout. Nothing else
prints there, so it can be redirected straight into a file. Replace the
file's `checks:` and `tables:` with what was printed; every other section
(`sources:`, `store:`, `calendar:`, `defaults:`, `include:`, and every
comment attached to them) is untouched, because migrate never renders
them.

Entries come out in the order their pair first appears in the file, and a
pair's own checks keep their original relative order. Every field on
every check is preserved verbatim -- `id:`, `by_weekday:`, `on_holiday:`,
`where:`, `severity:`, `freshness_source:`, anything else -- minus the
`source:`/`object:` that move up to the entry. Restructuring a config
this way never changes a check's `check_id` (see `check_id` and
identity, above), so it never orphans a stored observation.

A `tables:` entry that pulls in a `check_sets:` battery via `use:` is
carried over unchanged, keeping its `use:`/`with:`/`skip:` -- migrate
never expands it into literal checks, since that would undo the factoring
`check_sets:` exists for and grow the file instead of shrinking it.

Comments attached to the individual checks being regrouped are not
carried over -- the block is re-rendered from parsed data, not copied
text. Comments elsewhere in the file are untouched, since migrate never
renders those parts.

`config migrate` operates on the one file `-c` resolves to, never the
composed config: a config using `include:` keeps checks spread across
files on purpose, and collapsing them into one block would destroy that
layout. When the target file declares `include:`, migrate reports that
on stderr and names each included file, which needs its own run:

```text
config.yaml declares include:; each included file needs its own
`dbfresh config migrate -c <file>` run:
  checks/a.yaml
  checks/b.yaml
```

A file whose checks are already fully grouped, or a file with no checks
at all, prints one line on stderr saying so and emits nothing.
