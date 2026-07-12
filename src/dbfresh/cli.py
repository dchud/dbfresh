"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import os
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
    run.add_argument("--json", action="store_true", help="machine-readable output")
    run.add_argument("--store", default=None, help="observation store path")
    run.add_argument(
        "--no-store", action="store_true", help="do not persist observations"
    )

    history = subcommands.add_parser("history", help="show a check's recent history")
    history.add_argument("object")
    history.add_argument("--source", default=None)
    history.add_argument("--metric", default=None)
    history.add_argument("-n", type=int, default=30, help="observations to show")
    history.add_argument("-c", "--config", default="config.yaml")
    history.add_argument("--store", default=None, help="observation store path")

    prune = subcommands.add_parser("prune", help="enforce observation retention")
    prune.add_argument("-c", "--config", default="config.yaml")
    prune.add_argument("--store", default=None, help="observation store path")

    return parser


def _resolve_read_context(config_path: Path):
    """Config dir and store settings for a read-only store command.

    Tolerant of a missing config file: history/prune only need it for
    default store-path resolution and retain_days, not sources/checks.
    """
    from dbfresh.config import load_config

    if config_path.exists():
        config = load_config(config_path)
        return config.config_dir, config.store
    return Path.cwd(), None


def _run_command(args: argparse.Namespace) -> int:
    from dotenv import load_dotenv

    from dbfresh.adapters.factory import create_adapter
    from dbfresh.config import load_config
    from dbfresh.engine import exit_code, run_checks
    from dbfresh.report import render_digest, render_json
    from dbfresh.store import Store, capture_git_sha, resolve_store_path

    config_path = Path(args.config)
    load_dotenv(config_path.parent / ".env")
    config = load_config(config_path)

    adapters = {
        name: create_adapter(source.type, source.params)
        for name, source in config.sources.items()
    }

    # Opened before run_checks (not just after) so history-based
    # expectations (schema unchanged; later vs_previous) can read prior
    # observations during evaluation; at that point the store holds only
    # earlier runs, since this run's results are persisted below.
    store = None
    if not args.no_store:
        store_path = resolve_store_path(
            config_dir=config.config_dir,
            store_config=config.store,
            cli_store=args.store,
            env_store=os.environ.get("DBFRESH_STORE"),
        )
        store = Store(store_path)

    try:
        run = run_checks(adapters, config.checks, calendar=config.calendar, store=store)
    finally:
        for adapter in adapters.values():
            adapter.close()

    if store is not None:
        try:
            run_id = store.start_run(git_sha=capture_git_sha(config_path))
            for result in run.results:
                store.record_observation(run_id, result, calendar=config.calendar)
            store.finish_run(run_id, run.status)
        finally:
            store.close()

    print(render_json(run) if args.json else render_digest(run))
    return exit_code(run.status)


def _history_command(args: argparse.Namespace) -> int:
    from dbfresh.report import render_candidates, render_history
    from dbfresh.store import Store, resolve_store_path

    config_dir, store_config = _resolve_read_context(Path(args.config))
    store_path = resolve_store_path(
        config_dir=config_dir,
        store_config=store_config,
        cli_store=args.store,
        env_store=os.environ.get("DBFRESH_STORE"),
    )
    store = Store(store_path)
    try:
        candidates = store.find_checks(
            args.object, source=args.source, metric=args.metric
        )
        if not candidates:
            print(f"no observations found for {args.object!r}")
            return 1
        if len(candidates) > 1:
            print(render_candidates(args.object, candidates))
            return 2
        rows = store.history(candidates[0]["check_id"], limit=args.n)
        print(render_history(candidates[0], rows))
        return 0
    finally:
        store.close()


def _prune_command(args: argparse.Namespace) -> int:
    from dbfresh.config import StoreConfig
    from dbfresh.store import Store, resolve_store_path

    config_dir, store_config = _resolve_read_context(Path(args.config))
    store_path = resolve_store_path(
        config_dir=config_dir,
        store_config=store_config,
        cli_store=args.store,
        env_store=os.environ.get("DBFRESH_STORE"),
    )
    retain_days = (store_config or StoreConfig()).retain_days
    store = Store(store_path)
    try:
        deleted = store.prune(retain_days=retain_days)
        print(f"pruned {deleted} observation(s) older than {retain_days} days")
        return 0
    finally:
        store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run_command(args)
    if args.command == "history":
        return _history_command(args)
    if args.command == "prune":
        return _prune_command(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
