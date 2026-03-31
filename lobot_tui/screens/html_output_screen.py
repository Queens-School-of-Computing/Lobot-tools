"""HtmlOutputScreen: read-only viewer for a generated resource HTML file."""

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
        Binding("t", "terminal_view", "View in terminal", priority=True),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        yield TricolourStripe("▄")
        yield Label(
            f"[bold cyan]Resource HTML[/]  [dim]{self._path}[/]  "
            f"[dim]  (t) view in terminal for copy    (q) close[/]",
            markup=True,
            id="html-title",
        )
        try:
            content = Path(self._path).read_text()
        except Exception as exc:
            content = f"Error reading {self._path}: {exc}"
        yield TextArea(content, id="html-content", read_only=True)

    def on_mount(self) -> None:
        self.query_one("#html-content", TextArea).focus()

    def action_terminal_view(self) -> None:
        try:
            content = Path(self._path).read_text()
        except Exception as exc:
            self.notify(f"Error reading file: {exc}", severity="error", timeout=4)
            return
        with self.app.suspend():
            print(content)
            print("\033[2m--- Press Enter to return to lobot-tui ---\033[0m", flush=True)
            input()
