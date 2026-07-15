"""Configure screen: introspect a source+object, propose, append YAML.

Reuses the front-end-agnostic ``configurator`` module exactly as
`dbfresh add` does -- the same introspection, proposal, and YAML-emission
functions; only the prompt/rendering layer differs. Never writes to the
observation store.
"""

from __future__ import annotations

import re
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from dbfresh.adapters.base import Column
from dbfresh.checks import Check, check_id, parse_duration
from dbfresh.config import Config
from dbfresh.configurator import (
    append_checks,
    build_offered_check,
    check_object_exists,
    find_check_file,
    key_introspection_note,
    offered_column_checks,
    pick_timestamp_column,
    propose_checks,
    rewrite_check_expectation,
    target_files,
)
from dbfresh.tui.dashboard import check_label

_UNSAFE_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# Threshold-bearing offered metrics get a value Input beside their checkbox,
# pre-filled with the CLI wizard's own prompt default for that metric --
# every other offered metric (sum, row_count, ...) takes no threshold.
_OFFERED_VALUE_DEFAULTS: dict[str, str] = {"null_rate": "0.05", "freshness": "24h"}

# An existing check's expect: operand is editable via a single text Input
# only when it's one scalar value -- between (a [lo, hi] pair), vs_previous
# (a nested guard mapping), and schema's unchanged (no operand at all) all
# need more than one Input's worth of UI, so v1 shows those read-only
# rather than build a bespoke form per shape.
_NON_EDITABLE_OPERATORS = frozenset({"between", "unchanged", "vs_previous"})


def _id_part(value: str) -> str:
    """A CSS-identifier-safe fragment for a dynamic widget id."""
    return _UNSAFE_ID_CHARS.sub("_", value)


def _describe_proposed(block: dict) -> str:
    """One-line label for a proposed check's trim checkbox: metric, its
    column/key context (when it has one), and the expectation.

    Freshness omits the expectation from the label -- it gets its own
    editable value Input right beside the checkbox (see
    :meth:`ConfigureScreen._mount_proposed_checkboxes`), and showing the
    default a second time in static text would drift out of sync with
    whatever the user types into that Input.
    """
    context = block.get("column") or block.get("key")
    header = f"{block['metric']} ({context})" if context else block["metric"]
    if block["metric"] == "freshness":
        return header
    return f"{header}: {block['expect']}"


class ConfigureScreen(Screen[bool]):
    """Propose and append a check bundle for a named source + object.

    Dismisses with ``True`` when it wrote checks to disk (so Home reloads
    the config and refreshes the dashboard), ``False`` otherwise.
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, config_path: str | Path, config: Config) -> None:
        super().__init__()
        self._config_path = Path(config_path)
        self._config = config
        self._proposed: list[dict] = []
        self._proposed_checkboxes: list[Checkbox] = []
        self._proposed_value_inputs: list[Input | None] = []
        self._offered_blocks: list[dict] = []
        self._offered_checkboxes: list[Checkbox] = []
        self._offered_value_inputs: list[Input | None] = []
        self._has_calendar = False
        self._target_file: Path | None = None
        self._existing_checks: list[Check] = []
        self._existing_value_inputs: list[Input | None] = []
        self._existing_edit_made = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Source name")
        yield Select(
            [(name, name) for name in sorted(self._config.sources)],
            id="source-select",
        )
        yield Label("Object name")
        yield Input(id="object-input")
        yield Label("Timestamp column (only if ambiguous -- see proposal)")
        yield Input(id="timestamp-input")
        with Horizontal():
            yield Button("Propose", id="propose-btn")
            yield Button("Accept", id="accept-btn", disabled=True)
            yield Button("Cancel", id="cancel-btn")
        yield VerticalScroll(
            Static("", id="proposal-text", markup=False),
            Static("Existing checks for this object"),
            Vertical(id="existing-checks"),
            Static("Proposed checks (uncheck any to drop them)"),
            Vertical(id="proposed-checks"),
            Static("Offered checks (check any to add them)"),
            Vertical(id="offered-checks"),
            id="proposal-scroll",
        )
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "propose-btn":
            self._propose()
        elif button_id == "accept-btn":
            self._accept()
        elif button_id == "cancel-btn":
            self.dismiss(self._existing_edit_made)
        elif button_id.startswith("existing-save-"):
            self._save_existing(int(button_id.removeprefix("existing-save-")))

    def _reset_proposal(self) -> None:
        """Clear state and widgets left over from a previous Propose click."""
        self._proposed = []
        self._proposed_checkboxes = []
        self._proposed_value_inputs = []
        self._offered_blocks = []
        self._offered_checkboxes = []
        self._offered_value_inputs = []
        self._target_file = None
        self._existing_checks = []
        self._existing_value_inputs = []
        self.query_one("#existing-checks", Vertical).remove_children()
        self.query_one("#proposed-checks", Vertical).remove_children()
        self.query_one("#offered-checks", Vertical).remove_children()
        self.query_one("#accept-btn", Button).disabled = True

    def _mount_proposed_checkboxes(self) -> None:
        """One checkbox per proposed check, checked by default -- unchecking
        one trims it from what Accept writes. Freshness also gets a value
        Input beside its checkbox, pre-filled with the proposal's default
        max_lag (``propose_checks`` always proposes "24h") -- editable
        before Accept, so a custom threshold doesn't require accepting the
        default first and editing it back in as an already-written check.
        No other proposed metric (schema, row_count, duplicate_count) has
        a single-scalar threshold worth exposing this way.
        """
        container = self.query_one("#proposed-checks", Vertical)
        for i, block in enumerate(self._proposed):
            checkbox = Checkbox(
                _describe_proposed(block),
                value=True,
                id=f"proposed-{i}-{_id_part(block['metric'])}",
            )
            self._proposed_checkboxes.append(checkbox)
            if block["metric"] == "freshness":
                value_input = Input(
                    value=str(block["expect"]["max_lag"]), id=f"proposed-value-{i}"
                )
                self._proposed_value_inputs.append(value_input)
                container.mount(Horizontal(checkbox, value_input))
            else:
                self._proposed_value_inputs.append(None)
                container.mount(checkbox)

    def _mount_offered_checkboxes(
        self,
        source_name: str,
        object_name: str,
        columns: list[Column],
        has_calendar: bool,
        proposed: list[dict],
    ) -> None:
        """Per-column offered checks (:func:`offered_column_checks`), one
        unchecked checkbox per metric -- checking one adds it to what Accept
        writes. Mirrors the CLI wizard's "Offered for <column>: ..." prompt,
        which defaults to nothing added unless the user opts in. ``null_rate``
        and ``freshness`` also get a threshold Input beside the checkbox,
        pre-filled with the CLI wizard's own prompt default for that metric
        (see :data:`_OFFERED_VALUE_DEFAULTS`) -- Accept rebuilds the check
        from whatever value sits in that Input at that point, see
        :meth:`_rebuild_offered_check`. ``proposed`` is the bundle this
        object's Propose click already built, passed through so
        :func:`offered_column_checks` excludes any ``(metric, column)`` pair
        already covered there instead of offering it a second time."""
        container = self.query_one("#offered-checks", Vertical)
        for offer in offered_column_checks(columns, proposed):
            if not offer["checks"]:
                continue
            container.mount(
                Static(f"Offered for {offer['column']} ({offer['category']}):")
            )
            for metric in offer["checks"]:
                block = build_offered_check(
                    source_name, object_name, offer["column"], metric, has_calendar
                )
                checkbox = Checkbox(
                    metric,
                    value=False,
                    id=f"offered-{_id_part(offer['column'])}-{_id_part(metric)}",
                )
                self._offered_blocks.append(block)
                self._offered_checkboxes.append(checkbox)

                default = _OFFERED_VALUE_DEFAULTS.get(metric)
                if default is None:
                    self._offered_value_inputs.append(None)
                    container.mount(checkbox)
                else:
                    value_input = Input(
                        value=default,
                        id=(
                            f"offered-value-{_id_part(offer['column'])}"
                            f"-{_id_part(metric)}"
                        ),
                    )
                    self._offered_value_inputs.append(value_input)
                    container.mount(Horizontal(checkbox, value_input))

    def _mount_existing_checks(self, source_name: str, object_name: str) -> None:
        """The object's already-written checks: read-only except for a
        single-scalar ``expect:`` operand, which gets an editable Input +
        Save button -- the edit-existing-check half of this screen (the
        propose/accept flow below only ever writes *new* checks). Save
        writes straight to disk via :func:`rewrite_check_expectation`,
        unlike Accept, which stages new checks until the button is
        pressed -- there's no separate bundle to review here, just one
        value per already-configured check.
        """
        container = self.query_one("#existing-checks", Vertical)
        checks = [
            c
            for c in self._config.checks
            if c.source == source_name and c.object == object_name
        ]
        self._existing_checks = checks
        if not checks:
            container.mount(Static("(none yet)"))
            return

        for i, check in enumerate(checks):
            label = check_label(check)
            if check.expect is None:
                container.mount(Static(label))
                self._existing_value_inputs.append(None)
                continue
            if check.expect.operator in _NON_EDITABLE_OPERATORS:
                container.mount(Static(f"{label}: {check.expect.describe()}"))
                self._existing_value_inputs.append(None)
                continue
            operand = check.expect.operand
            current = operand if isinstance(operand, str) else str(operand)
            value_input = Input(value=current, id=f"existing-value-{i}")
            self._existing_value_inputs.append(value_input)
            container.mount(
                Horizontal(
                    Static(f"{label} ({check.expect.operator}):"),
                    value_input,
                    Button("Save", id=f"existing-save-{i}"),
                )
            )

    def _save_existing(self, index: int) -> None:
        """Parse the edited Input for existing check ``index`` and rewrite
        just that check's ``expect:`` operand on disk, preserving its
        operator (only the value beside it is editable)."""
        check = self._existing_checks[index]
        value_input = self._existing_value_inputs[index]
        # only a Save-able check (expect set, single-scalar operator) ever
        # gets a button wired to this index -- see _mount_existing_checks.
        assert check.expect is not None
        assert value_input is not None
        raw_value = value_input.value.strip()

        new_operand: str | float
        if check.metric == "freshness":
            try:
                parse_duration(raw_value)
            except ValueError as exc:
                self.notify(f"invalid max lag: {exc}", severity="error")
                return
            new_operand = raw_value
        else:
            try:
                new_operand = float(raw_value)
            except ValueError:
                self.notify(f"not a number: {raw_value!r}", severity="error")
                return

        cid = check_id(check)
        target = find_check_file(self._config_path, cid)
        if target is None:
            self.notify(f"could not locate check {cid} on disk", severity="error")
            return
        rewrite_check_expectation(target, cid, {check.expect.operator: new_operand})
        self._existing_edit_made = True
        self.notify(f"saved {check_label(check)}")

    def _propose(self) -> None:
        from dbfresh.adapters.factory import create_adapter

        proposal_widget = self.query_one("#proposal-text", Static)
        self._reset_proposal()

        select = self.query_one("#source-select", Select)
        if select.value is Select.BLANK:
            proposal_widget.update("select a source")
            return
        source_name = str(select.value)
        object_name = self.query_one("#object-input", Input).value.strip()

        source = self._config.sources.get(source_name)
        if source is None:
            proposal_widget.update(f"unknown source: {source_name!r}")
            return

        try:
            adapter = create_adapter(source.type, source.params, timeout=source.timeout)
        except Exception as exc:
            proposal_widget.update(f"could not connect to {source_name!r}: {exc}")
            return

        try:
            existence = check_object_exists(adapter, object_name)
            if not existence.exists:
                proposal_widget.update(
                    f"object not found: {object_name!r} ({existence.error})"
                )
                return
            # existence.exists is only True when describe() succeeded, which
            # is exactly when info is populated (see ExistenceCheck).
            assert existence.info is not None

            self._mount_existing_checks(source_name, object_name)

            ambiguity_note = None
            timestamp_override = None
            timestamp = pick_timestamp_column(existence.info.columns)
            if timestamp.needs_choice:
                entered = self.query_one("#timestamp-input", Input).value.strip()
                if entered in timestamp.candidates:
                    timestamp_override = entered
                else:
                    ambiguity_note = (
                        "ambiguous timestamp candidates: "
                        + ", ".join(timestamp.candidates)
                        + " -- enter one above and Propose again"
                    )

            has_calendar = self._config.calendar is not None
            self._has_calendar = has_calendar
            self._proposed = propose_checks(
                source_name,
                object_name,
                existence.info,
                adapter.dialect,
                has_calendar=has_calendar,
                is_view=existence.info.is_view,
                timestamp_override=timestamp_override,
            )
            key_note = key_introspection_note(adapter.dialect, existence.info)
            self._mount_proposed_checkboxes()
            self._mount_offered_checkboxes(
                source_name,
                object_name,
                existence.info.columns,
                has_calendar,
                self._proposed,
            )
        finally:
            adapter.close()

        notes: list[str] = []
        if key_note is not None:
            notes.append(key_note)
        if ambiguity_note is not None:
            notes.append(ambiguity_note)

        files = target_files(self._config_path)
        self._target_file = files[0] if files else self._config_path
        if len(files) > 1:
            notes.append(
                f"writing to {self._target_file} (of {len(files)} included files)"
            )

        if not notes and not self._proposed and not self._offered_blocks:
            notes.append("no checks proposed")

        proposal_widget.update("\n".join(notes))
        self.query_one("#accept-btn", Button).disabled = not self._proposed

    def _rebuild_offered_check(
        self, block: dict, raw_value: str
    ) -> tuple[dict | None, str | None]:
        """Re-run :func:`build_offered_check` with the threshold Input's
        current text in place of the default baked into ``block`` when it
        was mounted. Returns ``(rebuilt, None)`` on success, or
        ``(None, error)`` when ``raw_value`` doesn't parse for the metric --
        the same value formats the CLI wizard's prompt accepts."""
        metric = block["metric"]
        column = block["column"]
        value = raw_value.strip()
        if metric == "null_rate":
            try:
                max_null_rate = float(value)
            except ValueError:
                return None, f"{column}: not a number for max null rate: {value!r}"
            return (
                build_offered_check(
                    block["source"],
                    block["object"],
                    column,
                    metric,
                    self._has_calendar,
                    max_null_rate=max_null_rate,
                ),
                None,
            )
        assert metric == "freshness"
        try:
            parse_duration(value)
        except ValueError as exc:
            return None, f"{column}: invalid max lag: {exc}"
        return (
            build_offered_check(
                block["source"],
                block["object"],
                column,
                metric,
                self._has_calendar,
                max_lag=value,
            ),
            None,
        )

    def _rebuild_proposed_check(
        self, block: dict, raw_value: str
    ) -> tuple[dict | None, str | None]:
        """Re-run the proposed freshness block with the threshold Input's
        current text in place of the "24h" default baked in at Propose
        time. Returns ``(rebuilt, None)`` on success, or ``(None, error)``
        when ``raw_value`` doesn't parse as a duration -- the proposed-check
        counterpart to :meth:`_rebuild_offered_check`; only freshness is
        ever proposed with a single-scalar threshold to rebuild this way."""
        value = raw_value.strip()
        try:
            parse_duration(value)
        except ValueError as exc:
            return None, f"freshness: invalid max lag: {exc}"
        return {**block, "expect": {"max_lag": value}}, None

    def _selected_checks(self) -> tuple[list[dict], list[str]]:
        """Proposed checks still checked, plus offered checks checked in --
        the trim and offered-selection interactions collapsed into one
        list, in the order Accept writes them. A proposed ``freshness``
        check (and an offered ``null_rate`` or ``freshness`` check) is
        rebuilt from its threshold Input rather than the default block
        mounted at Propose time; a value that fails to parse is reported
        back as an error instead of being written."""
        selected: list[dict] = []
        errors: list[str] = []
        for block, checkbox, value_input in zip(
            self._proposed,
            self._proposed_checkboxes,
            self._proposed_value_inputs,
            strict=True,
        ):
            if not checkbox.value:
                continue
            if value_input is None:
                selected.append(block)
                continue
            rebuilt, error = self._rebuild_proposed_check(block, value_input.value)
            if error is not None:
                errors.append(error)
                continue
            assert rebuilt is not None
            selected.append(rebuilt)
        for block, checkbox, value_input in zip(
            self._offered_blocks,
            self._offered_checkboxes,
            self._offered_value_inputs,
            strict=True,
        ):
            if not checkbox.value:
                continue
            if value_input is None:
                selected.append(block)
                continue
            rebuilt, error = self._rebuild_offered_check(block, value_input.value)
            if error is not None:
                errors.append(error)
                continue
            assert rebuilt is not None
            selected.append(rebuilt)
        return selected, errors

    def _accept(self) -> None:
        selected, errors = self._selected_checks()
        if errors:
            self.query_one("#proposal-text", Static).update("\n".join(errors))
            return
        if not selected:
            return
        target = self._target_file
        if target is None:
            files = target_files(self._config_path)
            target = files[0] if files else self._config_path
        written, skipped = append_checks(
            target, selected, config_path=self._config_path
        )
        self.notify(f"wrote {written} check(s), skipped {len(skipped)} duplicate(s)")
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(self._existing_edit_made)
