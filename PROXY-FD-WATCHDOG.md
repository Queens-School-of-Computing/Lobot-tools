# proxy-fd-watchdog — JupyterHub Proxy Leak Alert

## Overview

A periodic health check for the JupyterHub `configurable-http-proxy` pod. It watches for a known file-descriptor/memory leak that causes severe, cluster-wide JupyterHub latency, and emails an alert before the leak becomes user-impacting.

**Capabilities at a glance:**

- Resolves the current proxy pod dynamically (`kubectl get pod -l component=proxy`) — never hardcodes a pod name, so it keeps working across restarts and reschedules
- Reads FD count and RSS memory from the proxy container via `kubectl exec` (`/proc/1/fd`, `/proc/1/status`) — no SSH or host access needed, works regardless of which node the pod lands on
- Also checks the FD count of the `jupyterhub` process in the hub pod (see [Hub FD check](#hub-fd-check))
- Emails one HTML alert covering whichever checks crossed a threshold
- If one pod can't be checked, the other is still checked, and the script exits non-zero
- **Alert only — does not auto-restart the proxy or change any limits.** A human decides when to kill user-facing proxy traffic.
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

### Hub FD check

Added 2026-10-05. During the proxy incident, a manual `prlimit` patch raised the hub process's open-file limit to 65536. That patch only lasts until the hub pod restarts, which drops the limit back to the container default of **1024 open files**. On 2026-10-05 the patch was reapplied at a lower **4096** (soft limit only, hard limit left at 524288), and the process held **13** files. Because a pod restart can quietly put it back to 1024, the watchdog alerts at **900**, which is under both limits. That leaves room to act before new opens fail with "Too many open files" (which would show up as failed spawns or API calls).

The `jupyterhub` process runs under `tini`, so it is not PID 1 (it was PID 7 when checked). The script finds it by its second command-line argument, which is exactly `/usr/local/bin/jupyterhub`. That skips `tini` (`--`), the idle culler (`-m`), and the probe's own shell (`-c`), so it doesn't depend on the PID staying the same. (A first version matched the text `bin/jupyterhub ` anywhere in the command line, and matched its own probe shell, whose script contains that text.) The alert email shows the soft limit actually in effect.

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

When everything is healthy, this prints the proxy FD count/RSS and the hub FD count, then exits with "OK: checked pods within normal range, no alert." — no email is sent. Follow logs from the timer-driven runs with:

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

`HUB_FD_THRESHOLD=1` tests the hub path the same way (restore it to `900`).

---

## Configuration

All configuration is at the top of `proxy-fd-watchdog.sh`:

| Variable | Default | Meaning |
|---|---|---|
| `NAMESPACE` | `jhub` | Namespace the proxy and hub pods run in |
| `FD_THRESHOLD` | `20000` | Alert if the proxy's open FD count reaches or exceeds this (healthy baseline ~150) |
| `RSS_KB_THRESHOLD` | `1500000` (1.5GB) | Alert if the proxy's resident memory reaches or exceeds this (healthy baseline ~64MB) |
| `HUB_FD_THRESHOLD` | `900` | Alert if the hub's `jupyterhub` process FD count reaches or exceeds this (healthy ~13; soft limit 1024 by default, 4096 when patched) |
| `EMAIL_ENABLED` | `true` | Set `false` to disable email (still logs to stdout/journald) |
| `SMTP_SERVER` / `SMTP_PORT` | `innovate.cs.queensu.ca` / `25` | Mail relay, same as `image-pull.sh` |
| `FROM_EMAIL` | `lobot+tools@cs.queensu.ca` | Sender address |
| `TO_EMAIL` | `aaron.visser+lobot@queensu.ca,whb1+lobot@queensu.ca` | Comma-separated recipients |

The proxy thresholds carry wide margin above the healthy baseline and well below the failure state observed in the 2026-09-25 incident, so an alert means something is clearly trending wrong, not noise. The hub threshold sits just under the default 1024 soft limit, so it still gives warning if a pod restart has dropped the 4096 patch.

---

## When You Get an Alert

The email includes the pod name, node, current FD count/RSS, and the restart command. To resolve:

```bash
kubectl -n jhub delete pod <proxy-pod-name>
```

The Deployment recreates the pod automatically. FD count and RSS should drop back to baseline (~150 FDs, ~64MB) immediately, and JupyterHub latency should resolve. This is a mitigation, not a fix — if alerts recur frequently, it's worth checking for a newer `configurable-http-proxy` image with the underlying leak fixed.

### Hub alert

The email includes the hub pod name, the `jupyterhub` PID, its FD count, and its current soft limit. To buy headroom, raise the soft limit to 4096 and leave the hard limit alone (lowering the hard limit can't be undone inside the container):

```bash
kubectl -n jhub exec <hub-pod-name> -c hub -- prlimit --pid=<pid> --nofile=4096:524288
```

This lasts only until the hub pod restarts (any helm upgrade restarts it). The hub normally holds ~13 FDs, so a count near 900 means something is leaking. Check the hub logs and see what the open handles are: `kubectl -n jhub exec <hub-pod-name> -c hub -- ls -l /proc/<pid>/fd`.

---

## Troubleshooting

**"could not resolve proxy pod via label selector"** — the `component=proxy` label may not match in your setup (chart version differences). Check with:

```bash
kubectl -n jhub get pods --show-labels | grep proxy
```

and adjust the selector in `proxy-fd-watchdog.sh` if needed.

**"could not read FD count or RSS from proxy pod (exec failed?)"** — check that the ServiceAccount/user running the script has `pods/exec` permission in the `jhub` namespace, and that the proxy pod is actually `Running` (not `CrashLoopBackOff` or `Pending`).

**"could not find the jupyterhub process or read its FD count in hub pod"** — check the hub pod is `Running`, and that the process list still shows a `/usr/local/bin/jupyterhub` command line:

```bash
kubectl -n jhub exec <hub-pod-name> -c hub -- sh -c 'for p in /proc/[0-9]*; do printf "%s  " "${p#/proc/}"; tr "\0" " " < $p/cmdline; echo; done'
```

If a hub image change altered the command line, adjust the `*/bin/jupyterhub` match in `HUB_PROBE`.

**Email fails silently** — run manually and check stdout for `⚠️ Email notification failed to send`; the underlying Python exception is printed above that line. Common causes: SMTP relay unreachable from this host, or `python3` not installed.

---

## Related

- `IMAGE-MANAGEMENT.md` — companion tooling docs for this repo
