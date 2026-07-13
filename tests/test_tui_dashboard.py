from pathlib import Path

from textual.widgets import Tree

from dbfresh.checks import Check, check_id
from dbfresh.config import Config
from dbfresh.engine import Result, Status
from dbfresh.store import Store
from dbfresh.tui.dashboard import build_dashboard


def _checks():
    return [
        Check(source="s", object="orders", metric="row_count"),
        Check(source="s", object="orders", metric="schema"),
        Check(source="s", object="orders", metric="null_rate", column="email"),
        Check(source="s", object="orders", metric="freshness", column="modified_at"),
        Check(source="t", object="items", metric="duplicate_count", key="sku"),
    ]


def _config(checks):
    return Config(sources={}, checks=checks, config_dir=Path("."))


def _seed_observation(store, check, status, value=None):
    run_id = store.start_run()
    result = Result(
        object=check.object,
        metric=check.metric,
        status=status,
        source=check.source,
        value=value,
        check_id=check_id(check),
    )
    store.record_observation(run_id, result)
    store.finish_run(run_id, status)


def _find_child(node, name):
    for child in node.children:
        if str(child.label).split(" ")[0] == name:
            return child
    raise AssertionError(f"no child named {name!r} among {list(node.children)}")


def test_build_dashboard_groups_by_source_then_object():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    source_names = sorted(str(n.label).split(" ")[0] for n in tree.root.children)
    assert source_names == ["s", "t"]


def test_table_level_checks_are_direct_children_of_object_node():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    leaf_names = {str(c.label).split(" ")[0] for c in orders_node.children}
    # row_count and schema (table-level, no column/key) are direct leaves;
    # email and modified_at (column-level) are nested column nodes instead.
    assert {"row_count", "schema"} <= leaf_names
    assert "email" in leaf_names
    assert "modified_at" in leaf_names


def test_column_level_checks_nest_under_a_column_node():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    email_node = _find_child(orders_node, "email")
    metric_names = {str(c.label).split(" ")[0] for c in email_node.children}
    assert metric_names == {"null_rate"}


def test_key_level_checks_also_nest_under_a_column_node():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    t_node = _find_child(tree.root, "t")
    items_node = _find_child(t_node, "items")
    sku_node = _find_child(items_node, "sku")
    metric_names = {str(c.label).split(" ")[0] for c in sku_node.children}
    assert metric_names == {"duplicate_count"}


def test_check_with_no_observation_renders_unknown():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    row_count_leaf = _find_child(orders_node, "row_count")
    assert "unknown" in str(row_count_leaf.label)


def test_check_status_reflects_latest_observation():
    checks = _checks()
    store = Store(":memory:")
    _seed_observation(store, checks[0], Status.OK, value=3)  # row_count
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    row_count_leaf = _find_child(orders_node, "row_count")
    assert "OK" in str(row_count_leaf.label)


def test_column_node_status_is_worst_of_its_checks():
    checks = _checks()
    store = Store(":memory:")
    _seed_observation(store, checks[2], Status.FAIL, value=0.5)  # email null_rate
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    email_node = _find_child(orders_node, "email")
    assert "FAIL" in str(email_node.label)


def test_column_node_rolls_up_to_skipped_when_all_checks_are_skipped():
    checks = _checks()
    store = Store(":memory:")
    _seed_observation(store, checks[2], Status.SKIPPED)  # email null_rate
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    email_node = _find_child(orders_node, "email")
    assert "SKIPPED" in str(email_node.label)
    assert "OK" not in str(email_node.label)


def test_column_node_rolls_up_to_ok_when_ok_and_skipped_are_mixed():
    checks = [
        Check(source="s", object="orders", metric="null_rate", column="email"),
        Check(source="s", object="orders", metric="sum", column="email"),
    ]
    store = Store(":memory:")
    _seed_observation(store, checks[0], Status.OK, value=0.01)
    _seed_observation(store, checks[1], Status.SKIPPED)
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    email_node = _find_child(orders_node, "email")
    assert "OK" in str(email_node.label)


def test_object_and_source_node_status_is_worst_of_their_checks():
    checks = _checks()
    store = Store(":memory:")
    _seed_observation(store, checks[0], Status.OK, value=3)
    _seed_observation(store, checks[2], Status.FAIL, value=0.9)  # email null_rate
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    s_node = _find_child(tree.root, "s")
    orders_node = _find_child(s_node, "orders")
    assert "FAIL" in str(orders_node.label)
    assert "FAIL" in str(s_node.label)


def test_object_with_all_unknown_checks_renders_unknown():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)

    t_node = _find_child(tree.root, "t")
    items_node = _find_child(t_node, "items")
    assert "unknown" in str(items_node.label)


def test_rebuild_clears_previous_tree_contents():
    checks = _checks()
    store = Store(":memory:")
    tree = Tree("dbfresh")

    build_dashboard(tree, _config(checks), store)
    build_dashboard(tree, _config(checks), store)

    source_names = [str(n.label).split(" ")[0] for n in tree.root.children]
    assert sorted(source_names) == ["s", "t"]
