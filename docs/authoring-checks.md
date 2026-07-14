# Authoring checks

`configurator.py` is one front-end-agnostic module with two surfaces:
`dbfresh add` (a CLI wizard) and the TUI's Configure screen -- the
proposal flow (introspect, propose, accept/trim) is identical between
them, only the prompt/rendering layer differs; the TUI additionally lets
you edit an already-written check's threshold in place, which the CLI
wizard doesn't (see "What it never does" below). It **emits YAML** into
the version-controlled config; it never writes a check into the
observation store. The design goal is minimal required input: name a source
and an object, and the wizard introspects it and proposes a complete check
bundle, which you accept, edit, or trim check by check -- metadata
proposes, you confirm, and the result is explicit YAML reviewed like any
other config change.

```bash
dbfresh add [-c config.yaml]
```

## The proposal bundle

Given a source and an object, `propose_checks()` introspects it via the
adapter's `describe()` (columns, keys, and whatever catalog stats the
engine can supply) and proposes:

- **`schema` with `unchanged: true`** -- always.
- **`row_count` volume stability** -- always, as `vs_previous` with ratio
  guards seeded at `0.5` / `2.0`; baseline is `last_same_weekday` when the
  config has a `calendar:` block, else `previous`.
- **`freshness` on an auto-detected timestamp column** -- see the
  timestamp heuristic below. When no column candidate exists but the source
  is a Databricks table (not a view), the wizard falls back to
  `freshness_source: describe_history`; on any other engine, or on a view,
  with no candidate, no freshness check is proposed at all.
- **`duplicate_count` (`expect: {max: 0}`)** on each single-column primary
  key or unique constraint found in the object's metadata. Composite keys
  are out of scope for v1 and never proposed.

Every absent capability or missing piece of metadata simply removes its
proposal -- no keys metadata means no `duplicate_count` proposal (you can
still add one manually); the wizard never invents thresholds it can't
justify from catalog metadata. There is no foreign-key graph traversal, no
cross-object inference, and no threshold learning: proposals use only the
named object's own metadata.

## The timestamp-column heuristic

`pick_timestamp_column()` selects the `freshness` column among a table's
`temporal`-category columns:

1. If any column name is conventional (`modified_at`, `updated_at`,
   `loaded_at`, `load_ts`, `created_at`, or ends in `_at` / `_ts` /
   `_date`) and exactly one such conventional name exists, use it.
2. Else, if there is exactly one temporal column at all (even
   unconventionally named), use it.
3. Else, several candidates match with no way to prefer one -- the wizard
   must ask; the CLI and TUI surfaces present the candidate list and let
   you pick.

## Category → offer mapping

Beyond the proposed bundle, the wizard offers additional per-column checks
keyed off the column's canonical `category` -- never its native type name,
so authoring works unchanged across engines. This mapping is generated
directly from `configurator.category_offers()`, the same function the
wizard itself calls, so it can never drift from the actual offers: see the
[generated applicability matrix](reference/matrix.md). `null_rate` is
omitted for a `NOT NULL` column -- the engine already enforces that, so
offering the check would be redundant.

The offered list also excludes any metric already auto-proposed for that
same column (for example, `freshness` on the auto-detected timestamp
column, or `duplicate_count` on a single-column key) -- offering it a
second time would collide on check identity and be silently dropped when
written. The proposed `freshness` check itself gets an editable max_lag
field right beside its checkbox in the TUI Configure screen, pre-filled
with the "24h" default -- change it there before Accept, no round trip
needed. To tune an already-*written* check's threshold afterward, use
the TUI Configure screen's existing-checks list instead (single-value
operators -- `max`, `min`, `max_lag`, and similar -- get an editable
field and a Save button there too) or edit the value directly in the
config; the CLI `dbfresh add` wizard itself still only ever appends new
checks, with no threshold editing at either point.

## Safety and degradation

- **A new source runs a mandatory connection test** (`probe_connection()`)
  before anything about it is written to the config.
- **Every named object is existence-checked** via `describe()`
  (`check_object_exists()`).
- A failed connection or a missing object requires explicit confirmation
  before anything is written. For an *already-configured* source found
  unreachable, the wizard degrades to manual entry and marks existence
  unverified rather than reporting the object as missing (it genuinely
  doesn't know). Manual entry is also the fallback whenever metadata is
  unavailable for any other reason.
- When the config uses `include:`, `target_files()` lists the root config
  plus every resolved included file, in load order. `dbfresh add` asks
  which one receives the new block; the TUI's Configure screen instead
  resolves it automatically to the first included file, naming its choice
  in the proposal rather than prompting for it. Without `include:`, both
  surfaces always append to the root config.

## What it never does

The configurator appends well-formed YAML check blocks (via
`append_checks()`) and, for a brand-new source, a `sources:` entry (via
`add_source()`); every check it proposes still has to pass the same
review as a hand-written one. It never writes an observation and never
touches the SQLite store. The one exception to "never mutates an existing
check block" is `rewrite_check_expectation()` -- the TUI Configure
screen's existing-checks editor -- which rewrites only a single-value
`expect:` operand in place (preserving comments/formatting via a text
splice where possible, falling back to a full YAML round trip otherwise);
it never changes a check's identity fields (`source`, `object`, `metric`,
`column`/`key`), so `check_id` and observation history are unaffected.
