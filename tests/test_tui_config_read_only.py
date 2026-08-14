"""Pins the read-only decision: the TUI never writes config.

Each configurator function that mutates a config file on disk is checked
here by static import scan -- a cheap, structural guard that catches a
write path creeping back into src/dbfresh/tui/ before it ships, rather than
relying on every future change remembering the rule by convention.
"""

from __future__ import annotations

import ast
from pathlib import Path

_TUI_DIR = Path(__file__).resolve().parent.parent / "src" / "dbfresh" / "tui"

# The configurator functions that write to a config file on disk. Reading
# (propose_checks, check_object_exists, partition_new_checks, ...) is fine
# and expected; only these mutate.
_FORBIDDEN_CONFIGURATOR_WRITE_NAMES = frozenset(
    {
        "add_source",
        "append_checks",
        "rewrite_check_expectation",
        "remove_check",
        "rewrite_source",
        "remove_source",
        "find_check_file",
        "target_files",
        "raw_source",
    }
)


def _imported_configurator_names(path: Path) -> set[str]:
    """Every name this module imports from ``dbfresh.configurator``,
    however it spells the import (``from ... import x``, `as` aliases, or
    a plain ``import dbfresh.configurator`` module-level import -- the
    latter is flagged in full since a module-level import gives access to
    every attribute on it, write functions included).
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in (
            "dbfresh.configurator",
            "configurator",
        ):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "dbfresh.configurator":
                    names.add("dbfresh.configurator")
    return names


def test_no_tui_module_imports_a_configurator_write_function():
    """No module under src/dbfresh/tui/ may import add_source,
    append_checks, rewrite_check_expectation, remove_check, rewrite_source,
    remove_source, find_check_file, target_files, or raw_source from
    dbfresh.configurator -- the TUI reads and proposes, it never writes
    config. A failure here means a write path crept back into the TUI;
    remove that import (and whatever call site pulled it in) rather than
    special-casing this test.
    """
    violations: dict[str, set[str]] = {}
    for path in sorted(_TUI_DIR.rglob("*.py")):
        imported = _imported_configurator_names(path)
        if imported == {"dbfresh.configurator"}:
            # A bare module import: flagged as its own violation category
            # below rather than matched against the write-name set, since
            # it grants access to everything on the module, write
            # functions included.
            violations.setdefault(str(path), set()).add(
                "dbfresh.configurator (whole-module import)"
            )
            continue
        offending = imported & _FORBIDDEN_CONFIGURATOR_WRITE_NAMES
        if offending:
            violations.setdefault(str(path), set()).update(offending)

    assert not violations, (
        "the TUI must never write config, but the following modules "
        "import a configurator write function:\n"
        + "\n".join(
            f"  {path}: {', '.join(sorted(names))}"
            for path, names in sorted(violations.items())
        )
    )


def test_tui_dir_is_not_empty():
    """Guards the scan itself: an empty match set (e.g. a moved/renamed
    tui/ directory) would make the test above pass vacuously."""
    assert list(_TUI_DIR.rglob("*.py"))
