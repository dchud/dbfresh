# TUI guide

`dbfresh ui [-c config.yaml] [--store PATH]` launches an interactive
Textual application over the same config, engine, and observation store the
batch CLI uses. It adds no check semantics of its own -- it's a second front
end, not a second source of truth. `run` (not `ui`) is what a scheduler
should invoke; the TUI is for a human looking around.

## Home -- the status dashboard

A tree grouped by the check tiers: source → object, with the object's
table-level checks (`row_count`, `schema`, assertions) as direct leaves
under the object node, and column/key-level checks nested under an
intermediate node per column or key. Every node's own status is the worst
of its children, colored from the *latest stored observation* per check --
`green` (`OK`), `yellow` (`WARN`), `red` (`FAIL`/`ERROR`), dim (`SKIPPED`).
A node with no stored observation yet renders as "unknown" rather than
winning or losing against a real status, until the next run.

## Keybindings

| key | action |
| --- | --- |
| `r` | Run every configured check now, refresh the dashboard. |
| `c` | Open **Configure**. |
| `p` | Open **Report** -- the digest from the latest in-session run. |
| `q` | Quit. |

Selecting a check's leaf node in the dashboard tree opens that check's
**History** drill-down directly (no separate keybinding).

## Configure

The Configure screen is the TUI surface of the [configurator](
authoring-checks.md) (§11) -- literally the same `configurator` module
`dbfresh add` uses, so proposals, YAML shape, and safety behavior are
identical; only the prompts differ (widgets instead of stdin prompts).
Enter a source and object name, press **Propose** to introspect the object
and see the proposed check bundle, then **Accept** to append it to the
config (the root config, or the first included checks file when `include:`
is configured) exactly as `dbfresh add` would. Accepting reloads the config
and refreshes the Home dashboard so the new checks show up (as "unknown"
until the next run).

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
simulated key presses and widget queries, asserting on the rendered tree
labels, screen contents, and navigation -- no real terminal required.
