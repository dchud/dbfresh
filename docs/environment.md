# Environment & secrets

Config uses `${VAR}` tokens anywhere a connection parameter or secret would
otherwise have to be written in plain text -- most often inside `sources:`.
The variable names are yours to choose; `config.example.yaml` at the repo
root shows a canonical set (`MSSQL_URL`, `DATABRICKS_HOST`,
`DATABRICKS_HTTP_PATH`, `DATABRICKS_TOKEN`), reused below and in
`.env.example`. Supply each `${VAR}` as a real environment variable, or via
a gitignored, per-user `.env` file next to the config.

## `.env` loading

Every command that reads a config -- `run`, `history`, `prune`, `add`,
`ui`, `env-template` -- loads `.env` from the config file's directory
automatically, before parsing the config. A real environment variable
already set takes precedence over the same name in `.env`. This is why a
config committed to a team repo never carries a credential: the YAML holds
only `${VAR}` references, and each person's or environment's actual values
live in their own untracked `.env` (or CI secrets, or the shell
environment) instead.

## SQL Server (`type: sqlserver`)

| field | example variable | value shape                                            | where to get it / notes                                                                                                                                                                                                             |
| ----- | ------------------ | -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `url` | `${MSSQL_URL}`     | `sqlserver://user:password@host:1433/Database`          | A single usql-style connection URL (aliases `mssql://` and `ms://` also work). The path segment is the database name. Percent-encode a password containing `:`, `/`, `@`, `?`, `#`, or `%` -- it's URL-decoded before use.        |
| `url` | `${MSSQL_URL}`     | `sqlserver://user:password@host/Instance?database=Database` | Named-instance form: put the instance name where the database normally goes, and disambiguate the real database with `?database=`. The port is always omitted for a named instance -- SQL Server resolves it dynamically via SQL Browser. |

`timeout` (connection timeout, seconds) and `timezone` (for interpreting
naive timestamp columns) are plain literals on the source, not usually
worth putting behind a `${VAR}` -- they aren't secrets and rarely differ
per environment.

## Databricks (`type: databricks`)

| field       | example variable          | value shape                              | where to get it / notes                                                                                       |
| ----------- | -------------------------- | ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- |
| `host`      | `${DATABRICKS_HOST}`       | `dbc-abc12345-6789.cloud.databricks.com` | The SQL warehouse's server hostname, from its **Connection Details** tab in the Databricks UI.                    |
| `http_path` | `${DATABRICKS_HTTP_PATH}`  | `/sql/1.0/warehouses/abcdef0123456789`   | The same warehouse's HTTP path, also from **Connection Details**.                                                  |
| `token`     | `${DATABRICKS_TOKEN}`      | `dapi` + 32 hex chars                    | A personal access token, generated under the user's **Settings -> Developer -> Access tokens**. Treat as a secret. |

## SQLite (`type: sqlite`)

| field      | example variable     | value shape | where to get it / notes                                                                                                    |
| ---------- | --------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------ |
| `database` | `${DEMO_DB_PATH}`     | `./demo.db`  | A filesystem path, not a secret -- still commonly kept behind a `${VAR}` so the path itself (which differs per machine) doesn't end up hardcoded in the committed config. |

## The copyable pair

`config.example.yaml` and `.env.example`, both at the repo root, use the
same canonical variable names as this page. `.env.example` is generated
from the config with `dbfresh env-template` rather than hand-maintained,
so it always matches the `${VAR}`s the config actually references. Copy
`config.example.yaml` to `config.yaml` and `.env.example` to `.env`, fill
in real values in `.env`, and never commit the real `.env`.
