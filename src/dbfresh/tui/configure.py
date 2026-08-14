"""Configure screen: introspect a source+object, propose, render YAML.

Reuses the front-end-agnostic ``configurator`` module exactly as
`dbfresh add` does -- the same introspection and proposal functions, and
the same :func:`~dbfresh.configurator.render_proposal` the CLI prints to
stdout; only the prompt/rendering layer differs. Never writes to config or
to the observation store: a source is defined by hand (or via
``dbfresh add``), and Accept renders the selected checks as YAML text to
copy rather than writing them anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)
from textual.worker import Worker, WorkerState

from dbfresh.adapters.base import Column
from dbfresh.adapters.factory import create_adapter
from dbfresh.checks import Check, parse_duration
from dbfresh.config import Config, SourceConfig
from dbfresh.configurator import (
    build_offered_check,
    check_object_exists,
    key_introspection_note,
    offered_column_checks,
    partition_new_checks,
    pick_timestamp_column,
    propose_checks,
    render_proposal,
)
from dbfresh.tui.dashboard import check_expectation_line

_PROPOSE_WORKER_GROUP = "propose"

_UNSAFE_ID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")

# Threshold-bearing offered metrics get a value Input beside their checkbox,
# pre-filled with the CLI wizard's own prompt default for that metric --
# every other offered metric (sum, row_count, ...) takes no threshold.
_OFFERED_VALUE_DEFAULTS: dict[str, str] = {
    "null_rate": "0.05",
    "freshness": "24h",
}


@dataclass
class _ProposeOutcome:
    """What :meth:`ConfigureScreen._propose_worker` hands back to the main
    thread, off of which :meth:`ConfigureScreen.on_worker_state_changed`
    mounts widgets.

    ``error``, when set, is the only field that matters -- a connect
    failure or a missing object aborts the proposal before anything else
    is known, and every other field is left at its default. Otherwise
    every field the main thread needs to mount the existing/proposed/
    offered-checks widgets and the review notes, none of which the worker
    itself may touch directly (see :meth:`ConfigureScreen._propose_worker`).
    """

    error: str | None = None
    source_name: str = ""
    object_name: str = ""
    columns: list[Column] = field(default_factory=list)
    has_calendar: bool = False
    proposed: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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


class ProposalYamlScreen(ModalScreen[None]):
    """The accepted checks, rendered as YAML in a read-only, selectable
    text area to copy into config by hand -- what
    :meth:`ConfigureScreen._accept` opens instead of writing anything. The
    Copy button uses :meth:`~textual.app.App.copy_to_clipboard` (OSC 52 --
    works in most terminals, notably not macOS's own Terminal.app); the
    text area itself is always selectable for a manual copy where that
    doesn't reach.
    """

    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def __init__(self, config_path: Path, yaml_text: str) -> None:
        super().__init__()
        self._config_path = config_path
        self._yaml_text = yaml_text

    def compose(self) -> ComposeResult:
        with Vertical(id="proposal-yaml-panel"):
            yield Static("Proposed checks", classes="screen-heading")
            yield Static(
                f"Paste under `checks:` in {self._config_path}.",
                id="proposal-yaml-note",
            )
            yield TextArea(
                self._yaml_text, read_only=True, id="proposal-yaml-text"
            )
            with Horizontal():
                yield Button(
                    "Copy", id="proposal-yaml-copy-btn", variant="primary"
                )
                yield Button("Close", id="proposal-yaml-close-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "proposal-yaml-copy-btn":
            self.app.copy_to_clipboard(self._yaml_text)
            self.notify("copied to clipboard")
        elif event.button.id == "proposal-yaml-close-btn":
            self.dismiss()

    def action_dismiss_screen(self) -> None:
        self.dismiss()


class ConfigureScreen(Screen[None]):
    """Propose a check bundle for a named source + object, and render the
    accepted ones as YAML to copy into config by hand.

    The source Select only ever offers sources already in the config --
    defining a new one is ``dbfresh add``'s job, not this screen's; with an
    empty config nothing is selectable here, which is the expected state
    for a project that hasn't defined a source yet.
    """

    TITLE = "Configure"

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
        self._existing_checks: list[Check] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Configure", classes="screen-heading")
        with Vertical(id="propose-section"):
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
                yield Button("Propose", id="propose-btn", variant="primary")
                yield Button(
                    "Accept", id="accept-btn", variant="success", disabled=True
                )
                yield Button("Cancel", id="cancel-btn")
            yield VerticalScroll(
                Static("", id="proposal-text", markup=False),
                Vertical(
                    Static(
                        "Existing checks for this object",
                        classes="section-title",
                    ),
                    Vertical(id="existing-checks"),
                    id="existing-section",
                    classes="panel",
                ),
                Vertical(
                    Static(
                        "Proposed checks (uncheck any to drop them)",
                        classes="section-title",
                    ),
                    Vertical(id="proposed-checks"),
                    id="proposed-section",
                    classes="panel",
                ),
                Vertical(
                    Static(
                        "Offered checks (check any to add them)",
                        classes="section-title",
                    ),
                    Vertical(id="offered-checks"),
                    id="offered-section",
                    classes="panel",
                ),
                id="proposal-scroll",
            )
        yield Footer()

    def on_mount(self) -> None:
        if len(self._config.sources) == 1:
            (name,) = self._config.sources
            self.query_one("#source-select", Select).value = name
        # The existing/proposed/offered panels have nothing to show until
        # a Propose click populates them -- an empty panel with just its
        # heading reads as broken rather than as "nothing here yet".
        self._set_sections_visible(False)
        self._update_object_suggester(self._selected_source_name())

    def _selected_source_name(self) -> str | None:
        """The currently-selected ``#source-select`` value, or ``None``
        when nothing is selected -- feeds the object suggester below."""
        value = self.query_one("#source-select", Select).value
        return None if value is Select.NULL else str(value)

    def _known_objects_for_source(self, source_name: str | None) -> list[str]:
        """Object names already configured for ``source_name`` -- the
        suggestion pool for ``#object-input``'s inline autocomplete,
        scoped to one source so objects belonging to other sources aren't
        noise. Empty (no suggestions, not an error) when no source is
        selected or it has no checks yet.
        """
        if source_name is None:
            return []
        return sorted(
            {c.object for c in self._config.checks if c.source == source_name}
        )

    def _update_object_suggester(self, source_name: str | None) -> None:
        objects = self._known_objects_for_source(source_name)
        self.query_one("#object-input", Input).suggester = SuggestFromList(
            objects, case_sensitive=True
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "source-select":
            return
        source_name = None if event.value is Select.NULL else str(event.value)
        self._update_object_suggester(source_name)

    def _set_sections_visible(self, visible: bool) -> None:
        section_ids = (
            "#existing-section",
            "#proposed-section",
            "#offered-section",
        )
        for section_id in section_ids:
            self.query_one(section_id).display = visible

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "propose-btn":
            self._propose()
        elif button_id == "accept-btn":
            self._accept()
        elif button_id == "cancel-btn":
            self.dismiss()

    def _reset_proposal(self) -> None:
        """Clear state and widgets left over from a previous Propose click."""
        self._proposed = []
        self._proposed_checkboxes = []
        self._proposed_value_inputs = []
        self._offered_blocks = []
        self._offered_checkboxes = []
        self._offered_value_inputs = []
        self._existing_checks = []
        self.query_one("#existing-checks", Vertical).remove_children()
        self.query_one("#proposed-checks", Vertical).remove_children()
        self.query_one("#offered-checks", Vertical).remove_children()
        self.query_one("#proposal-text", Static).update("")
        self.query_one("#accept-btn", Button).disabled = True
        self._set_sections_visible(False)

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
                    value=str(block["expect"]["max_lag"]),
                    id=f"proposed-value-{i}",
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
                    source_name,
                    object_name,
                    offer["column"],
                    metric,
                    has_calendar,
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

    def _mount_existing_checks(
        self, source_name: str, object_name: str
    ) -> None:
        """The object's already-written checks, read-only -- config is a
        file the user edits by hand, so this only shows what's there
        already (via :func:`~dbfresh.tui.dashboard.check_expectation_line`)
        for reference while reviewing the proposal below it, never a form
        to change one.
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
        for check in checks:
            container.mount(Static(check_expectation_line(check)))

    def _propose(self) -> None:
        """Validate the form, then hand introspection off to a worker thread.

        Everything here runs on the main thread and touches only cheap,
        already-in-memory state (widget values, ``self._config``) -- the
        network I/O (``create_adapter``, ``describe()`` via
        ``check_object_exists``) happens in :meth:`_propose_worker`
        instead, so a slow or unreachable source never blocks the UI. A
        source that isn't selected or doesn't exist is caught here, before
        a worker is even started, since neither needs a connection to
        detect.
        """
        select = self.query_one("#source-select", Select)
        if select.value is Select.NULL:
            self.notify("select a source", severity="error")
            return
        source_name = str(select.value)
        object_name = self.query_one("#object-input", Input).value.strip()
        timestamp_entered = self.query_one(
            "#timestamp-input", Input
        ).value.strip()

        source = self._config.sources.get(source_name)
        if source is None:
            self.notify(f"unknown source: {source_name!r}", severity="error")
            return

        self._reset_proposal()
        self.query_one("#propose-btn", Button).disabled = True
        self._propose_worker(
            source_name, object_name, timestamp_entered, source
        )

    @work(
        thread=True,
        exclusive=True,
        group=_PROPOSE_WORKER_GROUP,
        exit_on_error=False,
    )
    def _propose_worker(
        self,
        source_name: str,
        object_name: str,
        timestamp_entered: str,
        source: SourceConfig,
    ) -> _ProposeOutcome:
        """Introspect ``source_name``.``object_name`` off the Textual
        thread and build the proposal bundle from it.

        Runs on a worker thread (mirrors ``DbfreshApp._run_checks_worker``):
        ``create_adapter`` and ``describe()`` (via ``check_object_exists``)
        block on network I/O, which must never run on the UI thread. This
        method touches no widget -- it returns a plain-data
        :class:`_ProposeOutcome`; :meth:`on_worker_state_changed` does the
        actual mounting back on the main thread off of that return value.
        """
        try:
            adapter = create_adapter(
                source.type, source.params, timeout=source.timeout
            )
        except Exception as exc:
            return _ProposeOutcome(
                error=f"could not connect to {source_name!r}: {exc}"
            )

        try:
            existence = check_object_exists(adapter, object_name)
            if not existence.exists:
                return _ProposeOutcome(
                    error=f"object not found: {object_name!r} ({existence.error})"
                )
            # existence.exists is only True when describe() succeeded, which
            # is exactly when info is populated (see ExistenceCheck).
            assert existence.info is not None

            ambiguity_note = None
            timestamp_override = None
            timestamp = pick_timestamp_column(existence.info.columns)
            if timestamp.needs_choice:
                if timestamp_entered in timestamp.candidates:
                    timestamp_override = timestamp_entered
                else:
                    ambiguity_note = (
                        "ambiguous timestamp candidates: "
                        + ", ".join(timestamp.candidates)
                        + " -- enter one above and Propose again"
                    )

            has_calendar = self._config.calendar is not None
            proposed = propose_checks(
                source_name,
                object_name,
                existence.info,
                adapter.dialect,
                has_calendar=has_calendar,
                is_view=existence.info.is_view,
                timestamp_override=timestamp_override,
            )
            key_note = key_introspection_note(adapter.dialect, existence.info)
            offered_count = sum(
                len(offer["checks"])
                for offer in offered_column_checks(
                    existence.info.columns, proposed
                )
            )
        finally:
            adapter.close()

        notes: list[str] = []
        if key_note is not None:
            notes.append(key_note)
        if ambiguity_note is not None:
            notes.append(ambiguity_note)

        if not notes and not proposed and not offered_count:
            notes.append("no checks proposed")

        return _ProposeOutcome(
            source_name=source_name,
            object_name=object_name,
            columns=existence.info.columns,
            has_calendar=has_calendar,
            proposed=proposed,
            notes=notes,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up ``_propose_worker``'s outcome and mount widgets from it.

        Mirrors ``DbfreshApp.on_worker_state_changed``: the introspection
        already ran off-thread in ``_propose_worker``; every widget touch
        below -- mounting the existing/proposed/offered-checks sections,
        updating the notes panel and the Accept button -- runs here, back
        on the main thread, off of ``event.worker.result`` (or, on an
        unexpected exception, ``event.worker.error``) rather than from
        inside the worker itself.
        """
        if event.worker.group != _PROPOSE_WORKER_GROUP:
            return
        if event.state == WorkerState.RUNNING:
            self.sub_title = "proposing checks…"
            return
        if event.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            return

        self.sub_title = None
        self.query_one("#propose-btn", Button).disabled = False

        if event.state == WorkerState.CANCELLED:
            # Superseded by a later Propose click on this exclusive worker
            # group; the newer click already reset the widgets it cares
            # about, so there's nothing stale here to clean up.
            return
        if event.state == WorkerState.ERROR:
            self.notify(
                f"propose failed: {event.worker.error}", severity="error"
            )
            return

        outcome = event.worker.result
        assert outcome is not None
        if outcome.error is not None:
            self.notify(outcome.error, severity="error")
            return

        self._mount_existing_checks(outcome.source_name, outcome.object_name)
        self._has_calendar = outcome.has_calendar
        self._proposed = outcome.proposed
        self._mount_proposed_checkboxes()
        self._mount_offered_checkboxes(
            outcome.source_name,
            outcome.object_name,
            outcome.columns,
            outcome.has_calendar,
            outcome.proposed,
        )
        self.query_one("#proposal-text", Static).update(
            "\n".join(outcome.notes)
        )
        self.query_one("#accept-btn", Button).disabled = not self._proposed
        self._set_sections_visible(True)

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
                return (
                    None,
                    f"{column}: not a number for max null rate: {value!r}",
                )
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
            rebuilt, error = self._rebuild_proposed_check(
                block, value_input.value
            )
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
            rebuilt, error = self._rebuild_offered_check(
                block, value_input.value
            )
            if error is not None:
                errors.append(error)
                continue
            assert rebuilt is not None
            selected.append(rebuilt)
        return selected, errors

    def _accept(self) -> None:
        """Render the selected checks as YAML and show it in a modal to
        copy -- this screen never writes config itself. A selected block
        whose ``check_id`` already exists somewhere in the composed config
        (:func:`~dbfresh.configurator.partition_new_checks`) is left out of
        the rendered YAML and named in a warning instead: pasting it again
        would make the next config load raise on the duplicate id.
        """
        selected, errors = self._selected_checks()
        if errors:
            self.notify(
                "\n".join(errors),
                title="Invalid check value",
                severity="error",
            )
            return
        if not selected:
            return
        new_checks, already_defined = partition_new_checks(
            self._config_path, selected
        )
        if already_defined:
            self.notify(
                "\n".join(
                    f"already defined, not proposed again: {block}"
                    for block in already_defined
                ),
                title="Duplicate checks skipped",
                severity="warning",
            )
        if not new_checks:
            return
        self.app.push_screen(
            ProposalYamlScreen(
                self._config_path, render_proposal(None, new_checks)
            )
        )

    def action_cancel(self) -> None:
        self.dismiss()
