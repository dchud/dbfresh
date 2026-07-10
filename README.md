# dbfresh

External, value-level freshness and constraint checks for SQL Server and
Databricks data sources. `dbfresh` answers one question cheaply and from outside
the systems it watches: are the values in these tables what they should be right
now? It validates data values, not job status.

- Full specification: [`dbfresh.md`](dbfresh.md)
- Scope and tooling: [`AGENTS.md`](AGENTS.md)
- Build/test commands and working agreement: [`CLAUDE.md`](CLAUDE.md)

## Quickstart

```bash
uv sync            # create the environment
just check         # lint + format check + tests
uv run dbfresh --version
```
