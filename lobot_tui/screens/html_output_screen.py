"""HtmlOutputScreen: read-only viewer for a generated resource HTML file."""

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Label, RichLog

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
        yield TricolourStripe("▄")
        yield Label(
            f"[bold cyan]Resource HTML[/]  [dim]{self._path}[/]  "
            f"[dim]  click+drag to select, ctrl+c to copy    (q) close[/]",
            markup=True,
            id="html-title",
        )
        yield RichLog(id="html-content", highlight=False, markup=False, wrap=False)

    def on_mount(self) -> None:
        log = self.query_one("#html-content", RichLog)
        try:
            for line in Path(self._path).read_text().splitlines():
                log.write(line)
        except Exception as exc:
            log.write(f"Error reading {self._path}: {exc}")
