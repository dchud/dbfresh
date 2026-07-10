"""Command-line entrypoint."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
