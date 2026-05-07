# lobot-metrics — Cluster Resource Tracking & Billing

## Overview

A background service that tracks JupyterHub session history and produces usage reports for capacity planning and departmental billing.

**Capabilities at a glance:**

- Records every `jupyter-*` pod lifecycle as a session (start time, end time, duration)
- Captures requested CPU cores, RAM, GPU, and home directory PVC size per session
- Billing model: **requested resources × session duration** (HPC-style) — CPU-core-hours, RAM-GB-hours, GPU-hours
- Periodic cluster-wide snapshots (resource utilisation, node allocation, Longhorn disk) every 15 minutes
- Reports grouped by user, lab, or configurable billing groups (supports cross-lab groupings)
- CLI for ad-hoc reports, CSV export, and session inspection
- Monthly HTML email digest sent on the 1st of each month

The service subscribes to the `lobot-collector` SSE stream (`localhost:9095/api/events`) — no independent kubectl polling.

---

## Prerequisites

- `lobot-collector` service running (`sudo systemctl status lobot-collector`)
- Python 3.8+ with `python3.12-venv`:

```bash
sudo apt install python3.12-venv
```

- `kubectl` configured with cluster access (used only to look up PVC sizes when a pod appears)

---

## Installation

### Create the data directory

```bash
sudo mkdir -p /opt/Lobot/metrics_data
sudo chown croot:croot /opt/Lobot/metrics_data
```

### Set up the venv

```bash
python3 -m venv /opt/Lobot/tools/lobot_metrics/.venv

/opt/Lobot/tools/lobot_metrics/.venv/bin/pip install \
    -r /opt/Lobot/tools/lobot_metrics/requirements-metrics.txt

# lobot_tui is imported for ClusterState parsing — install its deps too
/opt/Lobot/tools/lobot_metrics/.venv/bin/pip install \
    -r /opt/Lobot/tools/lobot_tui/requirements-tui.txt
```

### Make the launcher executable

```bash
chmod +x /opt/Lobot/tools/lobot-metrics.sh
```

### Configure billing groups

```bash
cp /opt/Lobot/tools/lobot_metrics/billing_config.yaml.sample \
   /opt/Lobot/metrics_data/billing_config.yaml
# Edit to match your labs and external groups — see Configuration below
```

### Install and start the daemon

```bash
sudo cp /opt/Lobot/tools/lobot-metrics.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lobot-metrics

# Verify it is running
sudo systemctl status lobot-metrics

# Follow logs
sudo journalctl -u lobot-metrics -f
```

### Install the monthly digest timer (optional)

```bash
sudo cp /opt/Lobot/tools/lobot-metrics-digest.service /etc/systemd/system/
sudo cp /opt/Lobot/tools/lobot-metrics-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lobot-metrics-digest.timer

# Verify the timer is scheduled
sudo systemctl list-timers lobot-metrics-digest.timer
```

The timer fires on the 1st of each month at 08:00 and sends the previous month's digest. `Persistent=true` means it will fire on the next boot if the server was down when it was scheduled.

---

## Configuration

### `billing_config.yaml`

Located at `/opt/Lobot/metrics_data/billing_config.yaml`. Controls how sessions are rolled up into billing groups for reports and the email digest.

```yaml
billing_groups:

  # External users billed by username regardless of which lab node they use
  smith_business:
    display_name: "Smith School of Business"
    contact: "finance@smith.queensu.ca"    # optional; for your reference
    users:
      - jsmith
      - bwilson

  # Labs billed by node label (lab=<value>)
  bamlab:
    display_name: "BAM Lab"
    labs:
      - bamlab

  # Shared compute: sessions not matched by a user-level rule fall through here
  lobot_shared:
    display_name: "Lobot Shared Compute"
    labs:
      - lobot_a40
      - lobot_a5000
      - lobot_a16
      - lobot_problackwell
```

**Priority rules:**

1. User-level assignments are checked first — a user listed under `users:` is always billed to that group regardless of which lab node they run on.
2. Lab-level assignments are checked next — all users on a `lab=<value>` node roll up to the matching group.
3. Groups are checked in definition order; first match wins.
4. Unmatched sessions appear as `unassigned` in reports.

A sample config pre-filled with all known labs is at `lobot_metrics/billing_config.yaml.sample`.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `LOBOT_CLUSTER_DIR` | `/opt/Lobot` | Root of the cluster config repo; data directory is `$LOBOT_CLUSTER_DIR/metrics_data` |
| `LOBOT_TOOLS_DIR` | `$LOBOT_CLUSTER_DIR/tools` | Tools directory (overrides the derived default) |

---

## CLI Usage

All commands are run via the launcher:

```bash
/opt/Lobot/tools/lobot-metrics.sh <subcommand> [options]
```

### `report` — Print a usage report

```bash
# All sections (billing group, lab, and user)
./lobot-metrics.sh report --month 2026-05

# One section only
./lobot-metrics.sh report --month 2026-05 --by group
./lobot-metrics.sh report --month 2026-05 --by lab
./lobot-metrics.sh report --month 2026-05 --by user
```

**Report columns:**

| Column | Description |
|--------|-------------|
| Hours | Total session wall-clock hours |
| CPU-core-hrs | Requested CPU cores × session hours |
| RAM-GB-hrs | Requested RAM (GB) × session hours |
| GPU-hrs | Requested GPUs × session hours |
| Peak CPU | Sum of each user's largest CPU request in the period |
| Peak RAM GB | Sum of each user's largest RAM request in the period |
| Peak GPU | Sum of each user's largest GPU request in the period |
| Avg PVC GB | Largest home directory PVC seen for the user in the period |

Only sessions that both started **and** ended within the month are counted. Sessions still running at report time are excluded.

### `sessions` — Inspect recorded sessions

```bash
# Show all sessions
./lobot-metrics.sh sessions

# Show only active (currently running) sessions
./lobot-metrics.sh sessions --active

# Filter by user or lab
./lobot-metrics.sh sessions --user wiegerthefarmer
./lobot-metrics.sh sessions --lab quarrglab
```

### `export` — Export to CSV

```bash
# By user (default)
./lobot-metrics.sh export --month 2026-05 --out /tmp/usage-2026-05.csv

# By lab or billing group
./lobot-metrics.sh export --month 2026-05 --by lab   --out /tmp/usage-lab.csv
./lobot-metrics.sh export --month 2026-05 --by group --out /tmp/usage-group.csv
```

CSV columns match the report columns for the chosen grouping, including `pvc_capacity_gb`.

### `send-digest` — Send the monthly email digest

```bash
# Previous month (default)
./lobot-metrics.sh send-digest

# Specific month
./lobot-metrics.sh send-digest --month 2026-05

# Override recipient
./lobot-metrics.sh send-digest --month 2026-05 --to billing@example.com
```

The digest is an HTML email with compute and storage tables for all three groupings (billing group, lab, user). It is sent from `<hostname>@cs.queensu.ca` to the address configured in `config.py` (`TO_EMAIL`).

### `daemon` — Start the recorder (called by systemd)

```bash
./lobot-metrics.sh daemon
# or equivalently, with no subcommand:
./lobot-metrics.sh
```

---

## How It Works

### Session tracking

The daemon subscribes to `lobot-collector`'s SSE stream. On each cluster state update it diffs the current pod list against its internal state:

- **Pod appeared** → opens a session row in SQLite with the pod's Kubernetes `startTime`, node, lab, and requested resources. Looks up the user's home PVC size (`claim-<username>` in namespace `jhub`) via kubectl and stores it on the session row.
- **Pod disappeared** → closes the session row with `end_time`, computed `duration_seconds`, and `end_reason=normal`.

The session ID is `{pod_name}::{k8s_start_time}` — stable across daemon restarts; `INSERT OR IGNORE` is idempotent.

### Startup reconciliation

On startup, the daemon fetches `/api/state` from the collector and compares open database sessions against the live pod list:

- Open sessions whose pod is no longer running are closed (`end_reason=reconciled_missing`; duration estimated from now).
- Live pods with no database row get a new session opened.

This handles the case where the daemon was down while pods started or stopped.

### SSE reconnection

If the collector is unreachable, the daemon retries with exponential backoff from 5 s up to 120 s.

### Periodic snapshots

Every 15 minutes the daemon writes one snapshot row per lab (resource utilisation), per node (allocation), and per Longhorn disk. These are used for historical cluster capacity analysis but are not currently surfaced in the CLI reports.

### PVC storage

Home directory size is captured once when each pod appears, not polled. The value stored is the `spec.resources.requests.storage` field from the PVC `claim-<username>` in the `jhub` namespace. Reports show the largest PVC seen for each user in the period.

---

## Deploying Updates

Pull the latest code and restart the daemon to pick up changes:

```bash
cd /opt/Lobot/tools && git pull
sudo systemctl restart lobot-metrics
```

Reporting commands (`report`, `export`, `sessions`, `send-digest`) do not require a service restart — just pull and run.

---

## Verification

```bash
# Service is running and connected
sudo systemctl status lobot-metrics
sudo journalctl -u lobot-metrics -f

# Active sessions match running pods
./lobot-metrics.sh sessions --active

# Session count in the database
sqlite3 /opt/Lobot/metrics_data/lobot_metrics.db "SELECT COUNT(*) FROM sessions"

# Current month report
./lobot-metrics.sh report --month $(date +%Y-%m)

# Send a test digest to yourself
./lobot-metrics.sh send-digest --month $(date +%Y-%m) --to your@email.com
```

---

## Source Files

```
tools/lobot_metrics/
  __init__.py                  Package marker
  __main__.py                  Entry point (python3 -m lobot_metrics)
  config.py                    Paths, intervals, SSE URL, SMTP constants
  db.py                        SQLite schema + all query functions
  recorder.py                  SSE subscriber; session open/close; startup reconcile
  snapshotter.py               15-min periodic resource/node/storage snapshot loop
  billing.py                   billing_config.yaml loader + group resolution
  reporter.py                  Aggregation queries → tables / CSV (no async)
  emailer.py                   Monthly HTML digest builder + SMTP send
  cli.py                       argparse entry point (all subcommands)
  billing_config.yaml.sample   Pre-filled sample config with all known labs
  requirements-metrics.txt     aiohttp>=3.9, PyYAML>=6.0

tools/lobot-metrics.sh                 Shell launcher
tools/lobot-metrics.service            systemd unit (recorder daemon)
tools/lobot-metrics-digest.service     systemd oneshot unit for monthly digest
tools/lobot-metrics-digest.timer       Fires on *-*-01 08:00:00

/opt/Lobot/metrics_data/               Data directory (created during install)
  lobot_metrics.db                     SQLite database (WAL mode)
  billing_config.yaml                  Active billing group configuration
```
