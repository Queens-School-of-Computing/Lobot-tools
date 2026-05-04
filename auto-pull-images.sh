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
FROM_EMAIL="lobot+tools@cs.queensu.ca"
TO_EMAIL="aaron.visser+lobot@queensu.ca,whb1+lobot@queensu.ca"

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
    # Extract disk space before/after sections and the SUMMARY block.
    # Skips ctr progress lines (between "=== Pulling" and the after-disk section).
    # Strips ANSI codes and layer-hash lines.
    awk '
        /=== Node:/         { print; next }
        /=== Disk space/    { in_disk=1; print; next }
        /=== Pulling/       { in_disk=0; next }
        /=== Pull complete/ { in_disk=0; next }
        /=== Pull FAILED/   { in_disk=0; next }
        /SUMMARY/           { in_summary=1 }
        in_disk             { print }
        in_summary          { print }
    ' "$PULL_LOG" | \
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
      Sent by Lobot Cluster Management
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
      Sent by Lobot Cluster Management
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

PRUNE_SCRIPT="$TOOLS_DIR/prune-untagged-images.sh"
PRUNE_SSH_USER="${LOBOT_PRUNE_SSH_USER:-croot}"
PRUNE_NODE_DOMAIN="${LOBOT_NODE_DOMAIN:-cs.queensu.ca}"

# ── Prune helper ───────────────────────────────────────────────────────────────
prune_node() {
    local NODE="$1"
    if [ ! -f "$PRUNE_SCRIPT" ]; then
        log " ⚠️  prune-untagged-images.sh not found — skipping prune for $NODE"
        return 0
    fi
    local FQDN="$NODE"
    [[ "$NODE" != *.* ]] && FQDN="${NODE}.${PRUNE_NODE_DOMAIN}"
    log " 🧹 Pruning untagged images on $NODE..."
    local PRUNE_OUT
    PRUNE_OUT=$(ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
        -o ConnectTimeout=10 \
        "${PRUNE_SSH_USER}@${FQDN}" bash -s -- --yes < "$PRUNE_SCRIPT" 2>&1) || true
    while IFS= read -r pline; do
        log "    $pline"
    done <<< "$PRUNE_OUT"
}

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
NODES_TO_PRUNE=""       # deduplicated list of nodes to prune after all pulls
declare -A NOTIFY_MAP   # email -> newline-separated list of pulled tags

# Fetch worker node list once — control-plane is excluded by label selector.
# Cache is per (tag, node) so each node tracks its own digest independently.
CLUSTER_NODES=$(kubectl get nodes --no-headers \
    -o custom-columns=":metadata.name" \
    --selector='!node-role.kubernetes.io/control-plane' 2>/dev/null) || true

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

    # Build target node list: cluster nodes minus excluded nodes
    TARGET_NODES=""
    for NODE in $CLUSTER_NODES; do
        SKIP=false
        for EXCL in $(echo "$EXCLUDE" | tr ',' ' '); do
            [ "$NODE" = "$EXCL" ] && SKIP=true && break
        done
        $SKIP || TARGET_NODES="$TARGET_NODES $NODE"
    done
    TARGET_NODES="${TARGET_NODES# }"  # trim leading space

    if [ -z "$TARGET_NODES" ]; then
        log "⚠️  No target nodes after exclusions — skipping tag"
        log ""
        continue
    fi

    TAG_SLUG=$(tag_to_slug "$TAG")
    TAG_PULLED=false
    TAG_FAILED=false

    # Collect target nodes for end-of-run prune (deduplicated)
    for NODE in $TARGET_NODES; do
        case " $NODES_TO_PRUNE " in
            *" $NODE "*) ;;
            *) NODES_TO_PRUNE="${NODES_TO_PRUNE:+$NODES_TO_PRUNE }$NODE" ;;
        esac
    done

    # Pull per node — each node has its own digest cache
    for NODE in $TARGET_NODES; do
        NODE_CACHE="$CACHE_DIR/${TAG_SLUG}___${NODE}"
        CACHED_DIGEST=""
        [ -f "$NODE_CACHE" ] && CACHED_DIGEST=$(cat "$NODE_CACHE")

        if [ "$REMOTE_DIGEST" = "$CACHED_DIGEST" ]; then
            log "⏭️  $NODE: already up to date (digest: ${REMOTE_DIGEST:0:19}...)"
            SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
            continue
        fi

        if [ -n "$CACHED_DIGEST" ]; then
            log "🔍 $NODE: new digest (was: ${CACHED_DIGEST:0:19}... now: ${REMOTE_DIGEST:0:19}...)"
        else
            log "🔍 $NODE: no cached digest — first pull"
        fi

        if [ "$DRY_RUN" = "true" ]; then
            log "   [dry-run] would run: $IMAGE_PULL -i $TAG -n $NODE --yes --noemail"
            SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
            continue
        fi

        PULL_START=$(date +%s)
        PULL_FULL_LOG=$(mktemp)
        "$IMAGE_PULL" -i "$TAG" -n "$NODE" --yes --noemail > "$PULL_FULL_LOG" 2>&1
        PULL_EXIT=$?
        PULL_ELAPSED=$(( $(date +%s) - PULL_START ))
        log_pull_summary "$PULL_FULL_LOG"
        rm -f "$PULL_FULL_LOG"

        if [ "$PULL_EXIT" -eq 0 ]; then
            log "✅ $NODE: pull complete ($(format_elapsed $PULL_ELAPSED))"
            echo "$REMOTE_DIGEST" > "$NODE_CACHE"
            PULLED_COUNT=$((PULLED_COUNT + 1))
            TAG_PULLED=true
        else
            log "❌ $NODE: pull failed ($(format_elapsed $PULL_ELAPSED))"
            FAILED_COUNT=$((FAILED_COUNT + 1))
            TAG_FAILED=true
        fi
    done

    # Notify faculty if at least one node pulled successfully for this tag
    if [ "$TAG_PULLED" = "true" ] && [ -n "$NOTIFY" ]; then
        for addr in $(echo "$NOTIFY" | tr ',' ' '); do
            if [ -z "${NOTIFY_MAP[$addr]+x}" ]; then
                NOTIFY_MAP[$addr]="$TAG"
            else
                NOTIFY_MAP[$addr]="${NOTIFY_MAP[$addr]}
$TAG"
            fi
        done
    fi

    log ""

done < "$CONFIG_FILE"

# ── Prune untagged images on all target nodes ───────────────────────────────────
if [ "$DRY_RUN" != "true" ] && [ -n "$NODES_TO_PRUNE" ]; then
    log ""
    log "=========================================="
    log " Pruning untagged images"
    log "=========================================="
    for NODE in $NODES_TO_PRUNE; do
        prune_node "$NODE"
    done
fi

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
