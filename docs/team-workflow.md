# Team workflow

The config repository is the team's shared, reviewed definition of what
healthy data looks like. A few properties fall out of the design in
[Configuration reference](configuration.md) that make it work as a team
artifact rather than a personal script:

## Portable definitions

The committed YAML holds only definitions -- object names, metrics,
expectations, calendar rules. Secrets and machine-local details never
appear in it: they enter through `${VAR}` interpolation, resolved from the
environment or a gitignored per-user `.env` (see
[Quickstart](quickstart.md)), and the observation-store path is
machine-local too (a CLI flag, an environment variable, or a relative path
that resolves independently per clone). The same committed config runs
against staging and production by supplying different `${...}` values per
environment -- same definitions, different endpoints, no config fork.

## Per-check review

Each check is a self-contained YAML block (source, object, metric,
expectation, and nothing else it depends on), so a pull request that adds
or tunes one check is a few reviewable lines, not a diff across a shared
data structure. Splitting checks across included files (`include:`, one
file per source or per domain) keeps ownership and review routing clear --
the team that owns the warehouse config doesn't need to review changes to
the lakehouse checks, and vice versa.

## History survives refactors

`check_id` derives from *what is measured* (source, object, metric, the
discriminating column/key), never from *where the block lives* in the file
tree. Moving a check between included files, renaming a file, or
reorganizing `checks/` into more files preserves every check's observation
history untouched -- nothing about `dbfresh history` or `vs_previous`
depends on file layout.

## Provenance

Every run records the config repository's `git_sha` (best-effort; `null`
when the config isn't in a git repository, or git isn't available) in the
observation store. Every stored observation is thereby tied to the exact
reviewed config commit that produced it -- useful when auditing "what
threshold was in effect when this alerted."

## A suggested layout

```text
config.yaml          # sources, calendar, store, defaults, include:
checks/
  warehouse.yaml      # checks: [...] for the warehouse source
  lakehouse.yaml      # checks: [...] for the lakehouse source
.env.example          # documents which ${VAR}s a clone needs, no real values
.env                  # gitignored; the real values, per clone
```

`dbfresh add` asks which included file should receive a newly proposed
check whenever `include:` is configured; the TUI's Configure screen resolves
the same choice automatically (the first included file, named in the
proposal) rather than prompting for it. Either way, authoring checks
doesn't require remembering the layout by hand -- see
[Authoring checks](authoring-checks.md).

## Onboarding a clone

`.env.example` is generated from the config rather than hand-maintained:
`dbfresh env-template > .env.example` prints one `NAME=` line per `${VAR}`
the config references, sorted, with the value left blank. Regenerating it
whenever a check introduces a new `${VAR}` keeps the committed file in sync
with what the config actually needs (it also warns on stderr if a `.env`
sitting beside the config isn't gitignored).

A colleague joining the project clones the repo, copies `.env.example` to
`.env`, fills in their own secret values, and keeps `.env` gitignored --
the same pair described in "A suggested layout" above.

The first `dbfresh run` establishes this machine's own observation
baselines, since observations are per-machine local SQLite rather than
something the config repo carries. `dbfresh ui` launches even when a
`${VAR}` secret is still unset -- rather than refusing to start, it shows a
banner naming the missing secrets and where to set them.

## Pulling config updates

`git pull` brings in checks a teammate added or tuned since the last pull.
In the TUI, the `R` binding (Reload) re-reads the pulled config in place,
without a restart.

A check that's new on this machine -- present in the config but with no
local observation yet -- is surfaced on the dashboard as a count ("N checks
not yet run on this machine") and repeated in the reload toast; it
establishes its own baseline the next time it runs.

Tuning a check's expectation -- widening a `between` bound, say -- doesn't
reset its observation history: `check_id` derives from what's measured,
never from the expectation. This is the expectation-edit counterpart to the
file-move guarantee in "History survives refactors" above -- pulling a
threshold edit keeps `dbfresh history` and `vs_previous` continuous for
that check, the same way moving a check between included files does.
