"""structlog configuration: the CLI's one-time logging setup.

Off by default: a normal run emits nothing below WARNING. The CLI calls
:func:`configure_logging` exactly once, right after parsing ``-v``/``-vv``,
so every ``structlog.get_logger()`` used elsewhere in the package (runner,
engine, ...) picks up the same level and renderer. Logs always go to
**stderr** -- stdout is reserved for the digest / ``--json`` report.

Not named ``logging.py``: that would shadow the stdlib module for every
``import logging`` elsewhere in the package.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Mapping

import structlog

DBFRESH_LOG_LEVEL = "DBFRESH_LOG_LEVEL"

_write_lock = threading.Lock()


class _StderrLogger:
    """A minimal structlog logger that resolves ``sys.stderr`` per write.

    ``structlog.PrintLoggerFactory(file=sys.stderr)`` would instead bind
    whatever object ``sys.stderr`` is at *configure* time. That is wrong
    here: :func:`configure_logging` can run more than once in a process
    (every ``dbfresh`` CLI invocation calls it once, and so does every test
    that drives the CLI through :func:`dbfresh.cli.main`), and a stream
    captured by one call -- e.g. pytest's ``capsys`` replacement -- can be
    closed by the time a *later*, unrelated log call fires against the
    same still-configured global structlog state. Looking ``sys.stderr``
    up fresh on every write avoids binding to a stream that may no longer
    be open, and still always means "wherever stderr currently points."
    """

    def msg(self, message: str) -> None:
        with _write_lock:
            print(message, file=sys.stderr, flush=True)

    log = debug = info = warn = warning = msg
    fatal = failure = err = error = critical = exception = msg


def _stderr_logger_factory(*_args: object) -> _StderrLogger:
    return _StderrLogger()


def verbosity_to_level(verbose: int) -> int:
    """Map a repeated ``-v`` count to a stdlib logging level.

    No ``-v``: WARNING (warnings and errors only). ``-v``: INFO. ``-vv`` or
    more: DEBUG -- verbosity beyond ``-vv`` is accepted but has no further
    effect.
    """
    if verbose <= 0:
        return logging.WARNING
    if verbose == 1:
        return logging.INFO
    return logging.DEBUG


def _level_from_name(name: str) -> int:
    """The stdlib level for a level name, or raise on an unknown one."""
    try:
        return logging.getLevelNamesMapping()[name.upper()]
    except KeyError:
        raise ValueError(f"invalid {DBFRESH_LOG_LEVEL}: {name!r}") from None


def configure_logging(
    verbose: int = 0, *, env: Mapping[str, str] | None = None
) -> None:
    """Configure structlog to render to stderr at the resolved level.

    ``verbose`` is the ``-v`` count from the CLI (0 = quiet). The
    ``DBFRESH_LOG_LEVEL`` environment variable (an explicit level name:
    DEBUG/INFO/WARNING/ERROR/CRITICAL), when set, overrides it -- e.g. to
    force DEBUG in a CI job without touching the CLI invocation. ``env``
    defaults to the real process environment; a test may pass its own
    mapping instead of monkeypatching ``os.environ``.
    """
    env = os.environ if env is None else env
    level = verbosity_to_level(verbose)
    override = env.get(DBFRESH_LOG_LEVEL)
    if override:
        level = _level_from_name(override)

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.KeyValueRenderer(key_order=["event"]),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=_stderr_logger_factory,
        cache_logger_on_first_use=False,
    )
