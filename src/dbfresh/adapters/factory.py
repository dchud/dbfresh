"""Map a source type to its adapter. Adding an engine = one entry here."""

from __future__ import annotations

import inspect
import sys
import tomllib
from pathlib import Path
from typing import Any

from dbfresh.adapters.base import Adapter, Dialect
from dbfresh.adapters.databricks import DatabricksAdapter, DatabricksDialect
from dbfresh.adapters.postgres import PostgresAdapter, PostgresDialect
from dbfresh.adapters.sqlite import SqliteAdapter, SqliteDialect
from dbfresh.adapters.sqlserver import SqlServerAdapter, TSqlDialect

_ADAPTERS: dict[str, type[Adapter]] = {
    "sqlite": SqliteAdapter,
    "postgres": PostgresAdapter,
    "sqlserver": SqlServerAdapter,
    "databricks": DatabricksAdapter,
}

_DIALECTS: dict[str, type[Dialect]] = {
    "sqlite": SqliteDialect,
    "postgres": PostgresDialect,
    "sqlserver": TSqlDialect,
    "databricks": DatabricksDialect,
}

# Source types whose driver is an optional extra, not a core dependency:
# type -> (import module, pip/uv extra name). Kept out of the base install
# on purpose -- neither pymssql's native FreeTDS build nor the Databricks
# connector should be forced on a sqlite-only user -- so when one is missing
# create_adapter rewords the raw "No module named ..." into a pointer at the
# extra to add.
_DRIVER_EXTRAS: dict[str, tuple[str, str]] = {
    "sqlserver": ("pymssql", "sqlserver"),
    "databricks": ("databricks", "databricks"),
}


class MissingDriverError(RuntimeError):
    """A source type's optional driver package isn't installed.

    Raised by :func:`create_adapter` when connecting a source whose driver
    -- ``pymssql`` for SQL Server, ``databricks-sql-connector`` for
    Databricks -- was never installed, carrying a message that names the
    extra to add rather than leaving the caller with a bare
    ``ModuleNotFoundError``.
    """


def supported_types() -> list[str]:
    """The source type strings the factory can build an adapter for,
    sorted -- what a UI offers as the full set of valid choices (e.g.
    ``dbfresh add``'s source-type prompt) rather than a hard-coded, easily
    stale copy of :data:`_ADAPTERS`'s keys.
    """
    return sorted(_ADAPTERS)


def adapter_class_for(type_: str) -> type[Adapter]:
    """The adapter class registered for ``type_``, without constructing it.

    Used by config validation to introspect a source's accepted
    constructor parameters (via ``inspect.signature``) before any
    connection is attempted.
    """
    try:
        return _ADAPTERS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None


def create_adapter(
    type_: str, params: dict[str, Any], timeout: int | None = None
) -> Adapter:
    """Construct the adapter for a source ``type`` from its config params.

    ``timeout`` (a source's connection timeout) is forwarded to the
    adapter's constructor only when it declares a ``timeout`` parameter --
    engines that don't support one simply never receive it, with no
    engine-name test here.
    """
    cls = adapter_class_for(type_)
    kwargs = dict(params)
    # inspect.signature(cls), not cls.__init__: the latter is unsound to
    # access off an instance/class reference typed as a Protocol subtype,
    # and the class form already yields the constructor signature minus
    # ``self``.
    if timeout is not None and "timeout" in inspect.signature(cls).parameters:
        kwargs["timeout"] = timeout
    try:
        return cls(**kwargs)
    except ModuleNotFoundError as exc:
        # The driver import is deferred to connect time (create_engine is
        # lazy), so a missing optional driver surfaces here rather than at
        # import. Reword it into an actionable hint; anything else (an
        # unrelated missing module) propagates unchanged.
        driver = _DRIVER_EXTRAS.get(type_)
        if driver is not None and (
            exc.name == driver[0]
            or (exc.name or "").startswith(driver[0] + ".")
        ):
            module, extra = driver
            raise MissingDriverError(
                missing_driver_message(type_, module, extra, Path(sys.prefix))
            ) from exc
        raise


def missing_driver_message(
    type_: str, module: str, extra: str, prefix: Path
) -> str:
    """The :class:`MissingDriverError` text for a driver absent from the
    Python environment rooted at ``prefix`` (``sys.prefix`` in practice).

    Names that environment, because the usual cause is running a different
    install than the one the extra went into -- a bare ``dbfresh`` on
    ``PATH`` resolving to a tool install while the checkout's ``.venv`` has
    the driver, or the reverse. The install command matches how that
    environment was made: a uv tool install (its root holds uv's
    ``uv-receipt.toml``) is reinstalled with the extra, and a checkout's
    project environment (a ``.venv`` beside dbfresh's own
    ``pyproject.toml``) is synced with it. Anything else gets a generic
    pointer at the extra.

    One line, since it is stored as a check's error text and shown in the
    digest and history rows.
    """
    message = (
        f"{type_!r} sources need the optional '{module}' driver, which is not "
        f"installed in the Python environment dbfresh is running from "
        f"({prefix})."
    )
    if (prefix / "uv-receipt.toml").is_file():
        return (
            f"{message} That is a uv tool install; reinstall it from the "
            f"checkout with every extra you use, including '{extra}', e.g. "
            'uv tool install --force -e ".[sqlserver,databricks]".'
        )
    if _is_dbfresh_checkout(prefix.parent):
        return (
            f"{message} That is the checkout's project environment; run "
            f"uv sync --all-extras there (a plain uv sync removes extras "
            f"again) and invoke dbfresh as uv run dbfresh."
        )
    return f"{message} Install dbfresh's '{extra}' extra into it."


def _is_dbfresh_checkout(directory: Path) -> bool:
    """Whether ``directory`` holds dbfresh's own ``pyproject.toml``."""
    try:
        with (directory / "pyproject.toml").open("rb") as f:
            project = tomllib.load(f).get("project", {})
    except OSError, tomllib.TOMLDecodeError:
        return False
    return project.get("name") == "dbfresh"


def dialect_for_type(type_: str) -> Dialect:
    """The dialect for a source ``type``, instantiated with no connection.

    Every shipped ``Dialect`` subclass takes no constructor arguments, so
    this is safe to call at config-validation time -- before any source is
    reachable, and even when an engine's optional driver isn't installed.
    """
    try:
        cls = _DIALECTS[type_]
    except KeyError:
        raise ValueError(f"unknown source type: {type_!r}") from None
    return cls()
