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

# Resolve the proxy pod dynamically -- its name changes on every
# restart/reschedule, never hardcode it.
PROXY_POD=$(kubectl -n "$NAMESPACE" get pod -l component=proxy \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -z "$PROXY_POD" ]; then
  echo "ERROR: could not resolve proxy pod via label selector component=proxy in namespace $NAMESPACE"
  exit 1
fi
echo "PROXY_POD=$PROXY_POD"

PROXY_NODE=$(kubectl -n "$NAMESPACE" get pod "$PROXY_POD" -o jsonpath='{.spec.nodeName}' 2>/dev/null)
echo "PROXY_NODE=$PROXY_NODE"

FD_COUNT=$(kubectl -n "$NAMESPACE" exec "$PROXY_POD" -- sh -c 'ls /proc/1/fd | wc -l' 2>/dev/null)
RSS_KB=$(kubectl -n "$NAMESPACE" exec "$PROXY_POD" -- sh -c "grep VmRSS /proc/1/status | awk '{print \$2}'" 2>/dev/null)

if [ -z "$FD_COUNT" ] || [ -z "$RSS_KB" ]; then
  echo "ERROR: could not read FD count or RSS from proxy pod $PROXY_POD (exec failed?)"
  exit 1
fi

echo "FD_COUNT=$FD_COUNT (threshold: $FD_THRESHOLD)"
echo "RSS_KB=$RSS_KB (threshold: $RSS_KB_THRESHOLD)"

ALERT=false
REASON=""
if [ "$FD_COUNT" -ge "$FD_THRESHOLD" ]; then
  ALERT=true
  REASON="${REASON}FD count $FD_COUNT >= threshold $FD_THRESHOLD. "
fi
if [ "$RSS_KB" -ge "$RSS_KB_THRESHOLD" ]; then
  ALERT=true
  REASON="${REASON}RSS ${RSS_KB}KB >= threshold ${RSS_KB_THRESHOLD}KB. "
fi

if [ "$ALERT" != "true" ]; then
  echo "OK: proxy pod within normal range, no alert."
  exit 0
fi

echo "ALERT: $REASON"

BODY_TMPFILE=$(mktemp)
cat > "$BODY_TMPFILE" <<HTMLEOF
<html><body style="font-family:sans-serif">
<h2>⚠️ JupyterHub proxy pod resource watchdog alert</h2>
<p><b>${REASON}</b></p>
<table cellpadding="6" style="border-collapse:collapse">
<tr><td><b>Pod</b></td><td>${PROXY_POD}</td></tr>
<tr><td><b>Node</b></td><td>${PROXY_NODE}</td></tr>
<tr><td><b>FD count</b></td><td>${FD_COUNT} (threshold ${FD_THRESHOLD})</td></tr>
<tr><td><b>RSS</b></td><td>${RSS_KB} KB (threshold ${RSS_KB_THRESHOLD} KB)</td></tr>
<tr><td><b>Checked at</b></td><td>$(date -u +%Y-%m-%dT%H:%M:%SZ)</td></tr>
</table>
<p>This is the known configurable-http-proxy FD/memory leak (diagnosed 2026-09-25).
Healthy baseline right after a restart is ~150 FDs / ~64MB RSS. This watchdog does
NOT auto-restart the pod. To restart manually:</p>
<pre>kubectl -n ${NAMESPACE} delete pod ${PROXY_POD}</pre>
</body></html>
HTMLEOF

send_email "⚠️ proxy-fd-watchdog ALERT | FDs=${FD_COUNT} RSS=${RSS_KB}KB" "$BODY_TMPFILE"
