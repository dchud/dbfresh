from dbfresh.adapters.base import Dialect
from dbfresh.checks import Check, compile_metric_sql


def test_row_count():
    check = Check(source="w", object="dbo.fct_sales", metric="row_count")
    assert compile_metric_sql(check, Dialect()) == "SELECT COUNT(*) FROM dbo.fct_sales"


def test_row_count_with_where():
    check = Check(source="w", object="t", metric="row_count", where="active = 1")
    sql = compile_metric_sql(check, Dialect())
    assert sql == "SELECT COUNT(*) FROM t WHERE active = 1"
