"""Tests for the registry-derived reference-page generator (§14, §15).

Covers the "docs lockstep" testing requirement (§15): every registered
metric and operator appears in the generated pages, and regenerating after a
registry change actually changes the output -- proving the pages are
rendered live from the registries, not hand-copied, so forgetting to run
`just docs` after a registry edit leaves genuinely stale content behind.
"""

from __future__ import annotations

from dataclasses import replace

from dbfresh import docsgen, registry
from dbfresh.adapters.base import Category
from dbfresh.cli import build_parser
from dbfresh.configurator import category_offers
from dbfresh.engine import Status, exit_code


def test_every_registered_metric_appears_in_generated_metrics_page():
    content = docsgen.render_metrics()
    for spec in registry.METRICS:
        assert f"`{spec.name}`" in content
        assert spec.description in content


def test_every_registered_operator_appears_in_generated_operators_page():
    content = docsgen.render_operators()
    for spec in registry.OPERATORS:
        assert f"`{spec.operator}`" in content
        assert spec.meaning in content


def test_matrix_page_matches_category_offers_exactly():
    content = docsgen.render_matrix()
    for category in Category:
        offered = category_offers(category)
        lines = [line for line in content.splitlines() if line.startswith("| `")]
        (row,) = [line for line in lines if line.startswith(f"| `{category.value}`")]
        cells = [cell.strip() for cell in row.strip("|").split("|")][1:]
        columns = docsgen._matrix_columns()
        marked = {name for name, cell in zip(columns, cells, strict=True) if cell}
        assert marked == set(offered)


def test_cli_page_lists_every_subcommand_and_exit_code():
    content = docsgen.render_cli()
    parser = build_parser()
    sub_action = docsgen._subparsers_action(parser)
    for name in sub_action.choices:
        assert f"`dbfresh {name}`" in content
    for status in Status:
        assert str(exit_code(status)) in content
        assert status.value in content


def test_write_all_creates_the_four_generated_pages(tmp_path):
    pages = docsgen.write_all(tmp_path)
    assert set(pages) == {"metrics.md", "operators.md", "matrix.md", "cli.md"}
    for name in pages:
        assert (tmp_path / name).read_text() == pages[name]


def test_main_writes_to_a_custom_output_dir(tmp_path, capsys):
    exit_status = docsgen.main(["--output-dir", str(tmp_path)])
    assert exit_status == 0
    assert (tmp_path / "metrics.md").exists()
    assert "4 generated reference page(s)" in capsys.readouterr().out


def test_regenerating_after_a_metric_registry_change_updates_the_page():
    """A registry edit not followed by regeneration leaves stale content;
    regenerating must pick up the change, proving the page is rendered
    live from the registry rather than duplicated by hand."""
    before = docsgen.render_metrics()
    assert "made_up_metric" not in before

    extra = replace(
        registry.METRICS[0],
        name="made_up_metric",
        description="a metric added only for this test",
    )
    patched = (*registry.METRICS, extra)
    original = registry.METRICS
    try:
        registry.METRICS = patched
        after = docsgen.render_metrics()
    finally:
        registry.METRICS = original

    assert "made_up_metric" in after
    assert after != before


def test_regenerating_after_an_operator_registry_change_updates_the_page():
    before = docsgen.render_operators()
    assert "made_up_operator" not in before

    extra = replace(
        registry.OPERATORS[0],
        operator="made_up_operator",
        meaning="an operator added only for this test",
    )
    patched = (*registry.OPERATORS, extra)
    original = registry.OPERATORS
    try:
        registry.OPERATORS = patched
        after = docsgen.render_operators()
    finally:
        registry.OPERATORS = original

    assert "made_up_operator" in after
    assert after != before
