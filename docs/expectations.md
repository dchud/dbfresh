# Expectations & durations

An expectation is a single operator compared against a metric's observed
scalar. The full operator table -- name and meaning -- is generated from the
code: see the [generated operator reference](reference/operators.md). This
page covers the rules around operators that a flat table can't:

## One expectation per check

A check carries exactly one operator: a static bound (`between`, `max`,
`min`, `equals`, `lt`, `gt`, and their aliases `lte`/`gte`/`eq`), `max_lag`,
`vs_previous`, or `unchanged`/`equals` on a schema check -- never several.
`{min: x, max: y}` on one check is a validation error; use `between: [x, y]`
instead. Composing a static bound with `vs_previous` on the same check is a
documented future extension, not supported today.

Some operators are restricted to specific metrics:

- `unchanged` is only valid on a `schema` check; a `schema` check accepts
  only `unchanged` or `equals`/`eq` (a pinned fingerprint), nothing else.
- `vs_previous` applies only to numeric metrics -- it is a validation error
  on `freshness` and `schema`.
- `max_lag` is meaningful only on `freshness` (it compares a lag in
  seconds, computed from `now - max_ts`).

## Duration syntax

Durations used by `max_lag:` parse compound forms of `<integer><unit>`
tokens back to back, with no separators: `26h`, `2d`, `90m`, `45s`,
`1h30m`. Supported units are `s` (seconds), `m` (minutes), `h` (hours), and
`d` (days); anything else -- an empty string, a bare number, an unknown
unit, trailing garbage -- is a validation error raised eagerly at config
load time, not at run time.

## `allow_empty`

A metric that returns a `null` scalar -- an empty table, `MAX()` of no rows
-- fails its expectation by default (`FAIL`, or `WARN` under
`severity: warn`), since a silently-empty table is exactly the kind of
failure dbfresh exists to catch. A check that legitimately expects an empty
result some of the time opts in with `allow_empty: true`, which turns that
`null` case into `OK` instead. `null_rate` is the one exception: a `null`
scalar there means the table itself was empty (division by zero, guarded by
`NULLIF`), which is always reported as `ERROR` regardless of `allow_empty`,
since "no rows to compute a rate over" is a different failure mode than "the
rate was fine."

## Severity

Every check has `severity: error | warn` (default `error`). A failing
`warn` check yields status `WARN` (exit code `1`) instead of `FAIL` (exit
code `2`) -- for soft bounds you want visible in the digest and the
dashboard without paging anyone.
