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

- The TUI launches with a banner naming any unset `${VAR}` secrets instead
  of refusing to start.
- The config is located by walking up from the current directory to find
  `config.yaml`, and via a `DBFRESH_CONFIG` environment variable —
  previously only `./config.yaml` or an explicit `-c PATH`.
