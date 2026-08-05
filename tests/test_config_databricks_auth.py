"""databricks auth-param coherence, validated by ``load_config``.

``auth_type`` selects the method: ``pat`` (the default) needs ``token``;
``oauth_m2m`` needs a service principal's ``client_id`` and
``client_secret``. Name-based signature validation (see
``test_config_sources.py``) accepts these parameter names once the
adapter's constructor is widened -- it can't catch a contradictory or
half-specified combination, which is what this file covers.
"""

import pytest
from helpers import write_config

from dbfresh.config import ConfigError, load_config


def _databricks_config(tmp_path, **params):
    """A one-source databricks config whose auth params are ``params``."""
    auth = "".join(f"    {key}: {value}\n" for key, value in params.items())
    return write_config(
        tmp_path,
        f"""
sources:
  s:
    type: databricks
    host: h
    http_path: p
{auth}checks: []
""",
    )


@pytest.mark.parametrize(
    ("params", "expected", "absent"),
    [
        pytest.param(
            {
                "auth_type": "oauth_m2m",
                "client_id": "cid",
                "client_secret": "csec",
            },
            {"auth_type": "oauth_m2m"},
            ("token",),
            id="oauth_m2m-with-both-service-principal-creds",
        ),
        pytest.param(
            {"token": "t"},
            {"token": "t"},
            (),
            id="token-with-no-auth-type",
        ),
        pytest.param(
            {"auth_type": "pat", "token": "t"},
            {"auth_type": "pat"},
            (),
            id="explicit-pat-with-token",
        ),
    ],
)
def test_coherent_auth_params_load(tmp_path, params, expected, absent):
    loaded = load_config(_databricks_config(tmp_path, **params), env={})
    source_params = loaded.sources["s"].params
    for key, value in expected.items():
        assert source_params[key] == value
    for key in absent:
        assert key not in source_params


@pytest.mark.parametrize(
    ("params", "message"),
    [
        pytest.param(
            {"auth_type": "oauth_m2m", "client_id": "cid"},
            "requires both client_id and client_secret",
            id="oauth_m2m-missing-client-secret",
        ),
        pytest.param(
            {"auth_type": "oauth_m2m", "client_secret": "csec"},
            "requires both client_id and client_secret",
            id="oauth_m2m-missing-client-id",
        ),
        pytest.param(
            {
                "auth_type": "oauth_m2m",
                "client_id": "cid",
                "client_secret": "csec",
                "token": "t",
            },
            "does not use token",
            id="oauth_m2m-with-a-token-as-well",
        ),
        pytest.param(
            {"token": "t", "client_id": "cid"},
            "require auth_type: oauth_m2m",
            id="pat-with-client-id",
        ),
        pytest.param(
            {"token": "t", "client_secret": "csec"},
            "require auth_type: oauth_m2m",
            id="pat-with-client-secret",
        ),
        pytest.param(
            {},
            "needs token",
            id="neither-token-nor-service-principal-creds",
        ),
        pytest.param(
            {"auth_type": "bogus", "token": "t"},
            "auth_type must be 'pat' or 'oauth_m2m'",
            id="unrecognized-auth-type",
        ),
    ],
)
def test_incoherent_auth_params_are_a_config_error(tmp_path, params, message):
    with pytest.raises(ConfigError, match=message):
        load_config(_databricks_config(tmp_path, **params), env={})
