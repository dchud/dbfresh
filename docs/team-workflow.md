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
