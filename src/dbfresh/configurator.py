"""Front-end-agnostic configurator: introspect, propose, emit YAML.

All proposal, validation, YAML-serialization, connection-test, and
existence-check logic lives here as plain functions and dataclasses, so both
`dbfresh add` (a thin interactive shell) and the future TUI Configure screen
share one tested surface. This module never writes to the observation
store; it only reads catalog metadata via an adapter's ``describe()`` and
emits YAML for the version-controlled config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbfresh.adapters.base import Category, Column, Dialect, ObjectInfo

_ROW_COUNT_MIN_RATIO = 0.5
_ROW_COUNT_MAX_RATIO = 2.0
_DEFAULT_MAX_LAG = "24h"

_CONVENTIONAL_TIMESTAMP_NAMES = frozenset(
    {"modified_at", "updated_at", "loaded_at", "load_ts", "created_at"}
)
_CONVENTIONAL_TIMESTAMP_SUFFIXES = ("_at", "_ts", "_date")


@dataclass(frozen=True)
class TimestampChoice:
    """Result of the freshness timestamp-column heuristic.

    ``column`` is set when a single unambiguous candidate was found.
    ``needs_choice`` is set instead when several temporal columns match and
    the wizard must ask rather than guess; ``candidates`` then lists them.
    """

    column: str | None = None
    needs_choice: bool = False
    candidates: list[str] = field(default_factory=list)


def _is_conventional_timestamp_name(name: str) -> bool:
    return name in _CONVENTIONAL_TIMESTAMP_NAMES or name.endswith(
        _CONVENTIONAL_TIMESTAMP_SUFFIXES
    )


def pick_timestamp_column(columns: list[Column]) -> TimestampChoice:
    """Auto-detect the freshness timestamp column among temporal columns.

    Prefers conventional names; if exactly one temporal column exists at
    all, uses it even when unconventionally named; otherwise several
    candidates match and the caller must ask the user to pick.
    """
    temporal = [c for c in columns if c.category == Category.TEMPORAL]
    if not temporal:
        return TimestampChoice()

    conventional = [c for c in temporal if _is_conventional_timestamp_name(c.name)]
    if len(conventional) == 1:
        return TimestampChoice(column=conventional[0].name)
    if len(temporal) == 1:
        return TimestampChoice(column=temporal[0].name)

    pool = conventional or temporal
    return TimestampChoice(needs_choice=True, candidates=[c.name for c in pool])


_CATEGORY_OFFERS: dict[Category, list[str]] = {
    Category.NUMERIC: ["null_rate", "sum", "avg", "min", "max", "duplicate_count"],
    Category.TEMPORAL: ["freshness", "null_rate"],
    Category.STRING: ["null_rate", "duplicate_count"],
    Category.BOOLEAN: ["null_rate"],
    Category.OTHER: ["null_rate"],
}


def category_offers(category: Category) -> list[str]:
    """Column-level checks offered for a category.

    The single source of truth for the docs applicability matrix and for
    the wizard's per-column offer listing; keys off ``category`` only,
    never a native type name.
    """
    return list(_CATEGORY_OFFERS[category])


def offered_column_checks(columns: list[Column]) -> list[dict]:
    """Per-column offer entries: category-appropriate checks, not preselected.

    ``null_rate`` is omitted for ``NOT NULL`` columns -- the engine already
    enforces them.
    """
    offers = []
    for column in columns:
        checks = [
            metric
            for metric in category_offers(column.category)
            if metric != "null_rate" or column.nullable
        ]
        offers.append(
            {"column": column.name, "category": column.category.value, "checks": checks}
        )
    return offers


def build_check(
    source: str,
    obj: str,
    metric: str,
    *,
    column: str | None = None,
    key: str | None = None,
    expect: dict,
    **extra: Any,
) -> dict:
    """Assemble one YAML-ready check block.

    The single builder used both by :func:`propose_checks` and by a wizard
    turning an offered column check (or a fully manual entry) into a block,
    so every emitted check has the same shape.
    """
    block: dict[str, Any] = {"source": source, "object": obj, "metric": metric}
    if column is not None:
        block["column"] = column
    if key is not None:
        block["key"] = key
    block.update(extra)
    block["expect"] = expect
    return block


def _row_count_baseline(has_calendar: bool) -> str:
    return "last_same_weekday" if has_calendar else "previous"


def propose_checks(
    source: str,
    obj: str,
    info: ObjectInfo,
    dialect: Dialect,
    has_calendar: bool = False,
    is_view: bool = False,
    timestamp_override: str | None = None,
) -> list[dict]:
    """The metadata-driven proposal bundle for a named source + object.

    Always proposes ``schema`` (unchanged) and a ``row_count`` volume-stability
    check. Proposes ``freshness`` on the auto-detected timestamp column
    (:func:`pick_timestamp_column`); when no column candidate exists, a
    Databricks-capable dialect on a table (not a view) falls back to
    ``describe_history``, otherwise no freshness check is proposed. When
    several temporal columns are ambiguous, :func:`pick_timestamp_column`
    returns no column and this proposes no freshness check unless the
    caller passes ``timestamp_override`` -- the column a front end asked
    the user to pick among ``TimestampChoice.candidates`` -- which is used
    as-is, bypassing the auto-detect heuristic entirely. Proposes one
    ``duplicate_count`` check per single-column key in ``info.keys``
    (composite keys are out of scope).
    """
    checks: list[dict] = [
        build_check(source, obj, "schema", expect={"unchanged": True}),
        build_check(
            source,
            obj,
            "row_count",
            expect={
                "vs_previous": {
                    "baseline": _row_count_baseline(has_calendar),
                    "min_ratio": _ROW_COUNT_MIN_RATIO,
                    "max_ratio": _ROW_COUNT_MAX_RATIO,
                }
            },
        ),
    ]

    if timestamp_override is not None:
        checks.append(
            build_check(
                source,
                obj,
                "freshness",
                column=timestamp_override,
                freshness_source="column",
                expect={"max_lag": _DEFAULT_MAX_LAG},
            )
        )
    else:
        timestamp = pick_timestamp_column(info.columns)
        if timestamp.column is not None:
            checks.append(
                build_check(
                    source,
                    obj,
                    "freshness",
                    column=timestamp.column,
                    freshness_source="column",
                    expect={"max_lag": _DEFAULT_MAX_LAG},
                )
            )
        elif (
            not timestamp.needs_choice
            and not is_view
            and "describe_history" in dialect.freshness_sources
        ):
            checks.append(
                build_check(
                    source,
                    obj,
                    "freshness",
                    freshness_source="describe_history",
                    expect={"max_lag": _DEFAULT_MAX_LAG},
                )
            )

    for key in info.keys or []:
        if len(key) == 1:
            checks.append(
                build_check(
                    source, obj, "duplicate_count", key=key[0], expect={"max": 0}
                )
            )

    return checks


@dataclass(frozen=True)
class ConnectionProbe:
    """Result of a mandatory connection test for a new source."""

    ok: bool
    error: str | None = None


def probe_connection(type_: str, params: dict) -> ConnectionProbe:
    """Build the adapter and run a trivial query to confirm it connects.

    Mandatory before writing a block for a brand-new source; never
    raises -- any failure (unknown type, bad credentials, unreachable host)
    comes back as ``ConnectionProbe(ok=False, error=...)``.
    """
    from dbfresh.adapters.factory import create_adapter

    try:
        adapter = create_adapter(type_, params)
    except Exception as exc:
        return ConnectionProbe(ok=False, error=str(exc))
    try:
        adapter.scalar("SELECT 1")
    except Exception as exc:
        return ConnectionProbe(ok=False, error=str(exc))
    finally:
        adapter.close()
    return ConnectionProbe(ok=True)


def probe_new_source(type_: str, raw_params: dict) -> tuple[ConnectionProbe, dict]:
    """Probe a brand-new source's params after resolving ``${VAR}`` tokens.

    ``raw_params`` is exactly what will be written to the YAML -- it may
    hold ``${VAR}`` secrets. The connection test itself must run against
    the resolved value (never a literal ``${VAR}`` string), so this
    returns ``(probe, resolved_params)``: use ``resolved_params`` to build
    a live adapter for further use (e.g. ``describe()``) when
    ``probe.ok``, but never write it -- the caller writes ``raw_params``
    verbatim via :func:`add_source` so the tracked config keeps ``${VAR}``
    rather than a literal secret. An undefined variable fails the probe
    cleanly rather than raising.
    """
    from dbfresh.config import interpolate_env

    try:
        resolved = interpolate_env(raw_params)
    except ValueError as exc:
        return ConnectionProbe(ok=False, error=str(exc)), raw_params
    return probe_connection(type_, resolved), resolved


@dataclass(frozen=True)
class ExistenceCheck:
    """Result of existence-checking a named object via ``describe()``.

    ``verified`` is ``False`` only when the source itself could not be
    reached (the caller passes ``adapter=None``), in which case ``exists``
    is ``None`` -- degraded manual entry, not a false negative. When
    ``verified`` is ``True``, ``exists`` reports whether ``describe()``
    succeeded, and ``info`` carries its result.
    """

    verified: bool
    exists: bool | None
    info: ObjectInfo | None = None
    error: str | None = None


def check_object_exists(adapter: Any | None, object_name: str) -> ExistenceCheck:
    """Existence-check ``object_name`` on ``adapter`` via ``describe()``.

    ``adapter`` is ``None`` when an already-configured source was found
    unreachable; the wizard degrades to manual entry and existence stays
    unverified rather than being reported as missing.
    """
    if adapter is None:
        return ExistenceCheck(verified=False, exists=None)
    try:
        info = adapter.describe(object_name)
    except Exception as exc:
        return ExistenceCheck(verified=True, exists=False, error=str(exc))
    return ExistenceCheck(verified=True, exists=True, info=info)


def target_files(config_path: str | Path) -> list[Path]:
    """Files eligible to receive new checks.

    When the root config declares ``include:``, the wizard asks which
    included checks file receives the new block: this returns the resolved
    matches (lexicographic order, matching load order). Without
    ``include:``, the only target is the root config itself.
    """
    config_path = Path(config_path)
    data = yaml.safe_load(config_path.read_text()) or {}
    patterns = data.get("include")
    if not patterns:
        return [config_path]
    config_dir = config_path.resolve().parent
    matched: set[Path] = set()
    for pattern in patterns:
        matched.update(p for p in config_dir.glob(pattern) if p.is_file())
    return sorted(matched, key=lambda p: p.as_posix())


def _find_top_level_key(lines: list[str], key: str) -> int | None:
    """The line index of a column-0 ``key:`` line, or ``None`` if absent."""
    pattern = re.compile(rf"^{re.escape(key)}:(.*)$")
    for i, line in enumerate(lines):
        if pattern.match(line):
            return i
    return None


def _block_end(lines: list[str], start: int) -> int:
    """Index just past the last line of the block ``lines[start]`` opens.

    A block continues through blank lines, comment-only lines, and any
    line indented past column 0; it ends at the next column-0, non-blank,
    non-comment line (the next top-level key), or at EOF.
    """
    j = start + 1
    while j < len(lines):
        line = lines[j]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#") or line[:1] in (" ", "\t"):
            j += 1
            continue
        break
    return j


def _block_indent(lines: list[str], start: int, end: int, default: int) -> int:
    """The indent already used by the first real item in a block, if any."""
    for line in lines[start + 1 : end]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return len(line) - len(line.lstrip(" "))
    return default


def _reindent(rendered: str, indent: int) -> str:
    """Prefix every non-blank line of ``rendered`` (0-indented) by ``indent``."""
    pad = " " * indent
    lines = rendered.splitlines(keepends=True)
    return "".join(pad + line if line.strip() else line for line in lines)


def _append_into_block(
    text: str, key: str, rendered_items: str, *, default_indent: int = 0
) -> str | None:
    """Splice ``rendered_items`` onto the tail of a top-level ``key:`` block.

    Preserves every other line of ``text`` verbatim -- comments included --
    by editing around the existing block rather than reparsing and
    re-dumping the whole document. ``rendered_items`` is 0-indented YAML
    (as :func:`yaml.safe_dump` renders it) and gets re-indented to match
    whatever indent the block's existing items already use, or
    ``default_indent`` when the block is empty or ``key:`` is missing
    entirely (then it's added at EOF). ``default_indent`` is ``0`` for a
    sequence value (``yaml.safe_dump``'s own indentless convention) but
    must be positive for a mapping value, whose nested keys can never
    share the parent key's column.

    Returns ``None`` for a shape this doesn't handle (e.g. a single-line
    flow value with existing items, such as ``key: [a, b]``) so the
    caller can fall back to a full round trip instead of writing
    something broken.
    """
    lines = text.splitlines(keepends=True)
    idx = _find_top_level_key(lines, key)

    if idx is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{key}:\n")
        lines.append(_reindent(rendered_items, default_indent))
        return "".join(lines)

    end = _block_end(lines, idx)
    inline = lines[idx].split(":", 1)[1].strip()
    if inline not in ("", "[]", "{}"):
        return None  # e.g. a flow-style value with existing items

    indent = _block_indent(lines, idx, end, default=default_indent)
    if inline in ("[]", "{}"):
        lines[idx] = f"{key}:\n"
    insertion = _reindent(rendered_items, indent)
    return "".join(lines[:end]) + insertion + "".join(lines[end:])


def add_source(config_path: str | Path, name: str, type_: str, params: dict) -> None:
    """Write a new source definition into the root config.

    ``sources:`` is declared only in the root config, never an included
    checks file, so this always targets ``config_path`` directly.
    Appends the rendered block onto the existing ``sources:`` mapping
    (see :func:`_append_into_block`) so comments and formatting elsewhere
    in the file survive; falls back to a full round trip only for a
    shape that can't be textually spliced.
    """
    config_path = Path(config_path)
    entry = {"type": type_, **params}

    if not config_path.exists() or not config_path.read_text().strip():
        raw = {"sources": {name: entry}, "checks": []}
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        return

    text = config_path.read_text()
    raw = yaml.safe_load(text) or {}
    rendered = yaml.safe_dump({name: entry}, sort_keys=False)
    spliced = _append_into_block(text, "sources", rendered, default_indent=2)
    if spliced is None:
        raw = dict(raw)
        sources = dict(raw.get("sources") or {})
        sources[name] = entry
        raw["sources"] = sources
        raw.setdefault("checks", [])
        config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        return
    if "checks" not in raw:
        if not spliced.endswith("\n"):
            spliced += "\n"
        spliced += "checks: []\n"
    config_path.write_text(spliced)


def _raw_checks_in(path: Path) -> list[dict]:
    """The raw check blocks in one config or included-checks file."""
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    return list(raw.get("checks") or [])


def _check_id_of(raw: dict) -> str:
    """The :func:`dbfresh.checks.check_id` a raw YAML check block derives to.

    Built from only the identity-bearing fields ``check_id`` hashes --
    never ``expect``, so this never has to parse (and possibly reject) an
    expectation just to compute an identity for dedup purposes.
    """
    from dbfresh.checks import Check, check_id

    check = Check(
        source=raw.get("source", ""),
        object=raw.get("object", ""),
        metric=raw.get("metric"),
        column=raw.get("column"),
        key=raw.get("key"),
        assert_=raw.get("assert"),
        id=raw.get("id"),
    )
    return check_id(check)


def append_checks(
    target_path: str | Path,
    new_checks: list[dict],
    *,
    config_path: str | Path | None = None,
) -> tuple[int, list[dict]]:
    """Append proposed check blocks to ``target_path``, skipping duplicates.

    ``target_path`` is either the root config (a mapping with ``sources:``,
    ``checks:``, etc. -- every other top-level key is preserved) or an
    included checks file (a bare list, or a ``{checks: [...]}`` mapping).
    Never writes to the observation store -- definitions stay in git.

    A proposed block whose derived ``check_id`` already exists is skipped
    rather than written -- two ``check_id``-colliding blocks in the
    composed config make the next :func:`dbfresh.config.load_config` raise,
    so this dedup runs before anything touches disk. ``config_path`` is the
    root config; when given, existing ids are gathered across every file
    :func:`target_files` resolves (the whole composed config), not just
    ``target_path`` -- a duplicate anywhere is caught, not only one already
    in the same file. Without ``config_path``, only ``target_path``'s own
    current contents are considered.

    Returns ``(written, skipped)``: how many blocks were appended, and the
    list of proposed blocks skipped as duplicates (for the caller to warn
    about).
    """
    target_path = Path(target_path)
    existing_files = (
        target_files(config_path) if config_path is not None else [target_path]
    )
    existing_ids = {
        _check_id_of(raw) for f in existing_files for raw in _raw_checks_in(f)
    }

    to_write: list[dict] = []
    skipped: list[dict] = []
    for block in new_checks:
        cid = _check_id_of(block)
        if cid in existing_ids:
            skipped.append(block)
            continue
        existing_ids.add(cid)
        to_write.append(block)

    if to_write:
        _write_new_checks(target_path, to_write)

    return len(to_write), skipped


def _write_new_checks(target_path: Path, blocks: list[dict]) -> None:
    """Write ``blocks`` to ``target_path``, splicing onto the existing text
    (see :func:`_append_into_block`) so comments and formatting elsewhere
    in the file survive. Falls back to a full round trip for a missing or
    empty file (nothing to preserve) or an unsplice-able shape.
    """
    if not target_path.exists() or not target_path.read_text().strip():
        target_path.write_text(yaml.safe_dump({"checks": blocks}, sort_keys=False))
        return

    text = target_path.read_text()
    raw = yaml.safe_load(text)
    rendered = yaml.safe_dump(blocks, sort_keys=False)

    if isinstance(raw, list):
        if not text.endswith("\n"):
            text += "\n"
        target_path.write_text(text + rendered)
        return

    spliced = _append_into_block(text, "checks", rendered)
    if spliced is None:
        raw = dict(raw)
        raw["checks"] = list(raw.get("checks") or []) + blocks
        target_path.write_text(yaml.safe_dump(raw, sort_keys=False))
        return
    target_path.write_text(spliced)
