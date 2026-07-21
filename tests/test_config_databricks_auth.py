"""databricks auth-param coherence, validated by ``load_config``.

``auth_type`` selects the method: ``pat`` (the default) needs ``token``;
``oauth_m2m`` needs a service principal's ``client_id`` and
``client_secret``. Name-based signature validation (see
``test_config_sources.py``) accepts these parameter names once the
adapter's constructor is widened -- it can't catch a contradictory or
half-specified combination, which is what this file covers.
"""

import pytest

from dbfresh.config import ConfigError, load_config


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


def test_oauth_m2m_with_client_id_and_secret_loads(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: oauth_m2m
    client_id: cid
    client_secret: csec
checks: []
""",
    )
    cfg = load_config(path, env={})
    assert cfg.sources["s"].params["auth_type"] == "oauth_m2m"
    assert "token" not in cfg.sources["s"].params


def test_plain_token_with_no_auth_type_loads(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: databricks, host: h, http_path: p, token: t }
checks: []
""",
    )
    cfg = load_config(path, env={})
    assert cfg.sources["s"].params["token"] == "t"


def test_explicit_pat_auth_type_with_token_loads(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: pat
    token: t
checks: []
""",
    )
    cfg = load_config(path, env={})
    assert cfg.sources["s"].params["auth_type"] == "pat"


def test_oauth_m2m_missing_client_secret_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: oauth_m2m
    client_id: cid
checks: []
""",
    )
    with pytest.raises(
        ConfigError, match="requires both client_id and client_secret"
    ):
        load_config(path, env={})


def test_oauth_m2m_missing_client_id_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: oauth_m2m
    client_secret: csec
checks: []
""",
    )
    with pytest.raises(
        ConfigError, match="requires both client_id and client_secret"
    ):
        load_config(path, env={})


def test_oauth_m2m_with_a_token_also_set_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: oauth_m2m
    client_id: cid
    client_secret: csec
    token: t
checks: []
""",
    )
    with pytest.raises(ConfigError, match="does not use token"):
        load_config(path, env={})


def test_pat_with_client_id_set_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    token: t
    client_id: cid
checks: []
""",
    )
    with pytest.raises(ConfigError, match="require auth_type: oauth_m2m"):
        load_config(path, env={})


def test_pat_with_client_secret_set_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    token: t
    client_secret: csec
checks: []
""",
    )
    with pytest.raises(ConfigError, match="require auth_type: oauth_m2m"):
        load_config(path, env={})


def test_databricks_source_with_neither_token_nor_sp_creds_is_a_config_error(
    tmp_path,
):
    path = _write(
        tmp_path,
        """
sources:
  s: { type: databricks, host: h, http_path: p }
checks: []
""",
    )
    with pytest.raises(ConfigError, match="needs token"):
        load_config(path, env={})


def test_invalid_auth_type_is_a_config_error(tmp_path):
    path = _write(
        tmp_path,
        """
sources:
  s:
    type: databricks
    host: h
    http_path: p
    auth_type: bogus
    token: t
checks: []
""",
    )
    with pytest.raises(
        ConfigError, match="auth_type must be 'pat' or 'oauth_m2m'"
    ):
        load_config(path, env={})
