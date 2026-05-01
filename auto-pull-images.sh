#!/usr/bin/env bash

# ── Configuration ──────────────────────────────────────────────────────────────
CONFIG_FILE="/opt/Lobot/tools/auto-pull-images.conf"
CACHE_DIR="/opt/Lobot/tools/pull-digests"
TOOLS_DIR="${LOBOT_CLUSTER_DIR:-/opt/Lobot}/tools"
LOG_DIR="${LOBOT_CLUSTER_DIR:-/opt/Lobot}/logs"

EMAIL_ENABLED=true
SMTP_SERVER="innovate.cs.queensu.ca"
SMTP_PORT=25
SMTP_USE_TLS=false
SMTP_USERNAME=""
SMTP_PASSWORD=""
FROM_EMAIL="lobot-tools@cs.queensu.ca"
TO_EMAIL="aaron.visser@queensu.ca"

DRY_RUN=false

# ── Argument parsing ───────────────────────────────────────────────────────────
i=1
while [ $i -le $# ]; do
    arg="${!i}"
    case $arg in
        --dry-run)  DRY_RUN=true ;;
        --noemail)  EMAIL_ENABLED=false ;;
        --config)
            i=$((i + 1))
            CONFIG_FILE="${!i}" ;;
        *) echo "Unknown argument: $arg"; exit 1 ;;
    esac
    i=$((i + 1))
done

# ── Setup ──────────────────────────────────────────────────────────────────────
mkdir -p "$CACHE_DIR" "$LOG_DIR"
SCRIPT_START=$(date +%s)

if [ "$DRY_RUN" = "true" ]; then
    LOG_FILE="$LOG_DIR/auto-pull-dryrun-$(date +%Y%m%d-%H%M%S).log"
else
    LOG_FILE="$LOG_DIR/auto-pull-$(date +%Y%m%d-%H%M%S).log"
fi

# log() writes to both the email log and stdout (captured by cron to syslog/file).
# image-pull.sh output goes only to stdout — only its SUMMARY block is appended
# to LOG_FILE so the email stays compact.
log() { echo "$*" | tee -a "$LOG_FILE"; }

log_pull_summary() {
    local PULL_LOG="$1"
    # Append the SUMMARY block from image-pull.sh output to our email log.
    # Starts at the === SUMMARY === line; strips ANSI codes and layer-hash lines.
    awk '/SUMMARY/{found=1} found{print}' "$PULL_LOG" | \
        sed 's/\x1B\[[0-9;]*[A-Za-z]//g' | \
        grep -v "^[a-f0-9]\{12,64\}: " | \
        tee -a "$LOG_FILE"
}

# ── Helpers ────────────────────────────────────────────────────────────────────
format_elapsed() {
    local SECS=$1
    local H=$((SECS / 3600))
    local M=$(( (SECS % 3600) / 60 ))
    local S=$((SECS % 60))
    if [ $H -gt 0 ]; then
        printf "%dh %02dm %02ds" $H $M $S
    else
        printf "%dm %02ds" $M $S
    fi
}

tag_to_slug() {
    echo "$1" | tr '/: ' '___'
}

get_remote_digest() {
    local IMAGE="$1"
    python3 - "$IMAGE" <<'PYEOF'
import json, sys
try:
    from urllib.request import urlopen
except ImportError:
    print("ERROR: urllib not available", file=sys.stderr)
    sys.exit(1)

image = sys.argv[1]
if ':' not in image:
    print("ERROR: image must include a tag (e.g. repo/image:tag)", file=sys.stderr)
    sys.exit(1)

name, tag = image.rsplit(':', 1)
url = "https://hub.docker.com/v2/repositories/{}/tags/{}/".format(name, tag)
try:
    with urlopen(url, timeout=15) as r:
        data = json.load(r)
    digest = data.get("digest", "")
    if not digest:
        print("ERROR: no digest field in DockerHub response", file=sys.stderr)
        sys.exit(1)
    print(digest)
except Exception as e:
    print("ERROR: {}".format(e), file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ── Email helpers ──────────────────────────────────────────────────────────────
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
        log " 📧 Email sent to $TO_EMAIL"
    else
        log " ⚠️  Email failed to send"
    fi
    rm -f "$BODY_FILE"
}

build_email_body() {
    local LOG="$1"
    local STATUS="$2"

    if [ "$STATUS" = "success" ]; then
        STATUS_COLOR="#2e7d32"
        STATUS_LABEL="✅ Auto-Pull Complete"
    else
        STATUS_COLOR="#c62828"
        STATUS_LABEL="❌ Auto-Pull Completed With Errors"
    fi

    LOG_CONTENT=$(cat "$LOG" | \
        sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' | \
        sed 's/✅/<span style="color:#2e7d32">✅/g' | \
        sed 's/❌/<span style="color:#c62828">❌/g' | \
        sed 's/⚠️/<span style="color:#f57f17">⚠️/g' | \
        sed 's/🔍/<span style="color:#1565c0">🔍/g' | \
        sed 's/⏭️/<span style="color:#6a1e99">⏭️/g' | \
        sed 's/🎉/<span style="color:#2e7d32">🎉/g' | \
        awk '{print $0"</span><br>"}')

    cat <<BODYEOF
<html>
<body style="font-family: monospace; background-color: #1e1e1e; color: #d4d4d4; padding: 20px;">
  <div style="max-width: 900px; margin: 0 auto;">
    <div style="background-color: #2d2d2d; border-left: 5px solid ${STATUS_COLOR}; padding: 15px 20px; margin-bottom: 20px; border-radius: 4px;">
      <h2 style="margin: 0; color: ${STATUS_COLOR}; font-family: monospace;">${STATUS_LABEL}</h2>
      <p style="margin: 5px 0 0 0; color: #9e9e9e;">auto-pull-images.sh &mdash; $(date)</p>
    </div>
    <div style="background-color: #2d2d2d; padding: 20px; border-radius: 4px; line-height: 1.6;">
${LOG_CONTENT}
    </div>
    <div style="margin-top: 15px; color: #616161; font-size: 0.85em;">
      Sent by Lobot Cluster Management &mdash; ${SMTP_SERVER}
    </div>
  </div>
</body>
</html>
BODYEOF
}

send_notify_email() {
    local TO_ADDR="$1"
    local TAGS_LIST="$2"   # newline-separated

    local TAGS_HTML=""
    while IFS= read -r t; do
        [ -z "$t" ] && continue
        TAGS_HTML="${TAGS_HTML}<li style='margin:6px 0; color:#80cbc4;'>${t}</li>"
    done <<< "$TAGS_LIST"

    local BODY_FILE
    BODY_FILE=$(mktemp)
    cat > "$BODY_FILE" <<BODYEOF
<html>
<body style="font-family: sans-serif; background-color: #f5f5f5; padding: 30px;">
  <div style="max-width: 640px; margin: 0 auto; background: #fff; border-radius: 6px; padding: 30px; border: 1px solid #e0e0e0;">
    <h2 style="color: #2e7d32; margin-top: 0;">🆕 JupyterHub Image Updated</h2>
    <p>A new nightly image has been pulled to your cluster node(s) and is ready to use.</p>
    <p style="color: #555;">To use the updated image, stop your JupyterHub server and start a new one. The new image will be used automatically.</p>
    <p style="margin-bottom: 6px; color: #333;"><strong>Updated image(s):</strong></p>
    <ul style="font-family: monospace; font-size: 0.9em; background: #f5f5f5; padding: 14px 14px 14px 30px; border-radius: 4px;">
${TAGS_HTML}
    </ul>
    <p style="margin-top: 24px; font-size: 0.85em; color: #9e9e9e;">
      Sent by Lobot Cluster Management &mdash; ${SMTP_SERVER}
    </p>
  </div>
</body>
</html>
BODYEOF

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
to_addr     = "${TO_ADDR}"

with open("${BODY_FILE}", "r") as f:
    body = f.read()

msg = MIMEMultipart("alternative")
msg["Subject"] = "🆕 JupyterHub image updated | $(date '+%Y-%m-%d')"
msg["From"]    = f"{socket.getfqdn()} <{from_email}>"
msg["To"]      = to_addr
msg.attach(MIMEText(body, "html"))

try:
    with smtplib.SMTP(smtp_server, smtp_port) as server:
        if use_tls:
            server.starttls()
        if username and password:
            server.login(username, password)
        server.sendmail(from_email, [to_addr], msg.as_string())
    print("ok")
except Exception as e:
    print(f"error: {e}")
    exit(1)
PYEOF

    if [ $? -eq 0 ]; then
        log " 📧 Notification sent to $TO_ADDR"
    else
        log " ⚠️  Notification failed for $TO_ADDR"
    fi
    rm -f "$BODY_FILE"
}

# ── Validation ─────────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    echo "❌ Config file not found: $CONFIG_FILE"
    exit 1
fi

IMAGE_PULL="$TOOLS_DIR/image-pull.sh"
if [ ! -f "$IMAGE_PULL" ]; then
    echo "❌ image-pull.sh not found: $IMAGE_PULL"
    exit 1
fi

# ── Header ─────────────────────────────────────────────────────────────────────
log "=========================================="
if [ "$DRY_RUN" = "true" ]; then
    log " Auto-Pull Images - DRY RUN"
else
    log " Auto-Pull Images"
fi
log " $(date)"
log " Config:  $CONFIG_FILE"
log " Cache:   $CACHE_DIR"
log " Log:     $LOG_FILE"
log "=========================================="
log ""

PULLED_COUNT=0
FAILED_COUNT=0
SKIPPED_COUNT=0
declare -A NOTIFY_MAP   # email -> newline-separated list of pulled tags

# ── Main loop ──────────────────────────────────────────────────────────────────
while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// }" ]] && continue

    TAG=""
    EXCLUDE=""
    NOTIFY=""
    for token in $line; do
        case $token in
            tag=*)     TAG="${token#tag=}" ;;
            exclude=*) EXCLUDE="${token#exclude=}" ;;
            notify=*)  NOTIFY="${token#notify=}" ;;
        esac
    done

    if [ -z "$TAG" ]; then
        log "⚠️  Skipping malformed config line: $line"
        continue
    fi

    log "------------------------------------------"
    log " Tag: $TAG"
    [ -n "$EXCLUDE" ] && log " Exclude nodes: $EXCLUDE"
    [ -n "$NOTIFY" ]  && log " Notify:        $NOTIFY"
    log "------------------------------------------"

    # Query DockerHub for current digest
    DIGEST_STDERR_TMP=$(mktemp)
    REMOTE_DIGEST=$(get_remote_digest "$TAG" 2>"$DIGEST_STDERR_TMP") || true
    if [ -z "$REMOTE_DIGEST" ]; then
        log "❌ Failed to query DockerHub for: $TAG"
        [ -s "$DIGEST_STDERR_TMP" ] && log "   $(cat "$DIGEST_STDERR_TMP")"
        rm -f "$DIGEST_STDERR_TMP"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        log ""
        continue
    fi
    rm -f "$DIGEST_STDERR_TMP"

    # Compare to cache
    SLUG=$(tag_to_slug "$TAG")
    CACHE_FILE="$CACHE_DIR/$SLUG"
    CACHED_DIGEST=""
    [ -f "$CACHE_FILE" ] && CACHED_DIGEST=$(cat "$CACHE_FILE")

    if [ "$REMOTE_DIGEST" = "$CACHED_DIGEST" ]; then
        log "⏭️  No change (digest: ${REMOTE_DIGEST:0:19}...)"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        log ""
        continue
    fi

    if [ -n "$CACHED_DIGEST" ]; then
        log "🔍 New digest detected:"
        log "   was: ${CACHED_DIGEST:0:19}..."
        log "   now: ${REMOTE_DIGEST:0:19}..."
    else
        log "🔍 No cached digest — first run for this tag"
        log "   digest: ${REMOTE_DIGEST:0:19}..."
    fi

    if [ "$DRY_RUN" = "true" ]; then
        PULL_CMD_STR="$IMAGE_PULL -i $TAG --yes --noemail"
        [ -n "$EXCLUDE" ] && PULL_CMD_STR="$PULL_CMD_STR -e $EXCLUDE"
        log "   [dry-run] would run: $PULL_CMD_STR"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        log ""
        continue
    fi

    # Trigger pull — full output goes to stdout (cron log); only SUMMARY
    # is appended to LOG_FILE for the email.
    PULL_START=$(date +%s)
    PULL_FULL_LOG=$(mktemp)
    PULL_ARGS=(-i "$TAG" --yes --noemail)
    [ -n "$EXCLUDE" ] && PULL_ARGS+=(-e "$EXCLUDE")

    if "$IMAGE_PULL" "${PULL_ARGS[@]}" 2>&1 | tee "$PULL_FULL_LOG"; then
        PULL_ELAPSED=$(( $(date +%s) - PULL_START ))
        log_pull_summary "$PULL_FULL_LOG"
        log "✅ Pull complete: $TAG ($(format_elapsed $PULL_ELAPSED))"
        echo "$REMOTE_DIGEST" > "$CACHE_FILE"
        PULLED_COUNT=$((PULLED_COUNT + 1))
        if [ -n "$NOTIFY" ]; then
            for addr in $(echo "$NOTIFY" | tr ',' ' '); do
                if [ -z "${NOTIFY_MAP[$addr]+x}" ]; then
                    NOTIFY_MAP[$addr]="$TAG"
                else
                    NOTIFY_MAP[$addr]="${NOTIFY_MAP[$addr]}
$TAG"
                fi
            done
        fi
    else
        PULL_ELAPSED=$(( $(date +%s) - PULL_START ))
        log_pull_summary "$PULL_FULL_LOG"
        log "❌ Pull failed: $TAG ($(format_elapsed $PULL_ELAPSED))"
        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi
    rm -f "$PULL_FULL_LOG"
    log ""

done < "$CONFIG_FILE"

# ── Summary ────────────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(( $(date +%s) - SCRIPT_START ))

log "=========================================="
log " SUMMARY - $(date)"
log "=========================================="
log " ✅ Pulled:    $PULLED_COUNT"
log " ❌ Failed:    $FAILED_COUNT"
log " ⏭️  Unchanged: $SKIPPED_COUNT"
log " ⏱️  Total:     $(format_elapsed $TOTAL_ELAPSED)"
log "=========================================="

if [ "$DRY_RUN" = "true" ]; then
    log " [dry-run] No email sent."
    exit 0
fi

# ── Faculty notifications (one email per unique address) ───────────────────────
if [ ${#NOTIFY_MAP[@]} -gt 0 ] && [ "$EMAIL_ENABLED" = "true" ]; then
    log ""
    log " Sending faculty notifications..."
    for addr in "${!NOTIFY_MAP[@]}"; do
        send_notify_email "$addr" "${NOTIFY_MAP[$addr]}"
    done
fi

if [ $PULLED_COUNT -gt 0 ] || [ $FAILED_COUNT -gt 0 ]; then
    BODY_TMP=$(mktemp)
    if [ $FAILED_COUNT -gt 0 ]; then
        build_email_body "$LOG_FILE" "failure" > "$BODY_TMP"
        SUBJECT="❌ Auto-pull FAILED | ${PULLED_COUNT} pulled, ${FAILED_COUNT} failed | $(date '+%Y-%m-%d')"
    else
        build_email_body "$LOG_FILE" "success" > "$BODY_TMP"
        SUBJECT="✅ Auto-pull complete | ${PULLED_COUNT} pulled | $(date '+%Y-%m-%d')"
    fi
    send_email "$SUBJECT" "$BODY_TMP"
else
    log " No changes detected — no email sent."
fi

[ $FAILED_COUNT -gt 0 ] && exit 1
exit 0
