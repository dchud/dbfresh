"""Configure screen: introspect a source+object, propose, append YAML.

Reuses the front-end-agnostic ``configurator`` module exactly as
`dbfresh add` does -- the same introspection, proposal, and YAML-emission
functions; only the prompt/rendering layer differs. Never writes to the
observation store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
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
from dbfresh.adapters.factory import supported_types
from dbfresh.checks import Check, check_id, parse_duration
from dbfresh.config import Config, SourceConfig
from dbfresh.configurator import (
    add_source,
    append_checks,
    build_offered_check,
    check_object_exists,
    find_check_file,
    key_introspection_note,
    offered_column_checks,
    pick_timestamp_column,
    probe_new_source,
    propose_checks,
    raw_source,
    remove_source,
    rewrite_check_expectation,
    rewrite_source,
    target_files,
)
from dbfresh.tui.dashboard import check_label

_PROPOSE_WORKER_GROUP = "propose"
_NEW_SOURCE_WORKER_GROUP = "new-source"

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
    target_file: Path | None = None


def _id_part(value: str) -> str:
    """A CSS-identifier-safe fragment for a dynamic widget id."""
    return _UNSAFE_ID_CHARS.sub("_", value)


def _parse_params_text(text: str) -> dict[str, str]:
    """Parse the new-source form's ``key=value`` lines into a params dict.

    Mirrors the CLI wizard's own key=value prompt loop (``cli._select_source``)
    line for line -- blank lines are ignored, and a line with no ``=`` yields
    an empty value rather than raising, matching ``str.partition``'s own
    behavior there.
    """
    params: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        key, _sep, value = line.partition("=")
        params[key.strip()] = value.strip()
    return params


def _params_to_text(params: dict[str, str]) -> str:
    """Inverse of :func:`_parse_params_text`: one ``key=value`` per line --
    pre-fills the new-source form's params TextArea in edit mode from
    :func:`~dbfresh.configurator.raw_source`'s raw (``${VAR}``-intact)
    params, so a secret shows as the token the user actually typed rather
    than a resolved value.
    """
    return "\n".join(f"{key}={value}" for key, value in params.items())


@dataclass
class _NewSourceOutcome:
    """What :meth:`ConfigureScreen._new_source_worker` hands back to the
    main thread.

    ``error`` set means the probe failed and nothing was written to disk.
    Otherwise ``name``/``type_`` and ``params`` describe the source
    :func:`add_source` (or, in edit mode, :func:`rewrite_source`) already
    wrote. ``params`` here are the ${VAR}-resolved params, not the raw ones
    written to the YAML: the main thread builds an in-memory ``SourceConfig``
    from them so the source is immediately selectable -- and immediately
    connectable, with real values rather than a literal "${VAR}", the same
    values a later config reload would resolve -- without a full config
    reload. ``edited`` distinguishes an edit (source already existed) from
    a brand-new add, since the two notify and dirty the "config changed"
    signal slightly differently.
    """

    error: str | None = None
    name: str = ""
    type_: str = ""
    params: dict = field(default_factory=dict)
    edited: bool = False


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

    Also covers adding a brand-new source (see :meth:`_add_new_source`) --
    reached via the "+ new source" button, or automatically when
    ``config.sources`` starts out empty, since the propose form has nothing
    to offer until at least one source exists.

    Dismisses with ``True`` when it wrote checks to disk (so Home reloads
    the config and refreshes the dashboard), ``False`` otherwise.
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
        self._target_file: Path | None = None
        self._existing_checks: list[Check] = []
        self._existing_value_inputs: list[Input | None] = []
        self._config_changed = False
        # None in "add a new source" mode; the source's own name while the
        # new-source form is repurposed for editing it instead (see
        # _edit_selected_source).
        self._source_form_edit_name: str | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Configure", classes="screen-heading")
        with Vertical(id="propose-section"):
            yield Label("Source name")
            with Horizontal():
                yield Select(
                    [(name, name) for name in sorted(self._config.sources)],
                    id="source-select",
                )
                yield Button("+ new source", id="new-source-btn")
                yield Button("Edit source", id="edit-source-btn", disabled=True)
                yield Button("Remove source", id="remove-source-btn", disabled=True)
            remove_source_confirm_row = Horizontal(
                Label("remove this source permanently?", classes="hint"),
                Button("Confirm remove", id="remove-source-confirm-btn"),
                Button("Cancel", id="remove-source-cancel-btn"),
                id="remove-source-confirm-row",
            )
            remove_source_confirm_row.display = False
            yield remove_source_confirm_row
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
                    Static("Existing checks for this object", classes="section-title"),
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
        with Vertical(id="new-source-form", classes="panel"):
            yield Static("Add a new source", id="new-source-heading")
            yield Label("Source name")
            yield Input(id="new-source-name-input")
            yield Label("Source type")
            yield Select(
                [(t, t) for t in supported_types()],
                id="new-source-type-select",
                prompt="Select a source type",
            )
            yield Label(
                "Connection params, one key=value per line (blank lines ignored)"
            )
            yield Static(
                "tip: use key=${VAR} for secrets to keep them out of the YAML",
                classes="hint",
            )
            yield TextArea(id="new-source-params", placeholder="database=/path/to.db")
            with Horizontal():
                yield Button("Probe & add", id="new-source-add-btn", variant="primary")
                yield Button("Cancel", id="new-source-cancel-btn")
        yield Footer()

    def on_mount(self) -> None:
        # A brand-new project has no sources at all -- the propose form
        # would just be a dead end (an empty Select, nothing to Propose
        # against), so open straight into the new-source form instead of
        # making the user notice and click "+ new source" themselves.
        self._show_new_source_form(not self._config.sources)
        if len(self._config.sources) == 1:
            (name,) = self._config.sources
            self.query_one("#source-select", Select).value = name
        # The existing/proposed/offered panels have nothing to show until
        # a Propose click populates them -- an empty panel with just its
        # heading reads as broken rather than as "nothing here yet".
        self._set_sections_visible(False)
        self._update_object_suggester(self._selected_source_name())
        self._update_source_buttons_state()

    def _selected_source_name(self) -> str | None:
        """The currently-selected ``#source-select`` value, or ``None``
        when nothing is selected -- shared by the object suggester and the
        Edit/Remove-source affordances, both of which act on whatever is
        currently selected."""
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

    def _update_source_buttons_state(self) -> None:
        """Edit/Remove act on whatever's selected in ``#source-select`` --
        disabled with nothing selected (a zero-sources project, or right
        after removing the last remaining source)."""
        has_selection = self._selected_source_name() is not None
        self.query_one("#edit-source-btn", Button).disabled = not has_selection
        self.query_one("#remove-source-btn", Button).disabled = not has_selection

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "source-select":
            return
        source_name = None if event.value is Select.NULL else str(event.value)
        self._update_object_suggester(source_name)
        self._update_source_buttons_state()

    def _set_sections_visible(self, visible: bool) -> None:
        section_ids = ("#existing-section", "#proposed-section", "#offered-section")
        for section_id in section_ids:
            self.query_one(section_id).display = visible

    def _show_new_source_form(self, visible: bool) -> None:
        """Toggle between the new-source form and the propose form -- only
        one is ever shown at a time. See :meth:`on_mount` for why zero
        sources opens here automatically; otherwise it's reached any time
        via the "+ new source" button beside the source Select, or (in
        edit mode) via :meth:`_edit_selected_source`, which overwrites
        the blank-form defaults set here with the source's own current
        values right after calling this.
        """
        self.query_one("#new-source-form", Vertical).display = visible
        self.query_one("#propose-section", Vertical).display = not visible
        if visible:
            self._source_form_edit_name = None
            self.query_one("#new-source-heading", Static).update("Add a new source")
            self.query_one("#new-source-add-btn", Button).label = "Probe & add"
            name_input = self.query_one("#new-source-name-input", Input)
            name_input.value = ""
            name_input.disabled = False
            type_select = self.query_one("#new-source-type-select", Select)
            # Reset to exactly the factory's supported types -- clears any
            # extra legacy-type option _edit_selected_source may have
            # appended for a prior edit session (see there).
            type_select.set_options((t, t) for t in supported_types())
            type_select.value = Select.NULL
            self.query_one("#new-source-params", TextArea).text = ""

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "propose-btn":
            self._propose()
        elif button_id == "accept-btn":
            self._accept()
        elif button_id == "cancel-btn":
            self.dismiss(self._config_changed)
        elif button_id == "new-source-btn":
            self._show_new_source_form(True)
        elif button_id == "new-source-add-btn":
            self._add_new_source()
        elif button_id == "new-source-cancel-btn":
            self._show_new_source_form(False)
        elif button_id == "edit-source-btn":
            self._edit_selected_source()
        elif button_id == "remove-source-btn":
            self._arm_remove_source()
        elif button_id == "remove-source-confirm-btn":
            self._confirm_remove_source()
        elif button_id == "remove-source-cancel-btn":
            self._cancel_remove_source()
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
                    # Label (width:auto), not Static (which fills the row
                    # and pushes the Input + Save button off the right edge).
                    Label(f"{label} ({check.expect.operator}):"),
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
        self._config_changed = True
        self.notify(f"saved {check_label(check)}")

    def _edit_selected_source(self) -> None:
        """Repurpose the new-source form to edit the currently-selected
        source instead of adding one: pre-fills the type and params from
        :func:`raw_source` (so a ``${VAR}`` secret shows as the token,
        never a resolved value), disables the name Input (the name is the
        entry's identity here -- no rename), and routes the submit button
        to :meth:`_new_source_worker` in edit mode (see
        :attr:`_source_form_edit_name`).
        """
        name = self._selected_source_name()
        if name is None:
            return
        try:
            type_, params = raw_source(self._config_path, name)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return

        self._show_new_source_form(True)
        self._source_form_edit_name = name
        self.query_one("#new-source-heading", Static).update(f"Edit source: {name}")
        self.query_one("#new-source-add-btn", Button).label = "Probe & save"
        name_input = self.query_one("#new-source-name-input", Input)
        name_input.value = name
        name_input.disabled = True
        type_select = self.query_one("#new-source-type-select", Select)
        if type_ not in supported_types():
            # A hand-written or legacy type the factory doesn't (or no
            # longer) recognize -- surface it as an extra option instead
            # of silently blanking out a working source's type, or
            # crashing on a value Select doesn't consider legal. Probing
            # it will still fail as "unknown source type" if left as-is,
            # same as before this dropdown existed.
            type_select.set_options(
                [(t, t) for t in supported_types()] + [(type_, type_)]
            )
        type_select.value = type_
        self.query_one("#new-source-params", TextArea).text = _params_to_text(params)

    def _arm_remove_source(self) -> None:
        """First press of "Remove source": reveal the confirm/cancel row
        rather than removing outright -- a stray click must never drop a
        source (mirrors ``ObjectDetailScreen``'s delete-check confirm)."""
        if self._selected_source_name() is None:
            return
        self.query_one("#remove-source-confirm-row", Horizontal).display = True

    def _cancel_remove_source(self) -> None:
        self.query_one("#remove-source-confirm-row", Horizontal).display = False

    def _confirm_remove_source(self) -> None:
        """Second press: actually remove the selected source on disk via
        :func:`remove_source`, which refuses (raising ``ValueError``,
        writing nothing) when any check still references it. On success,
        drops the source from the Select and ``self._config`` and selects
        whatever remains -- or, with nothing left, reopens the new-source
        form, mirroring the zero-sources start-up case in :meth:`on_mount`.
        """
        self.query_one("#remove-source-confirm-row", Horizontal).display = False
        name = self._selected_source_name()
        if name is None:
            return
        try:
            remove_source(self._config_path, name)
        except ValueError as exc:
            self.notify(str(exc), severity="error")
            return

        del self._config.sources[name]
        self._config_changed = True
        self._reset_proposal()
        remaining = sorted(self._config.sources)
        select = self.query_one("#source-select", Select)
        select.set_options((n, n) for n in remaining)
        if remaining:
            select.value = remaining[0]
        else:
            self._show_new_source_form(True)
        self._update_source_buttons_state()
        self.notify(f"removed source {name!r}")

    def _add_new_source(self) -> None:
        """Validate the new-source form, then hand the connection probe
        (and, on success, the disk write) off to a worker thread.

        Mirrors :meth:`_propose`: everything here touches only cheap,
        already-in-memory state (widget values, ``self._config``); the
        network I/O (``probe_new_source``) and the config write
        (``add_source``/``rewrite_source``) both happen in
        :meth:`_new_source_worker` instead, so neither ever blocks the UI
        thread. :attr:`_source_form_edit_name` set means this is an edit
        of an already-existing source rather than adding a new one -- see
        :meth:`_edit_selected_source`.
        """
        name = self.query_one("#new-source-name-input", Input).value.strip()
        type_select = self.query_one("#new-source-type-select", Select)
        params_text = self.query_one("#new-source-params", TextArea).text
        edit_name = self._source_form_edit_name

        if not name:
            self.notify("enter a source name", severity="error")
            return
        if edit_name is None and name in self._config.sources:
            self.notify(f"source already exists: {name!r}", severity="error")
            return
        if type_select.value is Select.NULL:
            self.notify("select a source type", severity="error")
            return
        type_ = str(type_select.value)

        params = _parse_params_text(params_text)
        self.query_one("#new-source-add-btn", Button).disabled = True
        self._new_source_worker(name, type_, params, edit_name)

    @work(
        thread=True, exclusive=True, group=_NEW_SOURCE_WORKER_GROUP, exit_on_error=False
    )
    def _new_source_worker(
        self, name: str, type_: str, params: dict, edit_name: str | None = None
    ) -> _NewSourceOutcome:
        """Probe the (new or edited) source's connection and, only once it
        succeeds, write it to disk.

        Runs on a worker thread (mirrors :meth:`_propose_worker`):
        ``probe_new_source`` blocks on network I/O and ``add_source``/
        ``rewrite_source`` block on disk I/O, neither of which may run on
        the UI thread. This method touches no widget and never mutates
        ``self._config`` -- it returns a plain-data
        :class:`_NewSourceOutcome`; :meth:`on_worker_state_changed`
        reflects the source into ``self._config`` and the Select back on
        the main thread.
        """
        probe, resolved_params = probe_new_source(type_, params)
        if not probe.ok:
            return _NewSourceOutcome(
                error=f"could not connect to {name!r}: {probe.error}"
            )
        if edit_name is not None:
            rewrite_source(self._config_path, edit_name, type_, params)
            return _NewSourceOutcome(
                name=name, type_=type_, params=resolved_params, edited=True
            )
        # add_source writes the raw params -- ${VAR} secrets stay ${VAR} in
        # the YAML -- but the in-memory SourceConfig the main thread builds
        # from this outcome must carry the resolved params, so an immediate
        # Propose against the new source connects with real values rather
        # than a literal "${VAR}". That matches what a later config reload
        # produces: load_config resolves ${VAR} at load time, so an existing
        # source's params are already resolved in memory.
        add_source(self._config_path, name, type_, params)
        return _NewSourceOutcome(name=name, type_=type_, params=resolved_params)

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
        timestamp_entered = self.query_one("#timestamp-input", Input).value.strip()

        source = self._config.sources.get(source_name)
        if source is None:
            self.notify(f"unknown source: {source_name!r}", severity="error")
            return

        self._reset_proposal()
        self.query_one("#propose-btn", Button).disabled = True
        self._propose_worker(source_name, object_name, timestamp_entered, source)

    @work(thread=True, exclusive=True, group=_PROPOSE_WORKER_GROUP, exit_on_error=False)
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
        from dbfresh.adapters.factory import create_adapter

        try:
            adapter = create_adapter(source.type, source.params, timeout=source.timeout)
        except Exception as exc:
            return _ProposeOutcome(error=f"could not connect to {source_name!r}: {exc}")

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
                for offer in offered_column_checks(existence.info.columns, proposed)
            )
        finally:
            adapter.close()

        notes: list[str] = []
        if key_note is not None:
            notes.append(key_note)
        if ambiguity_note is not None:
            notes.append(ambiguity_note)

        files = target_files(self._config_path)
        target_file = files[0] if files else self._config_path
        if len(files) > 1:
            notes.append(f"writing to {target_file} (of {len(files)} included files)")

        if not notes and not proposed and not offered_count:
            notes.append("no checks proposed")

        return _ProposeOutcome(
            source_name=source_name,
            object_name=object_name,
            columns=existence.info.columns,
            has_calendar=has_calendar,
            proposed=proposed,
            notes=notes,
            target_file=target_file,
        )

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Route a worker's state change to its own handler by group --
        this screen runs two independent exclusive worker groups (Propose,
        new-source), each with its own busy state and outcome shape."""
        if event.worker.group == _PROPOSE_WORKER_GROUP:
            self._on_propose_worker_state_changed(event)
        elif event.worker.group == _NEW_SOURCE_WORKER_GROUP:
            self._on_new_source_worker_state_changed(event)

    def _on_propose_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up ``_propose_worker``'s outcome and mount widgets from it.

        Mirrors ``DbfreshApp.on_worker_state_changed``: the introspection
        already ran off-thread in ``_propose_worker``; every widget touch
        below -- mounting the existing/proposed/offered-checks sections,
        updating the notes panel and the Accept button -- runs here, back
        on the main thread, off of ``event.worker.result`` (or, on an
        unexpected exception, ``event.worker.error``) rather than from
        inside the worker itself.
        """
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
            self.notify(f"propose failed: {event.worker.error}", severity="error")
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
        self._target_file = outcome.target_file
        self.query_one("#proposal-text", Static).update("\n".join(outcome.notes))
        self.query_one("#accept-btn", Button).disabled = not self._proposed
        self._set_sections_visible(True)

    def _on_new_source_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Pick up ``_new_source_worker``'s outcome: reflect a successful
        probe+write into ``self._config`` and the source Select, or surface
        a failed probe as an error toast with the form left open to retry.

        Mirrors :meth:`_on_propose_worker_state_changed`: the probe (and,
        on success, the disk write) already ran off-thread in
        ``_new_source_worker``; every widget/``self._config`` touch below
        runs here, back on the main thread, off of ``event.worker.result``.
        """
        if event.state == WorkerState.RUNNING:
            self.sub_title = (
                "saving source…" if self._source_form_edit_name else "adding source…"
            )
            return
        if event.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            return

        self.sub_title = None
        self.query_one("#new-source-add-btn", Button).disabled = False

        if event.state == WorkerState.CANCELLED:
            # Superseded by a later Probe & add click on this exclusive
            # worker group -- the newer click already reset the form.
            return
        if event.state == WorkerState.ERROR:
            verb = "save" if self._source_form_edit_name else "add"
            self.notify(
                f"could not {verb} source: {event.worker.error}", severity="error"
            )
            return

        outcome = event.worker.result
        assert outcome is not None
        if outcome.error is not None:
            self.notify(outcome.error, severity="error")
            return

        self._config.sources[outcome.name] = SourceConfig(
            name=outcome.name, type=outcome.type_, params=outcome.params
        )
        select = self.query_one("#source-select", Select)
        select.set_options((name, name) for name in sorted(self._config.sources))
        select.value = outcome.name
        self._show_new_source_form(False)
        self._update_source_buttons_state()
        if outcome.edited:
            self._config_changed = True
            self.notify(f"updated source {outcome.name!r}")
        else:
            self.notify(f"added source {outcome.name!r}")

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
            self.notify(
                "\n".join(errors), title="Invalid check value", severity="error"
            )
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
        self.dismiss(self._config_changed)
