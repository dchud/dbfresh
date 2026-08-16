"""Front-end-agnostic configurator: introspect, propose, emit YAML.

All proposal, validation, YAML-serialization, connection-test, and
existence-check logic lives here as plain functions and dataclasses, so both
`dbfresh add` (a thin interactive shell) and the TUI Configure screen share
one tested surface. This module writes nothing to disk -- not the
observation store, not the config file itself. It only reads catalog
metadata via an adapter's ``describe()`` and renders a proposal as a YAML
string; pasting that into the version-controlled config is left to whoever
is running the wizard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from dbfresh.adapters.base import (
    Adapter,
    Category,
    Column,
    Dialect,
    ObjectInfo,
)
from dbfresh.adapters.factory import create_adapter
from dbfresh.checks import Check, check_id
from dbfresh.config import (
    flatten_table_checks,
    group_checks_by_table,
    interpolate_env,
    normalize_check_sets,
    resolve_includes,
)

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

    conventional = [
        c for c in temporal if _is_conventional_timestamp_name(c.name)
    ]
    if len(conventional) == 1:
        return TimestampChoice(column=conventional[0].name)
    if len(temporal) == 1:
        return TimestampChoice(column=temporal[0].name)

    pool = conventional or temporal
    return TimestampChoice(
        needs_choice=True, candidates=[c.name for c in pool]
    )


_CATEGORY_OFFERS: dict[Category, list[str]] = {
    Category.NUMERIC: [
        "null_rate",
        "sum",
        "avg",
        "min",
        "max",
        "duplicate_count",
    ],
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
            block.get("key")
            if metric == "duplicate_count"
            else block.get("column")
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
    drop one via :func:`partition_new_checks` instead of proposing two
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
            {
                "column": column.name,
                "category": column.category.value,
                "checks": checks,
            }
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
            source,
            obj,
            "null_rate",
            column=column,
            expect={"max": max_null_rate},
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
                    source,
                    obj,
                    "duplicate_count",
                    key=key[0],
                    expect={"max": 0},
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


class _IndentedDumper(yaml.SafeDumper):
    """A dumper that indents sequence items under their parent key.

    PyYAML renders a sequence indentless by default, putting its items in
    the parent key's own column. Items in that form cannot be pasted into
    an existing ``checks:`` whose items are indented -- a sequence's items
    must all share one indentation -- so the emitted proposal uses the
    two-space item indent that ``config.example.yaml`` and the docs use.
    """

    def increase_indent(self, flow: bool = False, indentless: bool = False):
        return super().increase_indent(flow, False)


class _FlowMap(dict):
    """A mapping rendered inline, as ``{ key: value }``."""


class _FlowSeq(list):
    """A sequence rendered inline, as ``[a, b]``."""


_IndentedDumper.add_representer(
    _FlowMap,
    lambda dumper, data: dumper.represent_mapping(
        "tag:yaml.org,2002:map", data, flow_style=True
    ),
)
_IndentedDumper.add_representer(
    _FlowSeq,
    lambda dumper, data: dumper.represent_sequence(
        "tag:yaml.org,2002:seq", data, flow_style=True
    ),
)

# The check fields whose values are rendered inline. An expectation is
# one short operator/operand pair, and `config.example.yaml` and the docs
# write every one of them as `expect: { max: 5 }`. Block style would
# spread that over two lines, and `expect: { between: [a, b] }` over
# four, which is how a config re-rendered wholesale can come back longer
# than it went in -- enough to cancel what grouping saves.
_INLINE_CHECK_FIELDS = frozenset({"expect", "on_holiday"})


def _inline(value: Any) -> Any:
    """Mark ``value`` and everything under it for inline rendering."""
    if isinstance(value, dict):
        return _FlowMap({key: _inline(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FlowSeq([_inline(item) for item in value])
    return value


def inline_check_expectations(check: dict) -> dict:
    """A copy of ``check`` whose expectation fields render inline.

    ``by_weekday:`` is handled one level down: the day names stay on
    their own lines, each day's expectation inline beside it, which is
    the shape the example config uses.
    """
    rendered: dict[str, Any] = {}
    for key, value in check.items():
        if key in _INLINE_CHECK_FIELDS:
            rendered[key] = _inline(value)
        elif key == "by_weekday" and isinstance(value, dict):
            rendered[key] = {day: _inline(item) for day, item in value.items()}
        else:
            rendered[key] = value
    return rendered


def _dump_document(document: dict[str, Any]) -> str:
    """Render one YAML document with :class:`_IndentedDumper`'s two-space
    sequence indent -- the single YAML-serialization call every emitted
    proposal goes through, so a pasted block's indentation always matches
    ``config.example.yaml`` and the docs regardless of which command
    produced it.
    """
    return yaml.dump(document, Dumper=_IndentedDumper, sort_keys=False)


def _rendered_table_entries(tables: list[dict]) -> list[dict]:
    """Table entries with inline-style ``checks:``/``with:`` rendering
    applied -- the per-entry rendering shared by :func:`render_proposal`
    (a synthesized ``tables:`` block, grouped from a flat proposal) and
    :func:`render_tables_proposal` (a full-file regroup for
    ``config migrate``), so both dumps look identical for identical
    entries regardless of which command built them.

    An entry with no ``checks:`` key at all -- a ``use:``-backed entry
    carried over unchanged, with no inline checks of its own -- keeps it
    that way: rendering ``checks: []`` into it would add clutter the
    entry never had, the one case where this must not touch a key the
    entry didn't already declare.

    A table's ``with:`` renders inline, for the same reason an
    expectation does: it is a short parameter list the docs write as
    ``with: { ts_column: modified_at }``, and a block-style copy would
    cost a line per parameter per table -- paid on every table in the
    file, in output whose purpose is a smaller one.
    """
    rendered = []
    for entry in tables:
        out = dict(entry)
        if "checks" in entry:
            out["checks"] = [
                inline_check_expectations(check)
                for check in entry.get("checks") or []
            ]
        if isinstance(entry.get("with"), dict):
            out["with"] = _inline(entry["with"])
        rendered.append(out)
    return rendered


def render_proposal(
    source_entry: tuple[str, dict] | None, checks: list[dict]
) -> str:
    """Render the proposal as one valid YAML document.

    Keyed by ``sources:`` / ``tables:`` rather than emitted as bare
    entries: the two would otherwise concatenate into text that is not
    YAML at all, and a document carrying its own keys is both a complete
    starter config when there is no config file yet and, when there is,
    a block whose entries sit at the indent they need under the matching
    key. ``checks`` is grouped into ``tables:`` entries by
    :func:`~dbfresh.config.group_checks_by_table` -- the same grouping
    ``dbfresh config migrate`` uses -- so ``source:``/``object:`` are
    stated once per object rather than repeated on every check; a single
    object's checks (the wizard's normal case) group into one entry the
    same way several would. Shared by ``dbfresh add`` (prints this to
    stdout) and the TUI Configure screen (shows this in a copyable text
    area), so both front ends emit identical YAML for the same proposal.
    """
    document: dict[str, Any] = {}
    if source_entry is not None:
        name, entry = source_entry
        document["sources"] = {name: entry}
    if checks:
        document["tables"] = _rendered_table_entries(
            group_checks_by_table(checks)
        )
    return _dump_document(document)


def render_tables_proposal(tables: list[dict]) -> str:
    """Render a ``tables:`` block alone, in the same style
    :func:`render_proposal` uses for ``sources:``/``tables:``.

    The sole caller is ``dbfresh config migrate``: it emits only the
    ``tables:`` block a file's checks fold into, never a full document,
    since every other section of the file stays exactly as the user wrote
    it.
    """
    return _dump_document({"tables": _rendered_table_entries(tables)})


@dataclass(frozen=True)
class ConnectionProbe:
    """Result of a mandatory connection test for a new source."""

    ok: bool
    error: str | None = None


def probe_connection(type_: str, params: dict) -> ConnectionProbe:
    """Build the adapter and run a trivial query to confirm it connects.

    Mandatory before a brand-new source's block is included in the
    rendered proposal; never raises -- any failure (unknown type, bad
    credentials, unreachable host) comes back as
    ``ConnectionProbe(ok=False, error=...)``.
    """
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


def probe_new_source(
    type_: str, raw_params: dict
) -> tuple[ConnectionProbe, dict]:
    """Probe a brand-new source's params after resolving ``${VAR}`` tokens.

    ``raw_params`` is exactly what the emitted YAML carries -- it may hold
    ``${VAR}`` secrets. The connection test itself must run against the
    resolved value (never a literal ``${VAR}`` string), so this returns
    ``(probe, resolved_params)``: use ``resolved_params`` to build a live
    adapter for further use (e.g. ``describe()``) when ``probe.ok``, but
    never emit it -- the caller passes ``raw_params`` verbatim to
    :func:`render_proposal` so the config the user pastes keeps ``${VAR}``
    rather than a literal secret. An undefined variable fails the probe
    cleanly rather than raising.
    """
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


def check_object_exists(
    adapter: Adapter | None, object_name: str
) -> ExistenceCheck:
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
    """Resolve this config's ``include:`` files, or the root config alone.

    When the root config declares ``include:``, this returns the resolved
    matches (lexicographic order, matching load order), via
    :func:`dbfresh.config.resolve_includes` -- the same resolver
    ``load_config`` uses, so an unmatched glob is a hard error here too,
    never a silently empty list. Without ``include:``, the sole result is
    the root config itself. :func:`check_bearing_files` is the only
    caller, prepending the root config to this result so a config that
    keeps checks in both the root file and its included files is fully
    covered.
    """
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

    Distinct from :func:`target_files`, which resolves only the included
    files once ``include:`` is set and drops the root. A config may keep
    checks in the root config *and* in included files (``load_config``
    composes both), and :func:`partition_new_checks` -- deciding which
    proposed checks are already defined -- needs to see all of them, not
    just the included ones. Root first, then the included files,
    de-duplicated by resolved path.
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


def _raw_check_sets_in(path: Path) -> dict[str, dict]:
    """One file's own ``check_sets:``, normalized via
    :func:`~dbfresh.config.normalize_check_sets` and its problems
    discarded -- the same tolerance :func:`_raw_checks_in` applies to a
    malformed ``tables:`` entry: a ``check_sets:`` problem is
    ``config validate``'s job to report, not this dedup pass's, and a set
    that failed to normalize can't be used by anything anyway.
    """
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        return {}
    sets, _problems = normalize_check_sets(raw.get("check_sets"), path)
    return sets


def _raw_checks_in(path: Path, check_sets: dict[str, dict]) -> list[dict]:
    """The raw check blocks in one config or included-checks file --
    flat ``checks:`` entries plus every check nested under ``tables:``,
    flattened via :func:`~dbfresh.config.flatten_table_checks` so a check
    defined either way counts as already-defined the same way. Whether a
    flattened entry is well-formed is ``config validate``'s job, not
    this dedup pass's: a problem it finds is simply discarded here,
    since a raw dict that couldn't be flattened can't collide with
    anything anyway.

    ``check_sets`` is composed across every check-bearing file by
    :func:`partition_new_checks` before this is called per-file, exactly
    as :func:`~dbfresh.config._load_config` composes it before flattening
    any one file's ``tables:`` -- a table in this file may ``use:`` a set
    defined in another.
    """
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if isinstance(raw, list):
        flat, tables = raw, []
    else:
        flat = list(raw.get("checks") or [])
        tables = list(raw.get("tables") or [])
    table_checks, _problems = flatten_table_checks(tables, check_sets)
    return [*flat, *table_checks]


def _check_id_of(raw: dict) -> str:
    """The :func:`dbfresh.checks.check_id` a raw YAML check block derives to.

    Built from only the identity-bearing fields ``check_id`` hashes --
    never ``expect``, so this never has to parse (and possibly reject) an
    expectation just to compute an identity for dedup purposes.
    """
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


def partition_new_checks(
    config_path: str | Path, blocks: list[dict]
) -> tuple[list[dict], list[dict]]:
    """Split proposed blocks into ``(new, already_defined)``.

    A block whose derived ``check_id`` already exists anywhere in the
    composed config -- the root config plus every included checks file, per
    :func:`check_bearing_files` -- is already defined, and adding it a
    second time would make the next :func:`dbfresh.config.load_config`
    raise on the duplicate id. Blocks colliding with each other inside
    ``blocks`` are separated the same way, the first occurrence winning.

    A config file that does not exist yet defines nothing, so every block
    is new. Read-only: this reports what is already there and never writes.
    """
    config_path = Path(config_path)
    seen: set[str] = set()
    if config_path.exists():
        files = check_bearing_files(config_path)
        # check_sets: composed across every check-bearing file before any
        # of them is flattened -- a table in one file may `use:` a set
        # defined in another, so flattening file-by-file in isolation
        # (as this used to) would silently miss those checks, and a
        # second `add` run would re-propose ones that already exist. The
        # first file to define a name keeps it; a duplicate is
        # config validate's problem to report, not this dedup pass's.
        check_sets: dict[str, dict] = {}
        for path in files:
            for name, definition in _raw_check_sets_in(path).items():
                check_sets.setdefault(name, definition)
        seen = {
            _check_id_of(raw)
            for path in files
            for raw in _raw_checks_in(path, check_sets)
        }

    new: list[dict] = []
    already_defined: list[dict] = []
    for block in blocks:
        cid = _check_id_of(block)
        if cid in seen:
            already_defined.append(block)
            continue
        seen.add(cid)
        new.append(block)
    return new, already_defined


def referenced_env_vars(params: dict) -> list[str]:
    """The sorted ``${VAR}`` names a source's connection params reference.

    Interpolates against an empty environment so every reference lands in
    ``missing`` whether or not it happens to be set here -- the same
    reasoning as :func:`dbfresh.config.collect_referenced_env_vars`, which
    reports the same names once the source is in the config. This one works
    from params the config does not hold yet.
    """
    missing: set[str] = set()
    interpolate_env(params, {}, missing)
    return sorted(missing)
