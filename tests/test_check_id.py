import re

from dbfresh.checks import Check, check_id, parse_expectation

_HEX12 = re.compile(r"^[0-9a-f]{12}$")


def test_check_id_is_twelve_hex_chars():
    check = Check(source="s", object="t", metric="row_count")
    assert _HEX12.match(check_id(check))


def test_check_id_uses_explicit_id_verbatim():
    check = Check(source="s", object="t", metric="row_count", id="my-custom-id")
    assert check_id(check) == "my-custom-id"


def test_check_id_stable_across_expectation_edits():
    loose = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"between": [1, 10]}),
    )
    tight = Check(
        source="s",
        object="t",
        metric="row_count",
        expect=parse_expectation({"max": 5}),
    )
    assert check_id(loose) == check_id(tight)


def test_check_id_differs_by_column():
    a = Check(source="s", object="t", metric="null_rate", column="email")
    b = Check(source="s", object="t", metric="null_rate", column="phone")
    assert check_id(a) != check_id(b)


def test_check_id_differs_by_key():
    a = Check(source="s", object="t", metric="duplicate_count", key="id")
    b = Check(source="s", object="t", metric="duplicate_count", key="uuid")
    assert check_id(a) != check_id(b)


def test_check_id_ignores_column_for_row_count_and_schema():
    # column/key are irrelevant discriminators for table-level checks
    a = Check(source="s", object="t", metric="row_count")
    b = Check(source="s", object="t", metric="row_count")
    assert check_id(a) == check_id(b)


def test_check_id_differs_by_metric():
    a = Check(source="s", object="t", metric="row_count")
    b = Check(source="s", object="t", metric="schema")
    assert check_id(a) != check_id(b)


def test_check_id_differs_by_source_or_object():
    base = Check(source="s", object="t", metric="row_count")
    other_source = Check(source="other", object="t", metric="row_count")
    other_object = Check(source="s", object="u", metric="row_count")
    assert check_id(base) != check_id(other_source)
    assert check_id(base) != check_id(other_object)


def test_check_id_assertion_uses_normalized_text():
    a = Check(source="s", object="t", assert_="amount >= 0")
    b = Check(source="s", object="t", assert_="  amount   >=   0  ")
    assert check_id(a) == check_id(b)


def test_check_id_assertion_preserves_case():
    a = Check(source="s", object="t", assert_="amount >= 0")
    b = Check(source="s", object="t", assert_="Amount >= 0")
    assert check_id(a) != check_id(b)


def test_check_id_assertion_differs_from_metric_check_with_same_object():
    metric_check = Check(source="s", object="t", metric="row_count")
    assertion = Check(source="s", object="t", assert_="1 = 1")
    assert check_id(metric_check) != check_id(assertion)
