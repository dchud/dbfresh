import pytest

from dbfresh.adapters.factory import create_adapter


def test_create_sqlite_adapter_works():
    adapter = create_adapter("sqlite", {"database": ":memory:"})
    assert adapter.scalar("SELECT 1") == 1
    adapter.close()


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        create_adapter("mystery", {})
