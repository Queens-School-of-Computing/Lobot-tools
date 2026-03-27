# Lobot-tools

Operational tooling for JupyterHub clusters managed with Z2JH (Zero to JupyterHub). Designed to work alongside a cluster-config repository.

Originally developed for the [Queen's School of Computing](https://github.com/Queens-School-of-Computing/Lobot) JupyterHub cluster.

## What's included

| Component | Description |
|---|---|
| `lobot_tui/` | Terminal UI for cluster administration (node status, image management, Helm upgrades) |
| `lobot_collector/` | Daemon that collects resource usage and writes JSON for the status page |
| `tests/` | Full test suite |
| `image-pull.sh` / `image-cleanup.sh` | Node image management scripts |
| `apply-config.sh` | Merges Helm config and applies it to the cluster |
| `sync_groups.sh` | Syncs JupyterHub groups from an external source |
| `lv-manage.sh` | Logical volume management helper |
| `lobot-collector.service` | Systemd unit for the collector daemon |

## Requirements

- Python 3.12+
- A cluster-config repository (contains `config.yaml`, `announcement.yaml`, etc.)
- `kubectl` and `helm` on `$PATH`

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `LOBOT_CLUSTER_DIR` | `/opt/Lobot` | Path to the cluster-config repository |
| `LOBOT_TOOLS_DIR` | `$LOBOT_CLUSTER_DIR/tools` | Path to this tools directory |

Both variables default to the Queen's School of Computing layout (`/opt/Lobot`). Set them to use a different cluster-config location.

## Deployment

### Default layout (Queen's School of Computing)

```bash
# Clone cluster config
git clone https://github.com/Queens-School-of-Computing/Lobot.git /opt/Lobot

# Clone tools into tools/ subdirectory
git clone https://github.com/Queens-School-of-Computing/Lobot-tools.git /opt/Lobot/tools
```

No environment variables need to be set — defaults match this layout.

### Another cluster

```bash
git clone https://github.com/Queens-School-of-Computing/Lobot-tools.git /opt/my-tools

# In /home/croot/.bashrc:
export LOBOT_CLUSTER_DIR=/opt/my-cluster-config
export LOBOT_TOOLS_DIR=/opt/my-tools
```

See [cluster-setup.md](cluster-setup.md) for full deployment instructions.

## Development

```bash
python3 -m venv .venv-dev
.venv-dev/bin/pip install -r requirements-dev.txt

# Run tests
LOBOT_TOOLS_DIR=. LOBOT_CLUSTER_DIR=.. .venv-dev/bin/pytest tests/ -v
```

See [testing.md](testing.md) for details.

## Documentation

- [cluster-setup.md](cluster-setup.md) — full cluster setup guide
- [lobot-tui.md](lobot-tui.md) — TUI installation and usage
- [IMAGE-MANAGEMENT.md](IMAGE-MANAGEMENT.md) — image pull/cleanup scripts
- [testing.md](testing.md) — running the test suite locally
