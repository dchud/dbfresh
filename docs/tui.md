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
| `R` | Re-read the config from disk, picking up edits made by hand. |
| `c` | Open **Configure**. |
| `p` | Open **Report** -- the digest from the latest in-session run. |
| `s` | Open **Store** -- observation counts and retention. |
| `f` | Show only rows that aren't OK. |
| `/` | Search the grid. |
| `?` | Open **Help**. |
| `q` | Quit. |

`R` is deliberately a separate key from `r`: config is edited outside the
app, so picking up an edit is a distinct action from running checks, and
the two are easy to confuse by feel. `R` never touches the observation
store or starts a run.

Selecting an object row on Home drills into that object's checks; selecting
a check row there opens that check's **History** drill-down (no separate
keybinding at either level). Below the grid, that drill-down also lists
the object's checks again, each with its expectation, read-only, and
names the config file to edit by hand to change or remove one.

## Configure

The Configure screen is the TUI surface of the [configurator](
authoring-checks.md) -- literally the same `configurator` module
`dbfresh add` uses, so proposals and YAML shape are identical; only the
prompts differ (widgets instead of stdin prompts). The source dropdown
only offers sources already in the config -- defining a new one is
`dbfresh add`'s job, not this screen's. Pick a source, enter an object
name, and press **Propose** to introspect the object and see both its
already-written checks (read-only, for reference) and the newly proposed
bundle. Uncheck any proposed check to trim it, and check any offered
per-column check to add it, then press **Accept**.

**Accept** opens a modal with the selected checks rendered as YAML in a
read-only, selectable text area, plus a **Copy** button -- nothing is
written to the config. A selected check whose `check_id` already exists
anywhere in the composed config is left out of the rendered YAML and
named in a warning instead, so accepting twice can't produce a duplicate.

**Copy** puts the block on the system clipboard over OSC 52, which most
terminals support and macOS's own Terminal.app does not. Where it doesn't
reach, select the text in the modal and copy it the way you normally
would -- the text area stays selectable for exactly that reason.

The proposed `freshness` check gets an editable max_lag field directly
beside its own checkbox, pre-filled with the "24h" default; an offered
`null_rate` or `freshness` check gets the same kind of field beside its
own checkbox. Tune the value before pressing Accept -- the rendered YAML
carries whatever sits in the field at that point.

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
