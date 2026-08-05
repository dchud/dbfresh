"""Helpers shared by more than one test module.

Only helpers with a single definition across the whole suite live here.
Where several modules define the same name with different bodies -- as
``_config``, ``_seed_db``, ``_seed``, ``_calendar``, and ``_result`` all
do -- those stay module-local on purpose: they are different fixtures
that happen to share a name, and hoisting one of them here would make
the others look like copies of it.

Plain functions rather than fixtures, so call sites read the same here as
they did when each module defined its own copy. conftest.py keeps the
fixtures (``pump_until``, ``seed_observations``, ...); this module keeps
the pure helpers.
"""

from __future__ import annotations

import subprocess

from dbfresh.adapters.sqlite import SqliteAdapter
from dbfresh.checks import Check, parse_expectation


def write_config(tmp_path, text):
    """Write ``text`` as ``config.yaml`` under ``tmp_path``.

    The common case for config-parsing tests, which only ever need one
    config at a known name. Use :func:`write_file` when the path itself
    matters -- an include target, a nested directory.
    """
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def write_file(path, text):
    """Write ``text`` to ``path``, creating parent directories.

    The counterpart to :func:`write_config` for tests that place files at
    specific paths -- include globs, per-directory configs, a ``.env``
    beside a config.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def isolate_git_config(tmp_path, monkeypatch):
    """Point git at empty per-test config files.

    Without this, a developer's own global/system git config leaks into
    tests that shell out to git, so results differ between machines.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-system"))


def git_init(repo_dir):
    """Initialize a quiet git repository in ``repo_dir``."""
    subprocess.run(["git", "init", "-q"], cwd=repo_dir, check=True)


def rows_adapter(n):
    """An in-memory SQLite adapter holding a table ``t`` of ``n`` rows."""
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (id INTEGER)")
    for i in range(n):
        a.rows(f"INSERT INTO t (id) VALUES ({i})")
    return a


def adapter_with_timestamp(value):
    """An in-memory SQLite adapter whose table ``t`` holds one
    ``created_at`` of ``value`` -- the freshness tests' fixture."""
    a = SqliteAdapter()
    a.rows("CREATE TABLE t (created_at TEXT)")
    a.rows(f"INSERT INTO t (created_at) VALUES ('{value}')")
    return a


def adapter_with_negatives():
    """An in-memory SQLite adapter whose ``fct`` table has two negative
    amounts among three rows -- the assertion tests' violating fixture."""
    a = SqliteAdapter()
    a.rows("CREATE TABLE fct (sale_id INTEGER, amount REAL)")
    a.rows("INSERT INTO fct VALUES (1, 10.0), (2, -5.0), (3, -1.0)")
    return a


def sqlite_table(db):
    """Create ``fct`` in ``db`` with one conventionally-named timestamp
    column, so timestamp picking is unambiguous."""
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, amount REAL, modified_at TIMESTAMP)"
    )
    adapter.close()


def ambiguous_sqlite_table(db):
    """Create ``fct`` in ``db`` with two equally plausible timestamp
    columns, so timestamp picking has to ask rather than guess."""
    adapter = SqliteAdapter(str(db))
    adapter.rows(
        "CREATE TABLE fct (id INTEGER PRIMARY KEY, created_at TIMESTAMP,"
        " updated_at TIMESTAMP)"
    )
    adapter.close()


def row_count_check():
    """The row_count check on source ``s``, object ``t`` -- the TUI tests'
    shared first check."""
    return Check(source="s", object="t", metric="row_count")


def null_rate_check():
    """The null_rate check on ``s``.``t``.``email`` -- the TUI tests'
    shared second check."""
    return Check(source="s", object="t", metric="null_rate", column="email")


def overall_glyph(table, row_key):
    """The plain text of ``row_key``'s ``overall`` cell in a status grid."""
    return table.get_cell(row_key, "overall").plain


def freshness_check(**overrides):
    """The freshness check on ``s``.``t``.``created_at`` with a 26h bound.

    ``overrides`` replaces any Check field, which is what lets the
    calendar and timezone suites vary one field without restating the
    rest. Callers wanting the plain check pass nothing.
    """
    return Check(
        source="s",
        object="t",
        metric="freshness",
        column="created_at",
        expect=parse_expectation({"max_lag": "26h"}),
        **overrides,
    )
