"""Configure screen: introspect a source+object, propose, append YAML.

Reuses the front-end-agnostic ``configurator`` module exactly as
`dbfresh add` does -- the same introspection, proposal, and YAML-emission
functions; only the prompt/rendering layer differs. Never writes to the
observation store.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from dbfresh.config import Config
from dbfresh.configurator import (
    append_checks,
    check_object_exists,
    propose_checks,
    target_files,
)


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

    def compose(self) -> ComposeResult:
        yield Header()
        yield Label("Source name")
        yield Input(id="source-input")
        yield Label("Object name")
        yield Input(id="object-input")
        with Horizontal():
            yield Button("Propose", id="propose-btn")
            yield Button("Accept", id="accept-btn", disabled=True)
            yield Button("Cancel", id="cancel-btn")
        yield VerticalScroll(Static("", id="proposal-text", markup=False))
        yield Footer()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "propose-btn":
            self._propose()
        elif event.button.id == "accept-btn":
            self._accept()
        elif event.button.id == "cancel-btn":
            self.dismiss(False)

    def _propose(self) -> None:
        from dbfresh.adapters.factory import create_adapter

        proposal_widget = self.query_one("#proposal-text", Static)
        accept_button = self.query_one("#accept-btn", Button)
        self._proposed = []
        accept_button.disabled = True

        source_name = self.query_one("#source-input", Input).value.strip()
        object_name = self.query_one("#object-input", Input).value.strip()

        source = self._config.sources.get(source_name)
        if source is None:
            proposal_widget.update(f"unknown source: {source_name!r}")
            return

        adapter = create_adapter(source.type, source.params)
        try:
            existence = check_object_exists(adapter, object_name)
            if not existence.exists:
                proposal_widget.update(
                    f"object not found: {object_name!r} ({existence.error})"
                )
                return
            has_calendar = self._config.calendar is not None
            self._proposed = propose_checks(
                source_name,
                object_name,
                existence.info,
                adapter.dialect,
                has_calendar=has_calendar,
            )
        finally:
            adapter.close()

        lines = [f"{c['metric']}: {c['expect']}" for c in self._proposed]
        proposal_widget.update("\n".join(lines) if lines else "no checks proposed")
        accept_button.disabled = not self._proposed

    def _accept(self) -> None:
        if not self._proposed:
            return
        target = target_files(self._config_path)[0]
        append_checks(target, self._proposed, config_path=self._config_path)
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
