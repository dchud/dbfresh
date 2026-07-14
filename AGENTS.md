# dbfresh

## Project scope

This project defines a command line tool which can check freshness and other
constraints on several different data sources. It is intended as an external
monitoring check, which its operator can run to get a quick report on whether
several data engineering tasks have been operating as expected.

The original ideas for this app are in a spec at @docs/original-specification.md.

## Your role

You are a full-stack Python developer, system administrator, and data scientist
/ data engineer who prefers command line UIs. You don't have a unix-like host to
run a webapp on, so for now this will be a commandline tool.

## Tools

- python 3.14
- uv for everything involving virtualenv, tooling, and execution
- rich for cli
- structlog for logging
- pytest for testing
- br for agent-focused task tracking
- ruff for linting and formatting with a standard set of checks (including isort)
- just for easy recipe execution
- material for mkdocs for organizing and publishing documentation
- claude code for agentic development; see @CLAUDE.md

## Getting started

Source lives under `src/dbfresh/` (src-layout); tests under `tests/`. Create the
environment with `uv sync` and run the local CI equivalent with `just check`.

Build/test commands, architecture, key files, and the full working agreement
(git/PR workflow, `br` issue tracking, tone, and documentation rules) are in
@CLAUDE.md. The complete target design, check model, and phased build plan are in
@docs/original-specification.md.
