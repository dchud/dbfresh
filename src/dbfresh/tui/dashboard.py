"""Build the Home dashboard tree from config + the observation store.

Structure: source -> object, with the object's table-level
checks (no ``column``/``key``) as direct leaves under the object node, and
column/key-level checks nested under an intermediate node per column/key.
Each node's own status is the worst of its children (reusing
:func:`~dbfresh.engine.worst_status`); a leaf with no stored observation
renders as "unknown" rather than winning or losing against a real status.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.widgets import Tree

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.models import Status, worst_status
from dbfresh.store import Store

_STATUS_STYLE: dict[Status | None, str] = {
    Status.OK: "bold green",
    Status.WARN: "bold yellow",
    Status.FAIL: "bold red",
    Status.ERROR: "bold red",
    Status.SKIPPED: "dim",
    None: "dim",
}


@dataclass(frozen=True)
class NodeInfo:
    """Data attached to each dashboard tree node.

    ``kind`` is one of ``source``, ``object``, ``column``, or ``check``;
    only ``check`` nodes carry the ``check`` itself (used by History
    drill-down and by the Run action's status refresh).
    """

    kind: str
    check: Check | None = None


def check_label(check: Check) -> str:
    """The short label shown on a check's leaf node (and reused by History)."""
    if check.assert_ is not None:
        return f"assert {check.assert_}"
    if check.assert_sql is not None:
        return f"assert_sql {check.assert_sql}"
    return check.metric or "check"


def _group_key(check: Check) -> str | None:
    """The column/key name a check nests under, or ``None`` for table-level."""
    return check.column or check.key


def _latest_status(store: Store, check: Check) -> Status | None:
    observation = store.latest_observation(check_id(check))
    if observation is None:
        return None
    return Status(observation["status"])


def _status_label(name: str, status: Status | None) -> Text:
    word = status.value if status is not None else "unknown"
    text = Text(name)
    text.append(f" [{word}]", style=_STATUS_STYLE[status])
    return text


def _worst_or_unknown(statuses: list[Status]) -> Status | None:
    """The worst known status, or ``None`` when none of the children are known.

    A node whose only known children are ``SKIPPED`` rolls up to ``SKIPPED``
    rather than ``OK``, even though the two share severity rank 0 in
    :func:`~dbfresh.engine.worst_status` (which exit-code aggregation
    depends on). A mix of ``OK`` and ``SKIPPED`` still rolls up to ``OK``.
    """
    if not statuses:
        return None
    if all(status == Status.SKIPPED for status in statuses):
        return Status.SKIPPED
    return worst_status(statuses)


def build_dashboard(tree: Tree, config: Config, store: Store) -> None:
    """(Re)build ``tree`` from ``config.checks``, colored by ``store``.

    Safe to call repeatedly (e.g. after the Run action): clears any prior
    contents under the root before rebuilding.
    """
    tree.reset(tree.root.label)

    by_source: dict[str, dict[str, list[Check]]] = {}
    for check in config.checks:
        by_source.setdefault(check.source, {}).setdefault(check.object, []).append(
            check
        )

    for source_name in sorted(by_source):
        objects = by_source[source_name]
        source_node = tree.root.add(
            source_name, data=NodeInfo(kind="source"), expand=True
        )
        source_statuses: list[Status] = []

        for object_name in sorted(objects):
            checks = objects[object_name]
            object_node = source_node.add(
                object_name, data=NodeInfo(kind="object"), expand=True
            )
            object_statuses: list[Status] = []

            table_checks = [c for c in checks if _group_key(c) is None]
            for check in table_checks:
                status = _latest_status(store, check)
                if status is not None:
                    object_statuses.append(status)
                object_node.add_leaf(
                    _status_label(check_label(check), status),
                    data=NodeInfo(kind="check", check=check),
                )

            grouped: dict[str, list[Check]] = {}
            for check in checks:
                key = _group_key(check)
                if key is not None:
                    grouped.setdefault(key, []).append(check)

            for column_name in sorted(grouped):
                column_node = object_node.add(
                    column_name, data=NodeInfo(kind="column"), expand=True
                )
                column_statuses: list[Status] = []
                for check in grouped[column_name]:
                    status = _latest_status(store, check)
                    if status is not None:
                        column_statuses.append(status)
                    column_node.add_leaf(
                        _status_label(check_label(check), status),
                        data=NodeInfo(kind="check", check=check),
                    )
                column_status = _worst_or_unknown(column_statuses)
                column_node.set_label(_status_label(column_name, column_status))
                object_statuses.extend(column_statuses)

            object_status = _worst_or_unknown(object_statuses)
            object_node.set_label(_status_label(object_name, object_status))
            source_statuses.extend(object_statuses)

        source_status = _worst_or_unknown(source_statuses)
        source_node.set_label(_status_label(source_name, source_status))

    # The root itself carries no status (it is the tree's title, not a
    # check tier); expand it so sources are visible without an extra
    # keypress. Kept expanded across rebuilds since reset() preserves it.
    tree.root.expand()
