"""PruneImagesScreen: wizard to run prune-untagged-images.sh on a remote node via SSH."""

import os

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Static

from ..config import NODE_DOMAIN, TOOLS_DIR

_SCRIPT = "/opt/Lobot/tools/prune-untagged-images.sh"


class PruneImagesScreen(ModalScreen):
    """
    Wizard for prune-untagged-images.sh on a remote node.
    Check mode does a read-only scan; Prune mode removes safe (untagged, unreferenced) images.
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
            yield Static(
                f"SSH key auth required. If not yet set up:\n"
                f"  ssh-keygen -t ed25519          (skip if a key already exists)\n"
                f"  ssh-copy-id <user>@{self._fqdn}",
                classes="wizard-ssh-info",
            )
            yield Label(
                "[dim]Check[/] scans and reports candidates without removing anything.\n"
                "[dim]Prune[/] removes all untagged images not referenced by any container.",
                classes="wizard-field-label",
                markup=True,
            )

            yield Label("SSH user *", classes="wizard-field-label")
            yield Input(
                value=os.environ.get("USER", ""),
                placeholder="username",
                id="field-ssh-user",
                classes="wizard-input",
            )

            with Horizontal(id="wizard-buttons"):
                yield Button("Cancel  (q)", variant="error", id="btn-cancel")
                yield Button("Check  (c)", variant="default", id="btn-check")
                yield Button("Prune  (p)", variant="warning", id="btn-prune")

    def on_mount(self) -> None:
        self.query_one("#btn-cancel").focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-cancel":
            self.dismiss(None)
        elif event.button.id == "btn-check":
            self._do_run("--check")
        elif event.button.id == "btn-prune":
            self._do_run("--yes")

    def on_key(self, event) -> None:
        if isinstance(self.focused, Input):
            return
        if event.key == "c":
            self._do_run("--check")
        elif event.key == "p":
            self._do_run("--yes")

    def _do_run(self, flag: str) -> None:
        ssh_user = self.query_one("#field-ssh-user", Input).value.strip()
        if not ssh_user:
            self.query_one("#field-ssh-user", Input).focus()
            return

        host = f"{ssh_user}@{self._fqdn}"
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            host,
            "bash", _SCRIPT, flag,
        ]
        self.dismiss(argv)
