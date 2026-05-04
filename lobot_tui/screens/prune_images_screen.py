"""PruneImagesScreen: wizard to run prune-untagged-images.sh on a remote node via SSH."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label

from ..config import NODE_DOMAIN, TOOLS_DIR

_SCRIPT = str(TOOLS_DIR / "prune-untagged-images.sh")


class PruneImagesScreen(ModalScreen):
    """
    Wizard for prune-untagged-images.sh on a remote node.
    Check-only mode scans without removing; unchecked removes safe untagged images.
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
            yield Label("[bold cyan]Prune Untagged Images[/]", id="wizard-title", markup=True)
            yield Label(
                f"Node: [bold]{self._node_name}[/]  →  SSH host: [cyan]{self._fqdn}[/]",
                classes="wizard-field-label",
                markup=True,
            )

            yield Label("SSH user *", classes="wizard-field-label")
            yield Input(
                value="croot",
                placeholder="username",
                id="field-ssh-user",
                classes="wizard-input",
            )

            yield Label("SSH key setup (if needed)", classes="wizard-field-label")
            with Horizontal(classes="wizard-ssh-row"):
                yield Input(
                    value="ssh-keygen -t ed25519",
                    id="ssh-cmd-keygen",
                    classes="wizard-ssh-cmd",
                )
                yield Button("Copy", id="btn-copy-keygen", classes="wizard-ssh-copy")
            with Horizontal(classes="wizard-ssh-row"):
                yield Input(
                    value=f"ssh-copy-id croot@{self._fqdn}",
                    id="ssh-cmd-copyid",
                    classes="wizard-ssh-cmd",
                )
                yield Button("Copy", id="btn-copy-copyid", classes="wizard-ssh-copy")

            with Horizontal(classes="wizard-checkbox-row"):
                yield Checkbox(
                    "Check only",
                    value=True,
                    id="cb-check-only",
                    classes="wizard-checkbox",
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
        bid = event.button.id
        if bid == "btn-cancel":
            self.dismiss(None)
        elif bid == "btn-run":
            self._do_run()
        elif bid == "btn-copy-keygen":
            self._copy(self.query_one("#ssh-cmd-keygen", Input).value)
        elif bid == "btn-copy-copyid":
            self._copy(self.query_one("#ssh-cmd-copyid", Input).value)

    def _copy(self, text: str) -> None:
        self.app.copy_to_clipboard(text)
        self.notify("Copied to clipboard", timeout=2)

    def on_key(self, event) -> None:
        if event.key in ("enter", "space") and isinstance(self.focused, Button):
            self.focused.press()
            event.stop()
        elif event.key == "r" and not isinstance(self.focused, Input):
            self._do_run()

    def _do_run(self) -> None:
        ssh_user = self.query_one("#field-ssh-user", Input).value.strip()
        if not ssh_user:
            self.query_one("#field-ssh-user", Input).focus()
            return

        check_only = self.query_one("#cb-check-only", Checkbox).value
        flag = "--check" if check_only else "--yes"
        host = f"{ssh_user}@{self._fqdn}"
        argv = [
            "bash", "-c",
            f"ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new {host} bash -s -- {flag} < {_SCRIPT}",
        ]
        self.dismiss(argv)
