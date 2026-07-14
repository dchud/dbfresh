# TUI guide

`dbfresh ui [-c config.yaml] [--store PATH]` launches an interactive
Textual application over the same config, engine, and observation store the
batch CLI uses. It adds no check semantics of its own -- it's a second front
end, not a second source of truth. `run` (not `ui`) is what a scheduler
should invoke; the TUI is for a human looking around.

## Home -- the status grid

A grid: one row per `source.object`, columns `overall` (the latest stored
observation, rolled up across that object's checks) plus the last 7
calendar days, each colored from the worst status observed that day --
`green` (`OK`), `yellow` (`WARN`), `red` (`FAIL`/`ERROR`), dim (`SKIPPED`
or no run that day). Selecting a row drills into that object's individual
checks at the same `[overall, last 7 days]` shape, one row per check.

## Keybindings

| key | action |
| --- | --- |
| `r` | Run every configured check now, refresh the grid. |
| `c` | Open **Configure**. |
| `p` | Open **Report** -- the digest from the latest in-session run. |
| `q` | Quit. |

Selecting an object row on Home drills into that object's checks; selecting
a check row there opens that check's **History** drill-down (no separate
keybinding at either level).

## Configure

The Configure screen is the TUI surface of the [configurator](
authoring-checks.md) -- literally the same `configurator` module
`dbfresh add` uses, so proposals, YAML shape, and safety behavior are
identical; only the prompts differ (widgets instead of stdin prompts).
Enter a source and object name, press **Propose** to introspect the object
and see the proposed check bundle, then **Accept** to append it to the
config (the root config, or the first included checks file when `include:`
is configured) exactly as `dbfresh add` would. Accepting reloads the config
and refreshes the Home grid so the new checks show up (dim, no observation
yet, until the next run).

## Report

Shows the digest ([`render_digest`](checks.md)) for the run triggered in
this TUI session. Until you press `r` at least once, there's nothing to
show yet -- the observation store's flattened rows don't retain enough to
reconstruct a full digest (sample violation rows and error text aren't
persisted, only the scalar/fingerprint and status), so Report is
session-scoped rather than replaying the store's history.

## History drill-down

The interactive form of `dbfresh history`: a selected check's recent
values, statuses, and a simple trend column, read straight from the store
-- the same [`render_history`](history.md) the CLI's `history` command
uses.

## Testing

The TUI is exercised with Textual's `App.run_test()` / `Pilot` harness:
simulated key presses and widget queries, asserting on rendered grid
cells, screen contents, and navigation -- no real terminal required.
