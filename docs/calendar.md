# Calendar & scheduling

A weekday-heavy business needs both weekend and holiday awareness. dbfresh
provides two independent, composable mechanisms, both built on one
top-level calendar block.

## The business calendar

Defined once, referenced by checks:

```yaml
calendar:
  timezone: America/New_York
  workdays: [mon, tue, wed, thu, fri] # default; the rest are non-business
  holidays:
    country: US # via the `holidays` package
    subdivision: null # e.g. a state code, optional
    extra: ["2026-11-27"] # explicit additional dates
    remove: [] # dates to treat as workdays anyway
```

Holiday dates come from the `holidays` package
(`holidays.country_holidays(country, subdiv=subdivision)`), unioned with
`extra` and minus `remove`. The concrete jurisdiction is a deployment
choice, not a code constant -- organization-specific closures belong in
`extra:`. A **business day** is a `workdays` weekday that is not a holiday.
All weekday/holiday logic evaluates in the calendar's `timezone`, never the
host's local time or UTC.

Any check that uses `by_weekday`, `on_holiday`, `calendar: business`, or
`skip_off_schedule` requires a top-level `calendar:` block to be configured
-- using one of those fields with no calendar is a validation error.

## Per-weekday / holiday expectation overrides

A check can override its expectation based on the weekday of the run:

```yaml
- source: warehouse
  object: dbo.fct_sales
  metric: row_count
  expect: { between: [10000, 500000] } # default (Tue-Fri)
  by_weekday:
    mon: { between: [0, 500000] } # Monday reflects a quiet weekend
    sat: { max: 100 }
    sun: { max: 100 }
  on_holiday: { max: 100 } # optional; used when today is a holiday
```

Selection precedence for the effective expectation, evaluated against the
run's current date in the calendar timezone:

1. `on_holiday`, if today is a holiday and the key is present.
2. `by_weekday[today]`, if present.
3. The base `expect:`.

## Business-time freshness

Freshness can opt into the calendar instead of wall-clock lag:

```yaml
- metric: freshness
  column: modified_at
  expect: { max_lag: 26h }
  calendar: business # default is wall-clock
```

- **Wall-clock (default):** `lag = now - max_ts`.
- **Business:** `lag = business_time_between(max_ts, now)` -- wall-clock
  elapsed **minus 24h for each whole non-business date strictly between**
  the two timestamps' calendar dates (both converted to the calendar
  timezone first).

For example: data last written Friday 18:00, checked Monday 07:00 -- Saturday
and Sunday are non-business, so business lag is roughly
`61h − 48h = 13h`, which passes a 26h threshold that wall-clock lag would
have tripped.

## Skipping on non-business days

Global or per-check `skip_off_schedule: true` (alias `skip_on_holiday`)
records the affected checks as `SKIPPED` (exit code `0`, excluded from
failure counts) instead of evaluating them, whenever the run date is not a
business day -- a non-workday weekday or a holiday. Default `false`: most
checks should keep running with `by_weekday` / `on_holiday` overrides
instead, so reserve `skip_off_schedule` for checks that are genuinely
meaningless off-schedule.

## Running under a scheduler

dbfresh has no built-in scheduler; `dbfresh run` is designed to be invoked
by one. The exit code (see the
[generated CLI reference](reference/cli.md)) is the contract:

- **cron**, once daily: capture stdout for the digest, and let cron's
  mail-on-nonzero-exit behavior (or a wrapper that checks `$?`) page on
  `WARN`/`FAIL`/`ERROR`.
- **systemd timer + service**: pair a `.timer` unit with a `.service` unit
  running `dbfresh run --json -c /path/to/config.yaml`; `OnFailure=` on the
  service can trigger an alerting unit when the exit code is nonzero.
- Either way, `by_weekday` / `on_holiday` / `skip_off_schedule` mean the
  scheduler itself doesn't need weekend/holiday logic -- it can run every
  day, and dbfresh decides what "normal" looks like for that day.
