"""Command-line entrypoint."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from dbfresh import __version__

_CONFIG_ERROR_EXIT = 2


def _report_config_error(exc: ValueError) -> int:
    """Print a config validation failure as one clean stderr line.

    Used at every command boundary that calls ``load_config`` so a
    validation problem (unknown source reference, duplicate check_id,
    calendar features without a calendar block, operator misuse, ...) exits
    cleanly instead of surfacing as an unhandled traceback.
    """
    print(f"config error: {exc}", file=sys.stderr)
    return _CONFIG_ERROR_EXIT


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

    add = subcommands.add_parser("add", help="interactive check-authoring wizard")
    add.add_argument("-c", "--config", default="config.yaml")

    ui = subcommands.add_parser("ui", help="interactive Textual dashboard")
    ui.add_argument("-c", "--config", default="config.yaml")
    ui.add_argument("--store", default=None, help="observation store path")

    return parser


def _resolve_read_context(config_path: Path):
    """Config dir, store settings, and calendar for a read-only store command.

    Tolerant of a missing config file: history/prune only need it for
    default store-path resolution and retain_days, not sources/checks.
    """
    from dbfresh.config import load_config

    if config_path.exists():
        config = load_config(config_path)
        return config.config_dir, config.store, config.calendar
    return Path.cwd(), None, None


def _run_command(args: argparse.Namespace) -> int:
    from dbfresh.config import load_config
    from dbfresh.engine import exit_code
    from dbfresh.report import display_timezone, render_digest, render_json
    from dbfresh.runner import run_and_persist
    from dbfresh.store import Store, resolve_store_path

    config_path = Path(args.config)
    try:
        config = load_config(config_path)
    except ValueError as exc:
        return _report_config_error(exc)

    # Opened before run_and_persist (not just after) so history-based
    # expectations (schema unchanged; vs_previous) can read prior
    # observations during evaluation; at that point the store holds only
    # earlier runs, since this run's results are persisted as part of the
    # same call.
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
        run = run_and_persist(config, store)
    finally:
        if store is not None:
            store.close()

    if args.json:
        print(render_json(run))
    else:
        print(render_digest(run, tz=display_timezone(config.calendar)))
    return exit_code(run.status)


def _history_command(args: argparse.Namespace) -> int:
    from dbfresh.report import display_timezone, render_candidates, render_history
    from dbfresh.store import Store, resolve_store_path

    try:
        config_dir, store_config, calendar = _resolve_read_context(Path(args.config))
    except ValueError as exc:
        return _report_config_error(exc)
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
        print(render_history(candidates[0], rows, tz=display_timezone(calendar)))
        return 0
    finally:
        store.close()


def _prune_command(args: argparse.Namespace) -> int:
    from dbfresh.config import StoreConfig
    from dbfresh.store import Store, resolve_store_path

    try:
        config_dir, store_config, _calendar = _resolve_read_context(Path(args.config))
    except ValueError as exc:
        return _report_config_error(exc)
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


def _prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    answer = input(f"{msg}{suffix}: ").strip()
    return answer or default


def _confirm(msg: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = input(f"{msg} ({hint}): ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes")


def _build_offered_check(source, obj, column, metric, has_calendar):
    from dbfresh.configurator import build_check

    if metric == "null_rate":
        value = float(_prompt("    max null rate", "0.05"))
        return build_check(
            source, obj, "null_rate", column=column, expect={"max": value}
        )
    if metric in ("sum", "avg", "min", "max"):
        baseline = "last_same_weekday" if has_calendar else "previous"
        guards = {"baseline": baseline, "min_ratio": 0.5, "max_ratio": 2.0}
        return build_check(
            source, obj, metric, column=column, expect={"vs_previous": guards}
        )
    if metric == "duplicate_count":
        return build_check(
            source, obj, "duplicate_count", key=column, expect={"max": 0}
        )
    if metric == "freshness":
        return build_check(
            source,
            obj,
            "freshness",
            column=column,
            freshness_source="column",
            expect={"max_lag": "24h"},
        )
    raise ValueError(f"unsupported offered metric: {metric!r}")


def _select_source(config, config_path):
    """Prompt for a source name; a new source is probed before anything else.

    Returns ``(source_name, adapter, aborted, new_source)``. ``adapter`` is
    ``None`` when the source could not be reached, degrading the rest of
    the wizard to manual entry with unverified existence (§11.3); ``aborted``
    is ``True`` only when the user declined to add an unreachable new
    source. ``new_source`` is ``(type_, params)`` when the user just defined
    a brand-new source -- written to the config only once the rest of the
    wizard confirms, never before the connection test (§11.3).
    """
    from dbfresh.adapters.factory import create_adapter
    from dbfresh.configurator import probe_connection

    sources = config.sources if config else {}
    if sources:
        print("Existing sources: " + ", ".join(sources))
    source_name = _prompt("Source name")

    if source_name in sources:
        src = sources[source_name]
        probe = probe_connection(src.type, src.params)
        if probe.ok:
            return source_name, create_adapter(src.type, src.params), False, None
        print(f"warning: could not reach {source_name!r}: {probe.error}")
        print("degrading to manual entry; existence will be unverified")
        return source_name, None, False, None

    print(f"{source_name!r} is a new source.")
    type_ = _prompt("Source type (e.g. sqlite)")
    params: dict = {}
    print("Enter connection params as key=value (blank line to finish):")
    while True:
        line = input("  ").strip()
        if not line:
            break
        key, _sep, value = line.partition("=")
        params[key.strip()] = value.strip()

    probe = probe_connection(type_, params)
    if not probe.ok:
        print(f"connection test failed: {probe.error}")
        if not _confirm("Add this source anyway (unverified)?"):
            return source_name, None, True, None
        return source_name, None, False, (type_, params)

    print("connection test passed")
    return source_name, create_adapter(type_, params), False, (type_, params)


def _add_command(args: argparse.Namespace) -> int:
    from dbfresh.config import load_config
    from dbfresh.configurator import (
        add_source,
        append_checks,
        check_object_exists,
        offered_column_checks,
        propose_checks,
        target_files,
    )

    config_path = Path(args.config)
    try:
        config = load_config(config_path) if config_path.exists() else None
    except ValueError as exc:
        return _report_config_error(exc)
    has_calendar = config.calendar is not None if config else False

    source_name, adapter, aborted, new_source = _select_source(config, config_path)
    if aborted:
        print("aborted")
        return 1

    object_name = _prompt("Object name")
    existence = check_object_exists(adapter, object_name)
    info = existence.info
    if not existence.verified:
        print("existence unverified (source unreachable)")
        if not _confirm("Continue with manual entry?"):
            return 1
    elif not existence.exists:
        print(f"warning: {object_name!r} not found: {existence.error}")
        if not _confirm("Add checks for it anyway?"):
            return 1

    proposed: list[dict] = []
    if info is not None:
        bundle = propose_checks(
            source_name, object_name, info, adapter.dialect, has_calendar=has_calendar
        )
        print(f"Proposed {len(bundle)} check(s):")
        for block in bundle:
            print(f"  - {block}")
        if _confirm("Accept the full proposed bundle?", default=True):
            proposed = list(bundle)
        else:
            for block in bundle:
                if _confirm(f"  include {block['metric']}?", default=True):
                    proposed.append(block)

        for offer in offered_column_checks(info.columns):
            if not offer["checks"]:
                continue
            print(
                f"Offered for {offer['column']} ({offer['category']}): "
                + ", ".join(offer["checks"])
            )
            choice = _prompt("  add which (comma-separated, blank to skip)", "")
            for metric in (m.strip() for m in choice.split(",") if m.strip()):
                proposed.append(
                    _build_offered_check(
                        source_name, object_name, offer["column"], metric, has_calendar
                    )
                )
    else:
        print("no metadata available; add checks manually by editing the YAML")

    if adapter is not None:
        adapter.close()

    if new_source is not None:
        add_source(config_path, source_name, *new_source)
        print(f"added source {source_name!r} to {config_path}")

    if not proposed:
        print("nothing to write")
        return 0

    files = target_files(config_path) if config_path.exists() else [config_path]
    if len(files) > 1:
        print("Included checks files:")
        for i, f in enumerate(files, 1):
            print(f"  {i}. {f}")
        idx = int(_prompt("Write to which file (number)", "1")) - 1
        target = files[idx]
    else:
        target = files[0] if files else config_path

    append_checks(target, proposed)
    print(f"wrote {len(proposed)} check(s) to {target}")
    return 0


def _ui_command(args: argparse.Namespace) -> int:
    from dbfresh.config import load_config
    from dbfresh.tui.app import DbfreshApp

    config_path = Path(args.config)
    try:
        load_config(config_path)
    except ValueError as exc:
        return _report_config_error(exc)

    app = DbfreshApp(config_path=args.config, store_path=args.store)
    app.run()
    return 0


_CONFIG_READING_COMMANDS = frozenset({"run", "history", "prune", "add", "ui"})


def _load_dotenv_beside_config(config_path: Path) -> None:
    """Load a ``.env`` file next to ``config_path``, if one is present.

    ``${VAR}`` interpolation in config happens for every command that reads
    it, not only ``run``, so this runs once at the CLI dispatch boundary
    rather than being duplicated inside each command function. Real
    environment variables already set take precedence over ``.env``.
    """
    from dotenv import load_dotenv

    load_dotenv(config_path.parent / ".env")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in _CONFIG_READING_COMMANDS:
        _load_dotenv_beside_config(Path(args.config))
    if args.command == "run":
        return _run_command(args)
    if args.command == "history":
        return _history_command(args)
    if args.command == "prune":
        return _prune_command(args)
    if args.command == "add":
        return _add_command(args)
    if args.command == "ui":
        return _ui_command(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
