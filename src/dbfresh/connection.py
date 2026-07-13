"""Source config to connection-parameter parsing.

An engine-specific adapter turns its source config into the arguments its
driver needs. For SQL Server, that config is a single usql-style connection
URL kept in an env var (secrets never live in the checked-in YAML); this
module is the one place that URL is parsed and disambiguated.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlsplit

_SQLSERVER_SCHEMES = frozenset({"sqlserver", "mssql", "ms"})
_SQLSERVER_DEFAULT_PORT = 1433


@dataclass(frozen=True)
class SqlServerConnectionParams:
    """Resolved connection parameters for an ``mssql+pymssql://`` engine.

    ``server`` is the plain host, or ``host\\instance`` for a named
    instance. A named instance carries ``port=None``: SQL Server resolves
    the instance's port dynamically via the SQL Browser service rather than
    a fixed port.
    """

    server: str
    port: int | None
    database: str
    user: str
    password: str


def parse_sqlserver_url(url: str) -> SqlServerConnectionParams:
    """Parse a usql-style SQL Server URL into resolved connection params.

    Format: ``sqlserver://user:pass@host:port/PATH?param=value`` (the
    scheme aliases ``mssql`` and ``ms`` are also accepted). The path
    segment is disambiguated two ways:

    - if ``?database=`` is present, it names the database and the path
      segment is a named **instance** (native-style);
    - otherwise the path segment **is** the database (dburl-style).

    ``user``, ``password``, and the database (whether from the path
    segment or ``?database=``) are all URL-decoded. The port defaults to
    1433 when omitted, except for a
    named instance, whose port is always omitted. A missing database or an
    unrecognized scheme raises ``ValueError``.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in _SQLSERVER_SCHEMES:
        raise ValueError(
            f"unrecognized SQL Server URL scheme: {parsed.scheme!r} "
            f"(expected one of {sorted(_SQLSERVER_SCHEMES)})"
        )
    if not parsed.hostname:
        raise ValueError(f"SQL Server URL is missing a host: {url!r}")

    query = parse_qs(parsed.query)
    path_segment = unquote(parsed.path.lstrip("/"))

    if "database" in query:
        database = query["database"][0]
        instance = path_segment or None
    else:
        database = path_segment
        instance = None

    if not database:
        raise ValueError(f"SQL Server URL is missing a database: {url!r}")

    if instance:
        server = f"{parsed.hostname}\\{instance}"
        port = None
    else:
        server = parsed.hostname
        port = parsed.port or _SQLSERVER_DEFAULT_PORT

    return SqlServerConnectionParams(
        server=server,
        port=port,
        database=database,
        user=unquote(parsed.username) if parsed.username else "",
        password=unquote(parsed.password) if parsed.password else "",
    )
