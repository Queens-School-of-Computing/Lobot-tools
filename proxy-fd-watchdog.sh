#!/bin/bash
#
# proxy-fd-watchdog.sh
#
# Periodic health check for the JupyterHub configurable-http-proxy pod.
#
# Background: the proxy pod has a known file-descriptor/memory leak
# (diagnosed 2026-09-25). Healthy baseline right after a restart is
# ~150 FDs / ~64MB RSS. The diagnosed incident reached 141,414 FDs and
# 9.2GB RSS after ~7 days uptime, causing severe cluster-wide JupyterHub
# latency (slow spawn/admin pages, slow notebook use) -- the control-plane
# host and hub pod were both healthy the whole time; the proxy pod was
# the actual bottleneck because it sits in the request path for every
# browser/hub interaction.
#
# This script checks FD count and RSS on the proxy pod and emails an
# alert if either crosses a threshold. It does NOT auto-restart the
# proxy -- alert only, so a human decides when to kill user-facing
# proxy traffic. To restart manually once alerted:
#   kubectl -n jhub delete pod <proxy-pod-name>
#
# It also checks the FD count of the jupyterhub process in the hub pod
# (added 2026-10-05). That process normally holds ~13. Its soft limit is
# 4096 via a manual prlimit patch, which resets to the container default of
# 1024 whenever the hub pod restarts. The alert fires at 900 so there is
# warning before "Too many open files" breaks spawns even at the default.
#
# Run manually:
#   bash proxy-fd-watchdog.sh
#
# Install as a systemd timer (matches lobot-metrics-digest pattern):
#   sudo cp proxy-fd-watchdog.{service,timer} /etc/systemd/system/
#   sudo systemctl daemon-reload
#   sudo systemctl enable --now proxy-fd-watchdog.timer
#
# Requires: kubectl configured with access to the jhub namespace, python3.

set -uo pipefail

NAMESPACE="jhub"
FD_THRESHOLD=20000        # healthy baseline ~150; alert well before 141k-class failure
RSS_KB_THRESHOLD=1500000  # 1.5GB; healthy baseline ~64MB
HUB_FD_THRESHOLD=900      # hub process; healthy ~13; soft limit 1024 default, 4096 when patched

# ==========================================
# Email configuration (same pattern as image-pull.sh / image-cleanup.sh)
# ==========================================
EMAIL_ENABLED=true
SMTP_SERVER="innovate.cs.queensu.ca"
SMTP_PORT=25
SMTP_USE_TLS=false
SMTP_USERNAME=""
SMTP_PASSWORD=""
FROM_EMAIL="lobot+tools@cs.queensu.ca"
TO_EMAIL="aaron.visser+lobot@queensu.ca,whb1+lobot@queensu.ca"

# ==========================================
# Email helper - reads body from temp file
# ==========================================
send_email() {
  local SUBJECT="$1"
  local BODY_FILE="$2"

  if [ "$EMAIL_ENABLED" != "true" ]; then
    rm -f "$BODY_FILE"
    return 0
  fi

  python3 <<PYEOF
import smtplib, socket
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

smtp_server = "${SMTP_SERVER}"
smtp_port   = ${SMTP_PORT}
use_tls     = "${SMTP_USE_TLS}" in ("true", "True", "1")
username    = "${SMTP_USERNAME}"
password    = "${SMTP_PASSWORD}"
from_email  = "${FROM_EMAIL}"
to_emails   = [a.strip() for a in "${TO_EMAIL}".split(",")]

with open("${BODY_FILE}", "r") as f:
    body = f.read()

msg = MIMEMultipart("alternative")
msg["Subject"] = """${SUBJECT}"""
msg["From"]    = f"{socket.getfqdn()} <{from_email}>"
msg["To"]      = ", ".join(to_emails)
msg.attach(MIMEText(body, "html"))

try:
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        if use_tls:
            server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(from_email, to_emails, msg.as_string())
    print("ok")
except Exception as e:
    print(f"error: {e}")
    exit(1)
PYEOF

  if [ $? -eq 0 ]; then
    echo " 📧 Email notification sent to $TO_EMAIL"
  else
    echo " ⚠️  Email notification failed to send"
  fi

  rm -f "$BODY_FILE"
}

# ==========================================
# Main check
# ==========================================
echo "=== proxy-fd-watchdog: $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="

# A failure checking one pod must not skip the other: collect errors,
# check both, alert on whatever was readable, exit non-zero at the end.
ERRORS=0
ALERT=false
REASON=""
EMAIL_ROWS=""

# ------------------------------------------
# 1. Proxy pod (configurable-http-proxy FD/memory leak)
# ------------------------------------------
# Resolve the proxy pod dynamically -- its name changes on every
# restart/reschedule, never hardcode it.
PROXY_POD=$(kubectl -n "$NAMESPACE" get pod -l component=proxy \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -z "$PROXY_POD" ]; then
  echo "ERROR: could not resolve proxy pod via label selector component=proxy in namespace $NAMESPACE"
  ERRORS=$((ERRORS + 1))
else
  echo "PROXY_POD=$PROXY_POD"
  PROXY_NODE=$(kubectl -n "$NAMESPACE" get pod "$PROXY_POD" -o jsonpath='{.spec.nodeName}' 2>/dev/null)
  echo "PROXY_NODE=$PROXY_NODE"

  FD_COUNT=$(kubectl -n "$NAMESPACE" exec "$PROXY_POD" -- sh -c 'ls /proc/1/fd | wc -l' 2>/dev/null)
  RSS_KB=$(kubectl -n "$NAMESPACE" exec "$PROXY_POD" -- sh -c "grep VmRSS /proc/1/status | awk '{print \$2}'" 2>/dev/null)

  if [ -z "$FD_COUNT" ] || [ -z "$RSS_KB" ]; then
    echo "ERROR: could not read FD count or RSS from proxy pod $PROXY_POD (exec failed?)"
    ERRORS=$((ERRORS + 1))
  else
    echo "FD_COUNT=$FD_COUNT (threshold: $FD_THRESHOLD)"
    echo "RSS_KB=$RSS_KB (threshold: $RSS_KB_THRESHOLD)"

    if [ "$FD_COUNT" -ge "$FD_THRESHOLD" ]; then
      ALERT=true
      REASON="${REASON}Proxy FD count $FD_COUNT >= threshold $FD_THRESHOLD. "
    fi
    if [ "$RSS_KB" -ge "$RSS_KB_THRESHOLD" ]; then
      ALERT=true
      REASON="${REASON}Proxy RSS ${RSS_KB}KB >= threshold ${RSS_KB_THRESHOLD}KB. "
    fi

    EMAIL_ROWS="${EMAIL_ROWS}
<tr><td colspan=\"2\"><b>Proxy pod</b></td></tr>
<tr><td>Pod</td><td>${PROXY_POD}</td></tr>
<tr><td>Node</td><td>${PROXY_NODE}</td></tr>
<tr><td>FD count</td><td>${FD_COUNT} (threshold ${FD_THRESHOLD})</td></tr>
<tr><td>RSS</td><td>${RSS_KB} KB (threshold ${RSS_KB_THRESHOLD} KB)</td></tr>"
  fi
fi

# ------------------------------------------
# 2. Hub pod (jupyterhub process FD count)
# ------------------------------------------
# The hub process runs under tini, so it is not PID 1 (it was PID 7 when
# checked 2026-10-05). Find it by command line instead of hardcoding the
# PID: its second argument is exactly ".../bin/jupyterhub"
# ("/usr/local/bin/python3.12 /usr/local/bin/jupyterhub --config ...").
# Matching argv[1] exactly skips tini ("--"), the idle culler ("-m"), and
# this probe's own sh ("-c"), whose script text contains the search string
# -- a substring match on the whole command line matched the probe itself.
# Prints: <pid> <fds> <soft limit>
HUB_PROBE='for p in /proc/[0-9]*; do
  [ "${p#/proc/}" = "$$" ] && continue
  arg1=$(tr "\0" "\n" < $p/cmdline 2>/dev/null | sed -n 2p)
  case "$arg1" in
    */bin/jupyterhub)
      echo "${p#/proc/} $(ls $p/fd | wc -l) $(grep "Max open files" $p/limits | awk "{print \$4}")"
      break ;;
  esac
done'

HUB_POD=$(kubectl -n "$NAMESPACE" get pod -l component=hub \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

HUB_PID=""
HUB_FD_COUNT=""
HUB_NOFILE_SOFT=""
if [ -z "$HUB_POD" ]; then
  echo "ERROR: could not resolve hub pod via label selector component=hub in namespace $NAMESPACE"
  ERRORS=$((ERRORS + 1))
else
  echo "HUB_POD=$HUB_POD"
  read -r HUB_PID HUB_FD_COUNT HUB_NOFILE_SOFT < <(
    kubectl -n "$NAMESPACE" exec "$HUB_POD" -c hub -- sh -c "$HUB_PROBE" 2>/dev/null)

  if [ -z "$HUB_FD_COUNT" ]; then
    echo "ERROR: could not find the jupyterhub process or read its FD count in hub pod $HUB_POD (exec failed?)"
    ERRORS=$((ERRORS + 1))
  else
    echo "HUB_PID=$HUB_PID"
    echo "HUB_FD_COUNT=$HUB_FD_COUNT (threshold: $HUB_FD_THRESHOLD, soft limit: $HUB_NOFILE_SOFT)"

    if [ "$HUB_FD_COUNT" -ge "$HUB_FD_THRESHOLD" ]; then
      ALERT=true
      REASON="${REASON}Hub FD count $HUB_FD_COUNT >= threshold $HUB_FD_THRESHOLD (soft limit $HUB_NOFILE_SOFT). "
    fi

    EMAIL_ROWS="${EMAIL_ROWS}
<tr><td colspan=\"2\"><b>Hub pod</b></td></tr>
<tr><td>Pod</td><td>${HUB_POD}</td></tr>
<tr><td>jupyterhub PID</td><td>${HUB_PID}</td></tr>
<tr><td>FD count</td><td>${HUB_FD_COUNT} (threshold ${HUB_FD_THRESHOLD}, soft limit ${HUB_NOFILE_SOFT})</td></tr>"
  fi
fi

# ------------------------------------------
# Alert
# ------------------------------------------
if [ "$ALERT" != "true" ]; then
  echo "OK: checked pods within normal range, no alert."
  [ "$ERRORS" -gt 0 ] && exit 1
  exit 0
fi

echo "ALERT: $REASON"

# One command per box. Email clients strip JavaScript, so a real copy button
# isn't possible; user-select:all makes one click select the whole command
# where supported (e.g. Apple Mail) and is ignored elsewhere. pre-wrap keeps
# long commands readable without adding line breaks to the copied text.
CMD='<pre style="user-select:all;-webkit-user-select:all;white-space:pre-wrap;word-break:break-all;background:#f4f4f4;border:1px solid #ddd;border-radius:4px;padding:8px 10px;margin:4px 0 12px;font-family:monospace">'
STEP='<p style="margin:12px 0 0">'

BODY_TMPFILE=$(mktemp)
cat > "$BODY_TMPFILE" <<HTMLEOF
<html><body style="font-family:sans-serif">
<h2>⚠️ JupyterHub proxy/hub resource watchdog alert</h2>
<p><b>${REASON}</b></p>
<table cellpadding="6" style="border-collapse:collapse">
${EMAIL_ROWS}
<tr><td>Checked at</td><td>$(date -u +%Y-%m-%dT%H:%M:%SZ)</td></tr>
</table>
<h3>If the proxy is over threshold: restart the proxy pod</h3>
<p>This is the known configurable-http-proxy FD/memory leak (diagnosed 2026-09-25).
Healthy baseline right after a restart is ~150 FDs / ~64MB RSS. Deleting the pod
is safe: the Deployment recreates it within seconds and the leaked FDs/memory are
released. Browser connections through the proxy drop briefly, so users may need to
refresh their tab. Running notebook servers are not stopped.</p>
${STEP}<b>1.</b> Find the proxy pod:</p>
${CMD}PROXY_POD=\$(kubectl -n ${NAMESPACE} get pod -l component=proxy -o jsonpath='{.items[0].metadata.name}'); echo "\$PROXY_POD"</pre>
${STEP}<b>2.</b> Confirm usage is high before restarting (FD count, then RSS in KB):</p>
${CMD}kubectl -n ${NAMESPACE} exec \$PROXY_POD -- sh -c 'ls /proc/1/fd | wc -l; grep VmRSS /proc/1/status'</pre>
${STEP}<b>3.</b> Restart the proxy pod:</p>
${CMD}kubectl -n ${NAMESPACE} delete pod \$PROXY_POD</pre>
${STEP}<b>4.</b> Watch the replacement come up (Ctrl-C once it shows Running 1/1):</p>
${CMD}kubectl -n ${NAMESPACE} get pod -l component=proxy -w</pre>
<h3>If the hub is over threshold: raise the hub's open-file limit</h3>
<p>The jupyterhub process normally holds ~13 FDs (checked 2026-10-05). Its soft
limit is 1024 by default, or 4096 if the prlimit patch below has been applied
since the last hub pod restart. Past the soft limit, new opens fail with
"Too many open files" (failed spawns / API calls). The patch raises the soft
limit until the next hub pod restart. A count near 900 means something in the
hub is leaking, so check the hub logs too. The jupyterhub PID found by this
check is ${HUB_PID:-unknown (7 on 2026-10-05)}; the commands below use it.</p>
${STEP}<b>1.</b> Find the hub pod:</p>
${CMD}HUB_POD=\$(kubectl get pods -n ${NAMESPACE} -o json | jq -r '.items[] | select(.metadata.name | startswith("hub-")) | .metadata.name' | head -n1); echo "\$HUB_POD"</pre>
${STEP}<b>2.</b> Check current FD usage and limits first:</p>
${CMD}kubectl exec -n ${NAMESPACE} \$HUB_POD -- sh -c 'ls /proc/${HUB_PID:-7}/fd | wc -l; grep "open files" /proc/${HUB_PID:-7}/limits'</pre>
${STEP}<b>3.</b> Set the soft limit to 4096 and keep the hard limit at 524288:</p>
${CMD}kubectl exec -n ${NAMESPACE} \$HUB_POD -- sh -c 'command -v prlimit &amp;&amp; prlimit --pid=${HUB_PID:-7} --nofile=4096:524288'</pre>
<p>This watchdog does NOT restart or change anything automatically.</p>
</body></html>
HTMLEOF

send_email "⚠️ proxy-fd-watchdog ALERT | ${REASON}" "$BODY_TMPFILE"

[ "$ERRORS" -gt 0 ] && exit 1
exit 0
