"""HtmlOutputScreen: read-only viewer for a generated resource HTML file."""

import socket
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Label, TextArea

from ..widgets.tricolour_stripe import TricolourStripe


class HtmlOutputScreen(Screen):
    """Displays the raw content of a generated spawn-form HTML file."""

    BINDINGS = [
        Binding("q", "dismiss", "Close"),
        Binding("escape", "dismiss", "Close"),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        hostname = socket.gethostname()
        yield TricolourStripe("▄")
        yield Label(
            f"[bold cyan]Resource HTML[/]  [dim](q) close[/]",
            markup=True,
            id="html-title",
        )
        yield Label(
            f"[dim]To copy to your local clipboard:[/]\n"
            f"  ssh {hostname} cat {self._path} | pbcopy",
            id="html-copy-hint",
        )
        try:
            content = Path(self._path).read_text()
        except Exception as exc:
            content = f"Error reading {self._path}: {exc}"
        yield TextArea(content, id="html-content", read_only=True)

    def on_mount(self) -> None:
        ta = self.query_one("#html-content", TextArea)
        ta.focus()
        ta.select_all()
