"""StorageStewardshipScreen: PVC/Longhorn volume report with delete."""

import asyncio
import json
from datetime import datetime, timezone

import yaml
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Label

from ..config import REPO_DIR, LONGHORN_NAMESPACE
from ..data import command_log
from ..widgets.tricolour_stripe import TricolourStripe

_RUNTIME_SETTING_YAML = REPO_DIR / "runtime_setting.yaml"


def _human_size(num_bytes: int) -> str:
    if not num_bytes:
        return "0B"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


def _days_since(ts: str) -> int | None:
    if not ts or ts in ("unknown", "0001-01-01T00:00:00Z"):
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - dt).days


def _load_username_to_lab() -> dict:
    """Invert runtime_setting.yaml's nodeaccess map: username -> lab."""
    try:
        data = yaml.safe_load(_RUNTIME_SETTING_YAML.read_text()) or {}
    except Exception:
        return {}
    mapping: dict = {}
    for lab, users in (data.get("nodeaccess") or {}).items():
        for user in users or []:
            mapping[str(user).strip().lower()] = lab
    return mapping


class StorageStewardshipScreen(Screen):
    """Lists PVCs with Longhorn last-activity info; select a row to delete."""

    BINDINGS = [
        Binding("escape", "go_back", "Back", priority=True),
        Binding("q", "go_back", "Back", priority=True),
        Binding("delete", "delete_pvc", "Delete"),
        Binding("x", "delete_pvc", "Delete"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[dict] = []
        self._pending_key: str | None = None
        self._pending_timer = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="screen-header"):
            yield Label(
                " [bold cyan]STORAGE STEWARDSHIP[/]  PVC / Longhorn volume report  "
                "[dim][x/Delete] delete (press twice)  [Esc/q] back[/]",
                id="screen-header-main",
                markup=True,
            )
            yield Label("", id="top-bar-cat", markup=False)
        yield TricolourStripe("▄")
        yield DataTable(id="storage-table", cursor_type="row", zebra_stripes=True)
        yield Label("[dim]Loading PVCs…[/]", id="screen-footer", markup=True)

    def on_mount(self) -> None:
        table = self.query_one("#storage-table", DataTable)
        table.add_columns(
            "USER", "LAB", "PVC", "SIZE", "USED", "STATE",
            "ROBUSTNESS", "LAST USED", "IDLE",
        )
        self.run_worker(self._load(), exclusive=True)

    async def _kubectl_json(self, *args: str) -> dict:
        proc = await asyncio.create_subprocess_exec(
            "kubectl", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        command_log.record(" ".join(["kubectl", *args]), [], proc.returncode)
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace").strip() or "kubectl failed")
        return json.loads(stdout.decode(errors="replace"))

    async def _load(self) -> None:
        table = self.query_one("#storage-table", DataTable)
        footer = self.query_one("#screen-footer", Label)

        try:
            pvc_data, lh_data = await asyncio.gather(
                self._kubectl_json("get", "pvc", "-A", "-o", "json"),
                self._kubectl_json(
                    "get", "volumes.longhorn.io", "-n", LONGHORN_NAMESPACE, "-o", "json"
                ),
            )
        except Exception as ex:
            footer.update(f"[red]Error loading storage data: {ex}[/]")
            return

        lh_by_name = {item["metadata"]["name"]: item for item in lh_data.get("items", [])}
        username_to_lab = _load_username_to_lab()

        rows = []
        for item in pvc_data.get("items", []):
            name = item["metadata"]["name"]
            if not name.startswith("claim-"):
                continue
            namespace = item["metadata"]["namespace"]
            username = name[len("claim-"):].lower()
            pv_name = item["spec"].get("volumeName", "")
            lh = lh_by_name.get(pv_name, {})
            status = lh.get("status", {})
            k8s_status = status.get("kubernetesStatus", {})

            size_bytes = int(status.get("actualSize") or 0)
            spec_size = int(lh.get("spec", {}).get("size") or 0)
            state = status.get("state", "unknown")
            robustness = status.get("robustness", "unknown")
            last_pod_ref = k8s_status.get("lastPodRefAt") or ""

            idle_days = _days_since(last_pod_ref)
            if state == "attached":
                idle_label = "active"
                sort_key = -1
            elif idle_days is None:
                idle_label = "unknown"
                sort_key = 10**9
            else:
                idle_label = f"{idle_days}d"
                sort_key = idle_days

            rows.append({
                "username": username,
                "lab": username_to_lab.get(username, "—"),
                "pvc": name,
                "namespace": namespace,
                "pv": pv_name,
                "size": _human_size(spec_size),
                "used": _human_size(size_bytes),
                "state": state,
                "robustness": robustness,
                "last_used": last_pod_ref if last_pod_ref else "unknown",
                "idle_label": idle_label,
                "sort_key": sort_key,
            })

        rows.sort(key=lambda r: r["sort_key"], reverse=True)
        self._rows = rows

        for row in rows:
            table.add_row(
                row["username"], row["lab"], row["pvc"], row["size"], row["used"],
                row["state"], row["robustness"], row["last_used"], row["idle_label"],
                key=row["pvc"],
            )

        count = len(rows)
        footer.update(
            f"[dim]{count} PVC{'s' if count != 1 else ''}  —  "
            "[bold]x/Delete[/bold] delete (press twice)  [bold]Esc/q[/bold] back[/]"
        )
        if count:
            table.focus()

    def _selected(self) -> dict | None:
        table = self.query_one("#storage-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        for row in self._rows:
            if row["pvc"] == row_key:
                return row
        return None

    def action_delete_pvc(self) -> None:
        row = self._selected()
        if not row:
            return
        key = f"delete:{row['pvc']}"
        if self._pending_key == key:
            self._clear_pending()
            self._run_delete(row)
        else:
            self._pending_key = key
            if self._pending_timer is not None:
                self._pending_timer.stop()
            self._pending_timer = self.set_timer(2.0, self._clear_pending)
            pv_part = f", PV {row['pv']}" if row["pv"] else ""
            self.notify(
                f"Press [x/Delete] again to confirm: delete PVC {row['pvc']}{pv_part} "
                f"and its Longhorn volume (user {row['username']}, {row['size']})",
                timeout=2.0,
                severity="warning",
            )

    def _clear_pending(self) -> None:
        self._pending_key = None
        self._pending_timer = None

    def _run_delete(self, row: dict) -> None:
        import shlex

        from .action_screen import ActionScreen

        # Three-step delete, same order as cleanup_users.sh: PVC, then PV,
        # then the backing Longhorn volume object. Chained with && so a
        # failed step stops the chain rather than deleting the next resource
        # out from under a PVC/PV that's still there.
        steps = [
            ["kubectl", "delete", "pvc", row["pvc"], "-n", row["namespace"]],
        ]
        if row["pv"]:
            steps.append(["kubectl", "delete", "pv", row["pv"]])
            steps.append(
                ["kubectl", "delete", "volumes.longhorn.io", row["pv"], "-n", LONGHORN_NAMESPACE]
            )
        shell_cmd = " && ".join(" ".join(shlex.quote(a) for a in step) for step in steps)
        argv = ["sh", "-c", shell_cmd]
        self.app.push_screen(
            ActionScreen(f"delete-pvc-{row['pvc']}", argv, auto_close=True)
        )

    def action_go_back(self) -> None:
        self.app.pop_screen()
