# History & trends

## The observation store

Check *definitions* stay in YAML; the store holds only *observations* --
enabling "today vs. previous" comparisons and trend inspection without
turning the config into a database. It's a local SQLite file, one
`run` row per invocation and one `observation` row per check per run
(including `OK` and `ERROR` runs -- every check writes an observation, not
just failures).

The store path resolves with precedence `--store` flag →
`DBFRESH_STORE` env var → `store.path` in config → default
`./dbfresh.db`. A relative `store.path` (the default included) resolves
against the *root config's* directory, never the process's current
directory -- so every clone of a shared config repo gets its own store file
without a machine-specific path ever being committed.

Each run also records the config repository's `git_sha` (best-effort HEAD
of the git repo containing the root config; `null` when the config isn't in
a repository or git is unavailable) -- tying every observation to the
reviewed config commit that produced it.

## `vs_previous`

A numeric metric can compare its current value to a prior observation of
the same check instead of (or as well as) a static bound:

```yaml
metric: row_count
expect:
  vs_previous:
    baseline: previous # previous | last_same_weekday
    min_ratio: 0.5 # current/baseline within [0.5, 2.0]
    max_ratio: 2.0
    # optional absolute guards instead of / alongside ratios:
    # min_delta / max_delta
    on_missing: pass # pass | warn | skip   (no baseline available)
```

- `baseline: previous` -- the most recent prior observation with status
  `OK`, `WARN`, or `FAIL` (`ERROR`/`SKIPPED` excluded, so a broken or
  skipped run never becomes the comparison point). With daily runs, this is
  "about one day later."
- `baseline: last_same_weekday` -- the most recent prior observation whose
  stored `weekday` matches today's and whose `observed_at` is at least 6
  calendar days back -- the right baseline for a weekday-heavy business
  (compare this Monday to last Monday, not to Friday). The 6-day floor skips
  a same-week rerun while tolerating a run that slips by a day.
- Ratio guards require a nonzero baseline; a zero baseline falls back to
  delta guards when configured, else is treated as a missing baseline.
- No baseline (first run, or nothing matches): evaluated per `on_missing`
  (default `pass`).
- `vs_previous` requires the observation store -- with `--no-store` nothing
  ever accumulates, so every run stays permanently on the `on_missing` path.
- Like every expectation, a check uses `vs_previous` *or* a static bound,
  never both.

`schema`'s `unchanged:` operator is its own history comparison, reusing the
same store -- see [Check reference](checks.md).

## `dbfresh history`

Reads the store; read-only, never touches a source.

```bash
dbfresh history OBJECT [--source S] [--metric M] [-n 30] [-c config.yaml]
```

`OBJECT` may match checks across multiple sources or metrics on the same
object name; an ambiguous match lists the candidates (with their
`check_id`) instead of guessing, and `--source`/`--metric` disambiguate. The
output is the check's recent values, statuses, and a simple up/down trend
column derived from consecutive numeric values.

## Retention and `prune`

`store.retain_days` (default 400 -- enough for a year of trend plus
`last_same_weekday` lookups) bounds how long observations are kept.
`dbfresh prune [--store PATH]` deletes observations (and any run rows left
with no remaining observations) older than the configured retention; it
does not run automatically, so schedule it separately (e.g. weekly) if the
store should stay bounded in size.
