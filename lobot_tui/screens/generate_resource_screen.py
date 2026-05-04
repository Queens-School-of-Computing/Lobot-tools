"""GenerateResourceScreen: wizard to run generate-resource-page.py on a remote node."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from ..config import NODE_DOMAIN, TOOLS_DIR


class GenerateResourceScreen(ModalScreen):
    """
    Input wizard for generate-resource-page.py --host.
    Reads CPU/RAM/GPU from the target node via SSH and prints HTML to the jobs screen.
    Dismisses with argv list on run, or None on cancel.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("q", "cancel", "Cancel", priority=True),
    ]

    def __init__(self, node_name: str) -> None:
        super().__init__()
        self._node_name = node_name
        self._fqdn = node_name if "." in node_name else f"{node_name}.{NODE_DOMAIN}"

    def action_cancel(self) -> None:
        self.dismiss(None)

    def compose(self) -> ComposeResult:
        with Vertical(id="wizard-dialog"):
            yield Label("[bold cyan]Generate Resource HTML[/]", id="wizard-title", markup=True)
            yield Label(
                f"Node: [bold]{self._node_name}[/]  →  SSH host: [cyan]{self._fqdn}[/]",
                classes="wizard-field-label",
                markup=True,
            )

            yield Label("Lab name *", classes="wizard-field-label")
            yield Input(
                value=self._node_name,
                placeholder="e.g. fz2",
                id="field-labname",
                classes="wizard-input",
            )

            yield Label("SSH user *", classes="wizard-field-label")
            yield Input(
                value="croot",
                placeholder="username",
                id="field-ssh-user",
                classes="wizard-input",
            )

            yield Label("SSH key setup (if needed — select to copy)", classes="wizard-field-label")
            yield Input(value="ssh-keygen -t ed25519", id="ssh-cmd-keygen", classes="wizard-ssh-cmd")
            yield Input(
                value=f"ssh-copy-id croot@{self._fqdn}",
                id="ssh-cmd-copyid",
                classes="wizard-ssh-cmd",
            )

            with Horizontal(id="wizard-buttons"):
                yield Button("Cancel  (q)", variant="error", id="btn-cancel")
                yield Button("Run  (r)", variant="success", id="btn-run")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel").focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "field-ssh-user":
            user = event.value.strip() or "croot"
            try:
                self.query_one("#ssh-cmd-copyid", Input).value = f"ssh-copy-id {user}@{self._fqdn}"
            except Exception:
                pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-run":
            self._do_run()

    def on_key(self, event) -> None:
        if event.key == "r" and not isinstance(self.focused, Input):
            self._do_run()

    def _do_run(self) -> None:
        labname = self.query_one("#field-labname", Input).value.strip()
        ssh_user = self.query_one("#field-ssh-user", Input).value.strip()

        if not labname:
            self.query_one("#field-labname", Input).focus()
            return
        if not ssh_user:
            self.query_one("#field-ssh-user", Input).focus()
            return

        host = f"{ssh_user}@{self._fqdn}"
        output_path = f"/tmp/lobot-resource-{labname}.html"
        argv = [
            "python3",
            str(TOOLS_DIR / "generate-resource-page.py"),
            "--lab", labname,
            "--host", host,
            "--output", output_path,
            "--yes",
        ]
        self.dismiss((argv, output_path))
