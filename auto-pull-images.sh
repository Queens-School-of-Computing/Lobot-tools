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
FROM_EMAIL="lobot@cs.queensu.ca"
TO_EMAIL="aaron@cs.queensu.ca,whb1@queensu.ca"

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

exec > >(tee "$LOG_FILE") 2>&1

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
        echo " 📧 Email sent to $TO_EMAIL"
    else
        echo " ⚠️  Email failed to send"
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
        sed 's/\x1B\[[0-9;]*[mGKHF]//g' | \
        sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g' | \
        sed 's/✅/<span style="color:#2e7d32">✅/g' | \
        sed 's/❌/<span style="color:#c62828">❌/g' | \
        sed 's/⚠️/<span style="color:#f57f17">⚠️/g' | \
        sed 's/🔍/<span style="color:#1565c0">🔍/g' | \
        sed 's/⏭️/<span style="color:#6a1e99">⏭️/g' | \
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
echo "=========================================="
if [ "$DRY_RUN" = "true" ]; then
    echo " Auto-Pull Images - DRY RUN"
else
    echo " Auto-Pull Images"
fi
echo " $(date)"
echo " Config:  $CONFIG_FILE"
echo " Cache:   $CACHE_DIR"
echo " Log:     $LOG_FILE"
echo "=========================================="
echo ""

PULLED_COUNT=0
FAILED_COUNT=0
SKIPPED_COUNT=0

# ── Main loop ──────────────────────────────────────────────────────────────────
while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line// }" ]] && continue

    TAG=""
    EXCLUDE=""
    for token in $line; do
        case $token in
            tag=*)     TAG="${token#tag=}" ;;
            exclude=*) EXCLUDE="${token#exclude=}" ;;
        esac
    done

    if [ -z "$TAG" ]; then
        echo "⚠️  Skipping malformed config line: $line"
        continue
    fi

    echo "------------------------------------------"
    echo " Tag: $TAG"
    [ -n "$EXCLUDE" ] && echo " Exclude nodes: $EXCLUDE"
    echo "------------------------------------------"

    # Query DockerHub for current digest
    DIGEST_STDERR_TMP=$(mktemp)
    REMOTE_DIGEST=$(get_remote_digest "$TAG" 2>"$DIGEST_STDERR_TMP") || true
    if [ -z "$REMOTE_DIGEST" ]; then
        echo "❌ Failed to query DockerHub for: $TAG"
        [ -s "$DIGEST_STDERR_TMP" ] && echo "   $(cat "$DIGEST_STDERR_TMP")"
        rm -f "$DIGEST_STDERR_TMP"
        FAILED_COUNT=$((FAILED_COUNT + 1))
        echo ""
        continue
    fi
    rm -f "$DIGEST_STDERR_TMP"

    # Compare to cache
    SLUG=$(tag_to_slug "$TAG")
    CACHE_FILE="$CACHE_DIR/$SLUG"
    CACHED_DIGEST=""
    [ -f "$CACHE_FILE" ] && CACHED_DIGEST=$(cat "$CACHE_FILE")

    if [ "$REMOTE_DIGEST" = "$CACHED_DIGEST" ]; then
        echo "⏭️  No change (digest: ${REMOTE_DIGEST:0:19}...)"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        echo ""
        continue
    fi

    if [ -n "$CACHED_DIGEST" ]; then
        echo "🔍 New digest detected:"
        echo "   was: ${CACHED_DIGEST:0:19}..."
        echo "   now: ${REMOTE_DIGEST:0:19}..."
    else
        echo "🔍 No cached digest — first run for this tag"
        echo "   digest: ${REMOTE_DIGEST:0:19}..."
    fi

    if [ "$DRY_RUN" = "true" ]; then
        PULL_CMD_STR="$IMAGE_PULL -i $TAG --yes --noemail"
        [ -n "$EXCLUDE" ] && PULL_CMD_STR="$PULL_CMD_STR -e $EXCLUDE"
        echo "   [dry-run] would run: $PULL_CMD_STR"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
        echo ""
        continue
    fi

    # Trigger pull
    PULL_START=$(date +%s)
    PULL_ARGS=(-i "$TAG" --yes --noemail)
    [ -n "$EXCLUDE" ] && PULL_ARGS+=(-e "$EXCLUDE")

    if "$IMAGE_PULL" "${PULL_ARGS[@]}"; then
        PULL_ELAPSED=$(( $(date +%s) - PULL_START ))
        echo "✅ Pull complete: $TAG ($(format_elapsed $PULL_ELAPSED))"
        echo "$REMOTE_DIGEST" > "$CACHE_FILE"
        PULLED_COUNT=$((PULLED_COUNT + 1))
    else
        PULL_ELAPSED=$(( $(date +%s) - PULL_START ))
        echo "❌ Pull failed: $TAG ($(format_elapsed $PULL_ELAPSED))"
        FAILED_COUNT=$((FAILED_COUNT + 1))
    fi
    echo ""

done < "$CONFIG_FILE"

# ── Summary ────────────────────────────────────────────────────────────────────
TOTAL_ELAPSED=$(( $(date +%s) - SCRIPT_START ))

echo "=========================================="
echo " SUMMARY - $(date)"
echo "=========================================="
echo " ✅ Pulled:    $PULLED_COUNT"
echo " ❌ Failed:    $FAILED_COUNT"
echo " ⏭️  Unchanged: $SKIPPED_COUNT"
echo " ⏱️  Total:     $(format_elapsed $TOTAL_ELAPSED)"
echo "=========================================="

if [ "$DRY_RUN" = "true" ]; then
    echo " [dry-run] No email sent."
    exit 0
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
    echo " No changes detected — no email sent."
fi

[ $FAILED_COUNT -gt 0 ] && exit 1
exit 0
