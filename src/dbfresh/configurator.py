"""Front-end-agnostic configurator: introspect, propose, emit YAML.

All proposal, validation, YAML-serialization, connection-test, and
existence-check logic lives here as plain functions and dataclasses, so both
`dbfresh add` (a thin interactive shell) and the TUI Configure screen share
one tested surface. This module never writes to the observation store; it
only reads catalog metadata via an adapter's ``describe()`` and emits YAML
for the version-controlled config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbfresh.adapters.base import Adapter, Category, Column, Dialect, ObjectInfo

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


def _proposed_metric_columns(proposed: list[dict]) -> set[tuple[str, str]]:
    """The ``(metric, column)`` pairs a proposed bundle already covers.

    Keyed the same way an offer entry names its own column: most metrics
    carry their column in ``column``, but ``duplicate_count``'s identity
    lives in ``key`` instead (see :func:`build_check` and
    :func:`build_offered_check`), so this reads ``key`` for that metric
    rather than missing the overlap entirely. A block with neither field
    (``schema``, ``row_count``, or a ``describe_history``-sourced
    ``freshness``) contributes nothing here.
    """
    pairs: set[tuple[str, str]] = set()
    for block in proposed:
        metric = block.get("metric")
        column = (
            block.get("key") if metric == "duplicate_count" else block.get("column")
        )
        if metric is not None and column is not None:
            pairs.add((metric, column))
    return pairs


def offered_column_checks(
    columns: list[Column], proposed: list[dict] | None = None
) -> list[dict]:
    """Per-column offer entries: category-appropriate checks, not preselected.

    ``null_rate`` is omitted for ``NOT NULL`` columns -- the engine already
    enforces them.

    ``proposed`` is the bundle :func:`propose_checks` already built for this
    object, if any. Any ``(metric, column)`` pair it already covers is
    excluded from the offer list rather than offered a second time: a
    ``check_id`` hashes source/object/metric/column but deliberately
    ignores ``expect`` (so tuning a threshold later doesn't fork history),
    which means an auto-proposed check and an offered one for the same
    metric and column collide on identity -- selecting both would silently
    drop one via :func:`append_checks`'s dedup instead of writing two
    checks. This affects more than ``freshness``: a single-column key that
    is also a ``numeric`` or ``string`` column gets a proposed
    ``duplicate_count``, which would otherwise be offered again for the
    same column too. Without ``proposed``, nothing is excluded.
    """
    already = _proposed_metric_columns(proposed or [])
    offers = []
    for column in columns:
        checks = [
            metric
            for metric in category_offers(column.category)
            if (metric != "null_rate" or column.nullable)
            and (metric, column.name) not in already
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


_DEFAULT_NULL_RATE_MAX = 0.05


def build_offered_check(
    source: str,
    obj: str,
    column: str,
    metric: str,
    has_calendar: bool,
    *,
    max_null_rate: float = _DEFAULT_NULL_RATE_MAX,
    max_lag: str = _DEFAULT_MAX_LAG,
) -> dict:
    """Turn one offered-checks pick (:func:`offered_column_checks`) into a
    YAML-ready block via :func:`build_check`, shared by both front ends so
    neither duplicates the volume-stability guards or the default max_lag
    :func:`propose_checks` already uses. ``max_null_rate`` and ``max_lag``
    only matter for their respective metrics; collecting them (e.g.
    interactively) is a front-end concern this module never performs
    itself.
    """
    if metric == "null_rate":
        return build_check(
            source, obj, "null_rate", column=column, expect={"max": max_null_rate}
        )
    if metric in ("sum", "avg", "min", "max"):
        guards = {
            "baseline": _row_count_baseline(has_calendar),
            "min_ratio": _ROW_COUNT_MIN_RATIO,
            "max_ratio": _ROW_COUNT_MAX_RATIO,
        }
        return build_check(
            source, obj, metric, column=column, expect={"vs_previous": guards}
        )
    if metric == "duplicate_count":
        return build_check(
            source, obj, "duplicate_count", key=column, expect={"max": 0}
        )
    if metric == "freshness":
        return build_check(
            source,
            obj,
            "freshness",
            column=column,
            freshness_source="column",
            expect={"max_lag": max_lag},
        )
    raise ValueError(f"unsupported offered metric: {metric!r}")


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


def key_introspection_note(dialect: Dialect, info: ObjectInfo) -> str | None:
    """Explain a missing ``duplicate_count`` proposal opportunity, when due.

    ``None`` when ``info.keys`` already has something to propose from, or
    when the dialect's ``introspection_capabilities`` declares ``"keys"`` --
    in that case an empty/``None`` ``info.keys`` means the object genuinely
    has no primary key or unique constraint, not that the engine has
    nothing to report. Otherwise the engine cannot introspect keys at all
    (e.g. Databricks/Unity Catalog), which is worth surfacing so the
    absence of a proposal doesn't read as "this object has no keys".
    """
    if info.keys:
        return None
    if "keys" in dialect.introspection_capabilities:
        return None
    return (
        f"note: the {dialect.name!r} dialect cannot introspect key/uniqueness "
        "metadata; add duplicate_count checks by hand if this object has a "
        "natural key"
    )


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


def check_object_exists(adapter: Adapter | None, object_name: str) -> ExistenceCheck:
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
    matches (lexicographic order, matching load order), via
    :func:`dbfresh.config.resolve_includes` -- the same resolver
    ``load_config`` uses, so an unmatched glob is a hard error here too,
    never a silently empty list. Without ``include:``, the only target is
    the root config itself.
    """
    from dbfresh.config import resolve_includes

    config_path = Path(config_path)
    data = yaml.safe_load(config_path.read_text()) or {}
    patterns = data.get("include")
    if not patterns:
        return [config_path]
    config_dir = config_path.resolve().parent
    return resolve_includes(config_dir, patterns)


def check_bearing_files(config_path: str | Path) -> list[Path]:
    """Every file that may hold check definitions for this config: the root
    config itself, plus any included checks files.

    Distinct from :func:`target_files`, which selects where *new* checks
    are written (only the included files, once ``include:`` is set). The
    consumers that must consider *all existing* checks -- :func:`append_checks`'s
    dedup scan, :func:`find_check_file`, and :func:`remove_source`'s orphan
    guard -- need this instead: a config may keep checks in the root config
    *and* in included files (``load_config`` composes both), and
    ``target_files`` drops the root once ``include:`` appears. Root first,
    then the included files, de-duplicated by resolved path.
    """
    config_path = Path(config_path)
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in (config_path, *target_files(config_path)):
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _read_config_text(path: Path) -> tuple[str, str]:
    """A config file's text with universal-newline translation, plus the
    newline style to restore when writing it back.

    Every splice in this module works on ``\\n``-terminated lines, so the
    editing logic never special-cases line endings. Pairing the text with
    the file's detected newline lets :func:`_write_config_text` put the
    original style back -- editing one check in a CRLF-terminated file must
    not rewrite every other line to LF, which would turn a one-line edit
    into a whole-file diff on repos that keep CRLF. A file mixing endings
    normalizes to its first-seen style; an empty or LF file is ``\\n``.
    """
    raw = path.read_bytes()
    if b"\r\n" in raw:
        newline = "\r\n"
    elif b"\r" in raw:
        newline = "\r"
    else:
        newline = "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return text, newline


def _write_config_text(path: Path, text: str, newline: str = "\n") -> None:
    """Write config ``text`` (``\\n``-terminated lines) to ``path``,
    translating each ``\\n`` to ``newline`` so the file keeps the ending
    style :func:`_read_config_text` detected. A freshly created file has no
    prior style, so it takes the ``\\n`` default and is written verbatim.
    """
    path.write_text(text, encoding="utf-8", newline=newline)


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
        # Annotated (not left to infer from this literal): `raw` is
        # reassigned below from `yaml.safe_load`, a differently-shaped
        # dict each time a config is re-read, so it needs one wide,
        # explicit type across the whole function rather than one pinned
        # to this branch's literal shape.
        raw: dict[str, Any] = {"sources": {name: entry}, "checks": []}
        _write_config_text(config_path, yaml.safe_dump(raw, sort_keys=False))
        return

    text, newline = _read_config_text(config_path)
    raw = yaml.safe_load(text) or {}
    rendered = yaml.safe_dump({name: entry}, sort_keys=False)
    spliced = _append_into_block(text, "sources", rendered, default_indent=2)
    if spliced is None:
        raw = dict(raw)
        sources = dict(raw.get("sources") or {})
        sources[name] = entry
        raw["sources"] = sources
        raw.setdefault("checks", [])
        _write_config_text(config_path, yaml.safe_dump(raw, sort_keys=False), newline)
        return
    if "checks" not in raw:
        if not spliced.endswith("\n"):
            spliced += "\n"
        spliced += "checks: []\n"
    _write_config_text(config_path, spliced, newline)


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
        assert_sql=raw.get("assert_sql"),
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
    :func:`check_bearing_files` resolves (the whole composed config), not just
    ``target_path`` -- a duplicate anywhere is caught, not only one already
    in the same file. Without ``config_path``, only ``target_path``'s own
    current contents are considered.

    Returns ``(written, skipped)``: how many blocks were appended, and the
    list of proposed blocks skipped as duplicates (for the caller to warn
    about).
    """
    target_path = Path(target_path)
    existing_files = (
        check_bearing_files(config_path) if config_path is not None else [target_path]
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
        _write_config_text(
            target_path, yaml.safe_dump({"checks": blocks}, sort_keys=False)
        )
        return

    text, newline = _read_config_text(target_path)
    raw = yaml.safe_load(text)
    rendered = yaml.safe_dump(blocks, sort_keys=False)

    if isinstance(raw, list):
        if not text.endswith("\n"):
            text += "\n"
        _write_config_text(target_path, text + rendered, newline)
        return

    spliced = _append_into_block(text, "checks", rendered)
    if spliced is None:
        raw = dict(raw)
        raw["checks"] = list(raw.get("checks") or []) + blocks
        _write_config_text(target_path, yaml.safe_dump(raw, sort_keys=False), newline)
        return
    _write_config_text(target_path, spliced, newline)


def find_check_file(config_path: str | Path, check_id_: str) -> Path | None:
    """Which of :func:`check_bearing_files` for ``config_path`` contains the
    check with this id, or ``None`` if it isn't found in any of them. The
    counterpart lookup to :func:`append_checks`'s own dedup scan -- for
    locating a check to edit rather than confirming one doesn't already
    exist.
    """
    for path in check_bearing_files(config_path):
        for block in _raw_checks_in(path):
            if _check_id_of(block) == check_id_:
                return path
    return None


def _sequence_item_bounds(
    lines: list[str], top_level_key: str | None
) -> list[tuple[int, int]]:
    """(start, end) line ranges for each block-style sequence item under a
    top-level ``key:`` (``top_level_key`` given), or for the whole file
    when it's a bare sequence (``top_level_key`` is ``None``). Empty when
    the key is missing or the shape isn't a recognizable block sequence
    (e.g. flow-style ``checks: [...]``) -- the caller falls back to a full
    round trip rather than guess at an unrecognized shape.
    """
    if top_level_key is not None:
        idx = _find_top_level_key(lines, top_level_key)
        if idx is None:
            return []
        search_start = idx + 1
    else:
        search_start = 0
    file_end = len(lines)

    # _block_end assumes indented content; a block sequence's items start
    # at column 0 relative to their own dash (e.g. "checks:\n- source:
    # ..."), which it would misread as the next top-level key -- so the
    # sequence's true end is found separately below, not via _block_end.
    item_indent: int | None = None
    for i in range(search_start, file_end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidate = len(lines[i]) - len(lines[i].lstrip())
        if lines[i][candidate:].startswith("- "):
            item_indent = candidate
        break
    if item_indent is None:
        return []

    marker = " " * item_indent + "- "
    block_end = file_end
    for i in range(search_start, file_end):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        this_indent = len(lines[i]) - len(lines[i].lstrip())
        starts_item = lines[i][: len(marker)] == marker
        dedented = this_indent < item_indent
        same_indent_non_item = this_indent == item_indent and not starts_item
        if dedented or same_indent_non_item:
            block_end = i
            break

    starts = [
        i for i in range(search_start, block_end) if lines[i][: len(marker)] == marker
    ]
    return [
        (start, starts[j + 1] if j + 1 < len(starts) else block_end)
        for j, start in enumerate(starts)
    ]


def _mapping_entry_bounds(
    lines: list[str], parent_key: str, entry_key: str
) -> tuple[int, int] | None:
    """(start, end) line range for one ``entry_key:`` entry nested under a
    top-level ``parent_key:`` mapping -- the mapping-entry counterpart to
    :func:`_sequence_item_bounds`'s sequence-item ranges (e.g. one named
    source under ``sources:``, rather than one item under ``checks:``).

    ``end`` includes the entry's own more-indented continuation lines
    (block-style, e.g. ``s:\\n  type: sqlite\\n``) or just its single line
    (inline flow-style, e.g. ``s: { type: sqlite, database: ... }``),
    stopping at the next line indented no deeper than the entry line
    itself, or at the parent block's end -- greedy about trailing blank
    and comment lines the same way :func:`_sequence_item_bounds` is, so a
    caller that deletes or replaces the range should trim it first (see
    :func:`_trim_trailing_blank_or_comment_lines`).

    Returns ``None`` when ``parent_key`` or ``entry_key`` isn't found, or
    when ``parent_key:``'s own value is flow-style with existing entries
    (e.g. ``sources: {a: {...}}`` on one line) -- a shape this can't
    safely splice into; the caller falls back to a full round trip.
    """
    idx = _find_top_level_key(lines, parent_key)
    if idx is None:
        return None
    block_end = _block_end(lines, idx)
    inline = lines[idx].split(":", 1)[1].strip()
    if inline not in ("", "[]", "{}"):
        return None  # flow-style value -- not this function's shape to handle

    indent = _block_indent(lines, idx, block_end, default=2)
    pattern = re.compile(rf"^ {{{indent}}}{re.escape(entry_key)}:(.*)$")
    entry_start = next(
        (i for i in range(idx + 1, block_end) if pattern.match(lines[i])), None
    )
    if entry_start is None:
        return None

    entry_end = entry_start + 1
    while entry_end < block_end:
        line = lines[entry_end]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            entry_end += 1
            continue
        if len(line) - len(line.lstrip(" ")) <= indent:
            break
        entry_end += 1
    return entry_start, entry_end


def _replace_nested_key_value(
    lines: list[str], start: int, end: int, key: str, new_value: Any
) -> list[str] | None:
    """Within ``lines[start:end]`` (one sequence item's own lines), rewrite
    a ``  key:`` line's block-style value to ``new_value``, freshly
    rendered. Returns the modified full line list, or ``None`` when
    ``key`` isn't found in this item, or its value is inline (e.g.
    ``expect: {max: 0}``) rather than block-style -- either way, the
    caller falls back to a full round trip instead of guessing at an
    unhandled shape.
    """
    key_idx = None
    key_indent = 0
    prefix = f"{key}:"
    for i in range(start, end):
        stripped = lines[i].lstrip()
        if stripped.startswith(prefix):
            key_indent = len(lines[i]) - len(stripped)
            key_idx = i
            break
    if key_idx is None:
        return None

    inline = lines[key_idx].split(":", 1)[1].strip()
    if inline:
        return None  # inline/flow value -- not this function's shape to handle

    value_end = key_idx + 1
    while value_end < end:
        line = lines[value_end]
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            value_end += 1
            continue
        if len(line) - len(line.lstrip()) <= key_indent:
            break
        value_end += 1

    # yaml.safe_dump renders {key: new_value} 0-indented, with its nested
    # content already correctly indented *relative to* the "key:" line --
    # shifting every rendered line by key_indent (not just the nested
    # ones) reproduces that same relative shape at the position "key:"
    # actually sits at in the file, without double-counting the nesting
    # yaml.safe_dump already applied.
    rendered = yaml.safe_dump({key: new_value}, sort_keys=False)
    pad = " " * key_indent
    new_block = [pad + line for line in rendered.splitlines(keepends=True)]
    return lines[:key_idx] + new_block + lines[value_end:]


def rewrite_check_expectation(
    target_path: str | Path, check_id_: str, new_expect: dict
) -> bool:
    """Find the check matching ``check_id_`` in ``target_path`` and rewrite
    just its ``expect:`` value in place -- the edit-existing-check
    counterpart to :func:`append_checks`. Safe with respect to history:
    :func:`~dbfresh.checks.check_id` deliberately excludes ``expect`` from
    its hash, so editing a threshold here never forks a check's identity
    or its stored observation history.

    Attempts a text splice that touches only the matched item's ``expect:``
    block, preserving comments and formatting everywhere else in the file
    (mirroring :func:`_append_into_block`'s approach for appends); falls
    back to a full YAML round trip -- which loses comments -- only for a
    shape the splice can't safely handle (inline/flow-style ``expect:``,
    or a sequence shape it doesn't recognize).

    Returns ``True`` when a matching check was found and rewritten,
    ``False`` when no check with that id exists in this file.
    """
    target_path = Path(target_path)
    text, newline = _read_config_text(target_path)
    raw = yaml.safe_load(text)
    is_bare_list = isinstance(raw, list)
    checks_list = raw if is_bare_list else list((raw or {}).get("checks") or [])
    index = next(
        (i for i, block in enumerate(checks_list) if _check_id_of(block) == check_id_),
        None,
    )
    if index is None:
        return False

    lines = text.splitlines(keepends=True)
    bounds = _sequence_item_bounds(lines, None if is_bare_list else "checks")
    if index < len(bounds):
        start, end = bounds[index]
        # Exclude the trailing blank/comment run (spacing, or the *next*
        # check's own leading note) before splicing: expect: is the last key
        # an item emits, so _replace_nested_key_value's value scan would
        # otherwise reach this item's greedy end and drop that run.
        # remove_check trims the same way.
        end = _trim_trailing_blank_or_comment_lines(lines, start, end)
        spliced = _replace_nested_key_value(lines, start, end, "expect", new_expect)
        if spliced is not None:
            _write_config_text(target_path, "".join(spliced), newline)
            return True

    if is_bare_list:
        raw[index]["expect"] = new_expect
    else:
        raw["checks"][index]["expect"] = new_expect
    _write_config_text(target_path, yaml.safe_dump(raw, sort_keys=False), newline)
    return True


def _trim_trailing_blank_or_comment_lines(
    lines: list[str], start: int, end: int
) -> int:
    """Shrink an item's ``[start, end)`` range to exclude a trailing run of
    blank or comment-only lines.

    :func:`_sequence_item_bounds` greedily assigns everything up to the next
    item's own dash line (or EOF) to the item before it -- including a
    comment that's really a leading note about *that next* item, or plain
    blank-line spacing that isn't specific to either one. A deletion must
    leave those lines in place rather than erasing them along with the item
    being removed; the item's own first line (its dash) is never blank or a
    comment, so this never trims past ``start``.
    """
    trimmed = end
    while trimmed > start:
        stripped = lines[trimmed - 1].strip()
        if stripped == "" or stripped.startswith("#"):
            trimmed -= 1
            continue
        break
    return trimmed


def remove_check(config_path: str | Path, check_id_: str) -> None:
    """Remove the check with this id from wherever it lives among
    :func:`target_files`, preserving every other check plus the file's
    structure and comments -- the delete counterpart to
    :func:`rewrite_check_expectation`.

    Raises :class:`ValueError` when no check with ``check_id_`` exists in
    any target file: a delete request naming an unknown check must fail
    clearly rather than silently no-op or, worse, corrupt whichever file
    happened to be guessed at.

    Attempts a text splice that removes only the matched item's own lines,
    preserving comments and formatting everywhere else in the file
    (mirroring :func:`rewrite_check_expectation`'s approach); falls back to
    a full YAML round trip -- which loses comments -- only for a shape the
    splice can't safely handle (e.g. a flow-style ``checks: [...]``
    sequence). Removing the last check in a file leaves ``checks:`` with no
    items rather than deleting the key itself, which every reader here
    already treats as an empty check list.
    """
    config_path = Path(config_path)
    target_path = find_check_file(config_path, check_id_)
    if target_path is None:
        raise ValueError(f"check not found: {check_id_!r}")

    text, newline = _read_config_text(target_path)
    raw = yaml.safe_load(text)
    is_bare_list = isinstance(raw, list)
    checks_list = raw if is_bare_list else list((raw or {}).get("checks") or [])
    index = next(
        (i for i, block in enumerate(checks_list) if _check_id_of(block) == check_id_),
        None,
    )
    if index is None:
        raise ValueError(f"check not found: {check_id_!r} in {target_path}")

    lines = text.splitlines(keepends=True)
    bounds = _sequence_item_bounds(lines, None if is_bare_list else "checks")
    if index < len(bounds):
        start, end = bounds[index]
        end = _trim_trailing_blank_or_comment_lines(lines, start, end)
        _write_config_text(target_path, "".join(lines[:start] + lines[end:]), newline)
        return

    if is_bare_list:
        raw = list(raw)
        del raw[index]
    else:
        raw = dict(raw)
        checks = list(raw.get("checks") or [])
        del checks[index]
        raw["checks"] = checks
    _write_config_text(target_path, yaml.safe_dump(raw, sort_keys=False), newline)


def raw_source(config_path: str | Path, name: str) -> tuple[str, dict[str, str]]:
    """The ``type`` and raw, un-interpolated params of one configured
    source, straight off the root config -- ``${VAR}`` tokens intact,
    never resolved.

    ``sources:`` is declared only in the root config (see :func:`add_source`),
    so this always reads ``config_path`` directly, never an included
    checks file. What the Configure screen's edit-source form pre-fills
    from, so a secret param shows as ``${VAR}`` rather than a resolved
    value the user never typed; the counterpart write-back is
    :func:`rewrite_source`.

    Raises :class:`ValueError` when ``name`` isn't a configured source.
    """
    config_path = Path(config_path)
    raw = yaml.safe_load(config_path.read_text()) or {}
    sources = raw.get("sources") or {}
    if name not in sources:
        raise ValueError(f"source not found: {name!r}")
    entry = dict(sources[name])
    type_ = entry.pop("type", "")
    return type_, entry


def rewrite_source(
    config_path: str | Path, name: str, type_: str, params: dict
) -> None:
    """Replace one already-configured source's ``type``/params in place --
    the edit counterpart to :func:`add_source`. ``name`` is the entry's
    identity and is never changed here, only its ``type:`` and params.

    ``params`` are written verbatim, exactly like :func:`add_source` --
    they may hold ``${VAR}`` secret tokens, which must never be resolved
    before writing (the caller runs the mandatory connection test via
    :func:`probe_new_source` first, against the *resolved* params, but
    writes only what's passed here).

    Attempts a text splice that touches only the matched entry's own
    lines (:func:`_mapping_entry_bounds`), re-rendered at the same indent
    via :func:`_reindent`, preserving every other source and all comments
    elsewhere in the file. A comment sitting immediately after the
    entry's own lines and before the next source is trimmed off the
    replaced range first (:func:`_trim_trailing_blank_or_comment_lines`)
    -- it's the next source's leading note, not this one's, so a rewrite
    must not swallow it any more than a removal would (see
    :func:`remove_source`). Falls back to a full YAML round trip -- which
    loses comments -- only for a shape the splice can't safely handle
    (e.g. a flow-style ``sources: {a: {...}}`` one-liner).

    Raises :class:`ValueError` when ``name`` isn't a configured source.
    """
    config_path = Path(config_path)
    text, newline = _read_config_text(config_path)
    raw = yaml.safe_load(text) or {}
    sources = raw.get("sources") or {}
    if name not in sources:
        raise ValueError(f"source not found: {name!r}")

    entry = {"type": type_, **params}
    lines = text.splitlines(keepends=True)
    bounds = _mapping_entry_bounds(lines, "sources", name)
    if bounds is not None:
        start, end = bounds
        core_end = _trim_trailing_blank_or_comment_lines(lines, start, end)
        indent = len(lines[start]) - len(lines[start].lstrip(" "))
        rendered = yaml.safe_dump({name: entry}, sort_keys=False)
        new_lines = _reindent(rendered, indent)
        _write_config_text(
            config_path,
            "".join(lines[:start]) + new_lines + "".join(lines[core_end:]),
            newline,
        )
        return

    raw = dict(raw)
    sources = dict(raw.get("sources") or {})
    sources[name] = entry
    raw["sources"] = sources
    _write_config_text(config_path, yaml.safe_dump(raw, sort_keys=False), newline)


def remove_source(config_path: str | Path, name: str) -> None:
    """Remove one configured source's entry from the root config -- the
    remove counterpart to :func:`add_source`.

    Refuses to orphan a check: counts checks referencing ``name`` as
    their ``source:`` across every :func:`target_files` (root plus any
    included checks files) and, if any exist, raises :class:`ValueError`
    naming how many -- writing nothing -- rather than leaving those
    checks pointing at a source that no longer exists.

    Attempts a text splice (:func:`_mapping_entry_bounds`) that removes
    only the matched entry's own lines -- trimmed of any trailing
    blank/comment lines first (:func:`_trim_trailing_blank_or_comment_lines`)
    so a following source's own leading comment survives -- preserving
    every other source and comment elsewhere in the file. Falls back to a
    full YAML round trip -- which loses comments -- only for a shape the
    splice can't safely handle. Removing the last source leaves
    ``sources:`` present but empty, the same shape every reader here
    already tolerates.

    Raises :class:`ValueError` when ``name`` isn't a configured source.
    """
    config_path = Path(config_path)

    referencing = sum(
        1
        for path in check_bearing_files(config_path)
        for block in _raw_checks_in(path)
        if block.get("source") == name
    )
    if referencing:
        raise ValueError(
            f"{referencing} check(s) still reference source {name!r}; remove them first"
        )

    text, newline = _read_config_text(config_path)
    raw = yaml.safe_load(text) or {}
    sources = raw.get("sources") or {}
    if name not in sources:
        raise ValueError(f"source not found: {name!r}")

    lines = text.splitlines(keepends=True)
    bounds = _mapping_entry_bounds(lines, "sources", name)
    if bounds is not None:
        start, end = bounds
        end = _trim_trailing_blank_or_comment_lines(lines, start, end)
        _write_config_text(config_path, "".join(lines[:start] + lines[end:]), newline)
        return

    raw = dict(raw)
    sources = dict(raw.get("sources") or {})
    del sources[name]
    raw["sources"] = sources
    _write_config_text(config_path, yaml.safe_dump(raw, sort_keys=False), newline)
