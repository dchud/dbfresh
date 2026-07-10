"""Command-line entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from dbfresh import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dbfresh",
        description=(
            "External, value-level freshness and constraint checks for "
            "SQL Server and Databricks data sources."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"dbfresh {__version__}",
    )
    subcommands = parser.add_subparsers(dest="command")

    run = subcommands.add_parser("run", help="run checks and report")
    run.add_argument("-c", "--config", default="config.yaml")

    return parser


def _run_command(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from dbfresh.adapters.factory import create_adapter
    from dbfresh.config import load_config
    from dbfresh.engine import exit_code, run_checks
    from dbfresh.report import render_digest

    config_path = Path(args.config)
    load_dotenv(config_path.parent / ".env")
    config = load_config(config_path)

    adapters = {
        name: create_adapter(source.type, source.params)
        for name, source in config.sources.items()
    }
    try:
        run = run_checks(adapters, config.checks)
    finally:
        for adapter in adapters.values():
            adapter.close()

    print(render_digest(run))
    return exit_code(run.status)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
