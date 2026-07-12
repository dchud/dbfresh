from dbfresh.adapters.base import Category, Column
from dbfresh.checks import diff_fingerprints, fingerprint_columns


def _col(name, type_, category=Category.OTHER, nullable=True):
    return Column(name=name, type=type_, nullable=nullable, category=category)


def test_fingerprint_stable_across_column_reorder():
    a = fingerprint_columns([_col("id", "INTEGER"), _col("name", "TEXT")])
    b = fingerprint_columns([_col("name", "TEXT"), _col("id", "INTEGER")])
    assert a == b


def test_fingerprint_ignores_nullability():
    a = fingerprint_columns([_col("id", "INTEGER", nullable=True)])
    b = fingerprint_columns([_col("id", "INTEGER", nullable=False)])
    assert a == b


def test_fingerprint_changes_on_added_column():
    a = fingerprint_columns([_col("id", "INTEGER")])
    b = fingerprint_columns([_col("id", "INTEGER"), _col("name", "TEXT")])
    assert a != b


def test_fingerprint_changes_on_removed_column():
    a = fingerprint_columns([_col("id", "INTEGER"), _col("name", "TEXT")])
    b = fingerprint_columns([_col("id", "INTEGER")])
    assert a != b


def test_fingerprint_changes_on_retyped_column():
    a = fingerprint_columns([_col("id", "INTEGER")])
    b = fingerprint_columns([_col("id", "BIGINT")])
    assert a != b


def test_diff_fingerprints_reports_added_removed_retyped():
    before = fingerprint_columns(
        [_col("id", "INTEGER"), _col("name", "TEXT"), _col("age", "INTEGER")]
    )
    after = fingerprint_columns(
        [_col("id", "BIGINT"), _col("name", "TEXT"), _col("email", "TEXT")]
    )
    diff = diff_fingerprints(after, before)
    assert "+ email (TEXT)" in diff
    assert "- age (INTEGER)" in diff
    assert "~ id (INTEGER -> BIGINT)" in diff


def test_diff_fingerprints_empty_when_identical():
    fp = fingerprint_columns([_col("id", "INTEGER")])
    assert diff_fingerprints(fp, fp) == []
