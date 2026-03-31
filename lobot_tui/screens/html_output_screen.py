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
        Binding("y", "copy_html", "Copy to clipboard"),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self._path = path

    def compose(self) -> ComposeResult:
        yield TricolourStripe("▄")
        yield Label(
            f"[bold cyan]Resource HTML[/]  [dim]{self._path}[/]  "
            f"[dim]  (y) copy to clipboard    (q) close[/]",
            markup=True,
            id="html-title",
        )
        yield RichLog(id="html-content", highlight=False, markup=False, wrap=False)

    def action_copy_html(self) -> None:
        try:
            content = Path(self._path).read_text()
            self.app.copy_to_clipboard(content)
            self.notify("HTML copied to clipboard", timeout=2)
        except Exception as exc:
            self.notify(f"Copy failed: {exc}", severity="error", timeout=4)

    def on_mount(self) -> None:
        log = self.query_one("#html-content", RichLog)
        try:
            content = Path(self._path).read_text()
            for line in content.splitlines():
                log.write(line)
        except Exception as exc:
            log.write(f"Error reading {self._path}: {exc}")
