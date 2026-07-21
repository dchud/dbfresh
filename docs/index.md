# dbfresh

External, value-level freshness and constraint checks for SQL Server and
Databricks data sources.

`dbfresh` answers one question cheaply and from outside the systems it
watches: are the values in these tables what they should be right now? It is
a data-**value** validator, not a job monitor -- it never inspects whether an
extract or ETL job "ran"; it inspects the data those jobs produced. A silent
empty load or a partial extract surfaces as a count, range, freshness, or
null-rate violation.

> **Note:** dbfresh is developed with agentic coding tools.

## Where to start

- New to dbfresh? Start with [Quickstart](quickstart.md).
- Want the mental model before the how-to? Read [Concepts](concepts.md).
- Looking for a specific check, operator, or flag? See the reference
  section in the navigation -- [Check reference](checks.md),
  [Expectations & durations](expectations.md), and the generated
  [CLI reference](reference/cli.md).
- Rolling this out for a team? See [Team workflow](team-workflow.md).
- Adding a new database engine? See
  [Extending -- adding a source type](extending.md).

## What it checks

- Two tiers of value checks: table-level (row-count ranges, schema/shape
  stability, whole-table assertions) and column-level (freshness, aggregate
  bounds, null-rate, uniqueness).
- Arbitrary SQL assertions that must return zero rows.
- A local observation history enabling "compare to previous run" and trend
  inspection.
- Weekend- and holiday-aware expectations for a weekday-heavy business.
- A batch CLI for scheduler-driven alerting and an interactive Textual TUI
  over the same engine and store.
