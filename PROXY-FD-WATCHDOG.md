# proxy-fd-watchdog — JupyterHub Proxy Leak Alert

## Overview

A periodic health check for the JupyterHub `configurable-http-proxy` pod. It watches for a known file-descriptor/memory leak that causes severe, cluster-wide JupyterHub latency, and emails an alert before the leak becomes user-impacting.

**Capabilities at a glance:**

- Resolves the current proxy pod dynamically (`kubectl get pod -l component=proxy`) — never hardcodes a pod name, so it keeps working across restarts and reschedules
- Reads FD count and RSS memory from the proxy container via `kubectl exec` (`/proc/1/fd`, `/proc/1/status`) — no SSH or host access needed, works regardless of which node the pod lands on
- Emails an HTML alert if either crosses a threshold
- **Alert only — does not auto-restart the proxy.** A human decides when to kill user-facing proxy traffic.
- Runs every 2 hours via a systemd timer

---

## Background

Diagnosed 2026-09-25. The `proxy` pod (image `quay.io/jupyterhub/configurable-http-proxy:4.6.2`) leaks file descriptors and memory over time. After roughly a week of uptime it reached **141,414 open FDs and 9.2GB RSS** (healthy baseline right after a restart is **~150 FDs / ~64MB RSS**). Only a small fraction of the leaked FDs were actual TCP sockets — the bulk were other handle types, consistent with the proxy's error-handling path (visible in its logs as repeated `write EPIPE` errors) leaking whatever handle was tied to a failed/aborted request instead of cleaning it up.

Because the proxy sits in the request path for every browser and hub interaction, its slow decay presented as generic, cluster-wide "JupyterHub is slow" — spawn pages, the admin page, and in-notebook use all degraded, while the control-plane host and hub pod were completely healthy the entire time. Migrating the control-plane host, patching/rebooting it, and restarting the hub pod all did nothing, because none of them touched the actual bottleneck. The fix was simply restarting the proxy pod:

```bash
kubectl -n jhub delete pod <proxy-pod-name>
```

The pod had already restarted 4 times roughly a week before this incident, suggesting the leak recurs on something close to a weekly cycle. This watchdog exists to catch the next occurrence early rather than requiring another multi-hour diagnosis from scratch.

**Not yet done:** root-causing why the leak happens (likely a bug in configurable-http-proxy 4.6.2's error-handling path), and no auto-restart or newer-image upgrade has been applied yet.

---

## Prerequisites

- `kubectl` configured with access to the `jhub` namespace (`get`/`list` on pods, `create` on `pods/exec`)
- `python3` (used for sending the alert email, same as `image-pull.sh` / `image-cleanup.sh`)

---

## Installation

### Make the script executable

```bash
chmod +x /opt/Lobot/tools/proxy-fd-watchdog.sh
```

### Install and start the timer

```bash
sudo cp /opt/Lobot/tools/proxy-fd-watchdog.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now proxy-fd-watchdog.timer

# Verify it's scheduled
sudo systemctl list-timers proxy-fd-watchdog.timer
```

`Persistent=true` on the timer means a missed run (e.g. the host was down) fires on next boot.

---

## Manual Usage

Run the check immediately, without waiting for the timer:

```bash
bash /opt/Lobot/tools/proxy-fd-watchdog.sh
```

On a healthy proxy, this prints the current FD count/RSS and exits with "OK: proxy pod within normal range, no alert." — no email is sent. Follow logs from the timer-driven runs with:

```bash
sudo journalctl -u proxy-fd-watchdog -f
```

### Force-testing the email path

Since the proxy is normally healthy, the alert path won't trigger in an ordinary manual run. To confirm email delivery end-to-end, temporarily lower the threshold in the script, run once, then restore it:

```bash
# In proxy-fd-watchdog.sh, temporarily set:
#   FD_THRESHOLD=1
bash /opt/Lobot/tools/proxy-fd-watchdog.sh
# Confirm the alert email arrives, then set FD_THRESHOLD back to 20000
```

---

## Configuration

All configuration is at the top of `proxy-fd-watchdog.sh`:

| Variable | Default | Meaning |
|---|---|---|
| `NAMESPACE` | `jhub` | Namespace the proxy pod runs in |
| `FD_THRESHOLD` | `20000` | Alert if open FD count reaches or exceeds this (healthy baseline ~150) |
| `RSS_KB_THRESHOLD` | `1500000` (1.5GB) | Alert if resident memory reaches or exceeds this (healthy baseline ~64MB) |
| `EMAIL_ENABLED` | `true` | Set `false` to disable email (still logs to stdout/journald) |
| `SMTP_SERVER` / `SMTP_PORT` | `innovate.cs.queensu.ca` / `25` | Mail relay, same as `image-pull.sh` |
| `FROM_EMAIL` | `lobot+tools@cs.queensu.ca` | Sender address |
| `TO_EMAIL` | `aaron.visser+lobot@queensu.ca,whb1+lobot@queensu.ca` | Comma-separated recipients |

Both thresholds carry wide margin above the healthy baseline and well below the failure state observed in the 2026-09-25 incident, so an alert means something is clearly trending wrong, not noise.

---

## When You Get an Alert

The email includes the pod name, node, current FD count/RSS, and the restart command. To resolve:

```bash
kubectl -n jhub delete pod <proxy-pod-name>
```

The Deployment recreates the pod automatically. FD count and RSS should drop back to baseline (~150 FDs, ~64MB) immediately, and JupyterHub latency should resolve. This is a mitigation, not a fix — if alerts recur frequently, it's worth checking for a newer `configurable-http-proxy` image with the underlying leak fixed.

---

## Troubleshooting

**"could not resolve proxy pod via label selector"** — the `component=proxy` label may not match in your setup (chart version differences). Check with:

```bash
kubectl -n jhub get pods --show-labels | grep proxy
```

and adjust the selector in `proxy-fd-watchdog.sh` if needed.

**"could not read FD count or RSS from proxy pod (exec failed?)"** — check that the ServiceAccount/user running the script has `pods/exec` permission in the `jhub` namespace, and that the proxy pod is actually `Running` (not `CrashLoopBackOff` or `Pending`).

**Email fails silently** — run manually and check stdout for `⚠️ Email notification failed to send`; the underlying Python exception is printed above that line. Common causes: SMTP relay unreachable from this host, or `python3` not installed.

---

## Related

- `IMAGE-MANAGEMENT.md` — companion tooling docs for this repo
