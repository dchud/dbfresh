# Changelog

All notable, user-facing changes to dbfresh are recorded here. The format
is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
the project aims to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once releases are tagged.

## [Unreleased]

### Added

- `dbfresh env-template` prints an `.env` template listing the `${VAR}`
  secrets a config references, for seeding a committed `.env.example`.
- Databricks sources can authenticate as a service principal (OAuth M2M)
  with `auth_type: oauth_m2m` plus `client_id` and `client_secret`,
  alongside the existing personal access token.
- A warning when a `.env` beside a git-tracked config is not gitignored,
  from both `dbfresh env-template` and the TUI.
- The Home dashboard shows a count of checks not yet run on this machine,
  and repeats it in the config-reload toast — surfacing checks a pulled
  config added.
- Documentation of the versioned-config and `.env` team sharing workflow.

### Changed

- `dbfresh add` no longer edits config files. It runs the same live-source
  analysis and prints the proposed source and checks as YAML on stdout for
  you to paste; wizard prompts and guidance move to stderr so stdout holds
  only the YAML. Checks already defined anywhere in the composed config are
  still left out of the proposal.
- The TUI no longer edits config files. The object-detail screen shows
  each check's threshold read-only, alongside the config path to edit by
  hand. The Configure screen still proposes checks against a live source,
  but Accept now shows the proposed YAML in a copyable, read-only text
  area instead of writing it; adding, editing, or removing a source is no
  longer possible from the TUI.
- The TUI launches with a banner naming any unset `${VAR}` secrets instead
  of refusing to start.
- The config is located by walking up from the current directory to find
  `config.yaml`, and via a `DBFRESH_CONFIG` environment variable —
  previously only `./config.yaml` or an explicit `-c PATH`.
- The TUI's run-complete toast points at the `p` report and stays on
  screen when a run has failures to review.
- The object-detail screen shows the highlighted check's error (or
  expected vs observed) inline, without drilling into its history.
- The run report's failing checks are selectable — pressing Enter on one
  opens that check's history.
- A `freshness` check on a numeric, boolean, or other non-date/datetime
  column fails with a message naming the column and its type, before the
  query runs. A text column holding ISO timestamps stays valid.
- The object-detail screen's run affordance is labeled "Run these checks",
  or "Run this check" when the object has a single check (was "Run this
  object").
- During a run, each check's status glyph updates as its result arrives —
  on the Home dashboard (per object) and the object-detail screen (per
  check) — instead of all at once when the run finishes.
- A check's status glyph briefly highlights when it updates live during a
  run, then clears, so the change catches the eye.
- The Home dashboard shows a progress bar that fills as a run's checks
  complete, alongside the existing "running checks: N/total" subtitle.

### Fixed

- Drilling into an object while a run is in flight now shows the results
  that run has already produced for it, instead of reading as never-run
  until the whole run finishes.
- A TUI left open past midnight no longer errors and freezes on the next
  run. The Home dashboard's trailing-day columns are brought up to date
  when a run starts, so the new day's results have a column to land in.
- The run-complete toast no longer offers `p` for the report after a run
  finished from a screen where the report can't be opened — the report is
  Home-only, so a run started from the object-detail screen now completes
  without a dead hint.
- A freshness check on a `date`-typed column no longer crashes; a
  date-only value is treated as midnight in the source timezone.
- The `databricks` extra now installs `pyarrow` (optional in
  databricks-sql-connector since 4.0), so `dbfresh[databricks]` can fetch
  query results out of the box.
- `freshness_source: describe_history` counts every Databricks data
  operation (e.g. `CREATE OR REPLACE TABLE AS SELECT`, `STREAMING UPDATE`,
  `COPY INTO`), not just `WRITE`/`MERGE`/`DELETE`/`UPDATE`, so a table
  written by one of those no longer reports no observation.
