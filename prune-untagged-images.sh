#!/bin/bash
# Show untagged (<none>/<none>) images and whether any are still referenced by containers.
# Offers to remove safe images and reports disk space freed.
#
# Flags:
#   --check   Scan and report only — no removal prompt (read-only)
#   --yes     Auto-confirm removal without prompting (used by lobot-tui)
#   (none)    Interactive: prompt before removing

set -euo pipefail

CHECK_ONLY=false
AUTO_YES=false
for arg in "$@"; do
    case "$arg" in
        --check) CHECK_ONLY=true ;;
        --yes)   AUTO_YES=true ;;
    esac
done

# Pin to containerd socket to suppress "trying next endpoint" warnings.
CRICTL="sudo crictl --runtime-endpoint unix:///run/containerd/containerd.sock"

disk_avail_bytes() {
    df --output=avail -B1 /var/lib/containerd 2>/dev/null | tail -1
}

disk_total_bytes() {
    df --output=size -B1 /var/lib/containerd 2>/dev/null | tail -1
}

human_bytes() {
    python3 -c "
v = $1
for unit in ['B','KB','MB','GB','TB']:
    if v < 1024 or unit == 'TB':
        print(f'{v:.1f} {unit}')
        break
    v /= 1024
"
}

disk_summary() {
    local avail total
    avail=$(disk_avail_bytes)
    total=$(disk_total_bytes)
    python3 -c "
avail, total = $avail, $total

def fmt(v):
    for unit in ['B','KB','MB','GB','TB']:
        if v < 1024 or unit == 'TB':
            return f'{v:.1f} {unit}'
        v /= 1024

pct = round(avail / total * 100) if total else 0
print(f'{fmt(avail)} free of {fmt(total)} ({pct}% available)')
"
}

# ── Scan ────────────────────────────────────────────────────────────────────

# Capture once — avoids repeated crictl calls and warnings inside the loop.
images_raw=$($CRICTL images)
mapfile -t candidates < <(echo "$images_raw" | awk '$1 == "<none>" && $2 == "<none>" {print $3}')

if [ ${#candidates[@]} -eq 0 ]; then
    echo "No untagged images found."
    exit 0
fi

echo "Scanning ${#candidates[@]} untagged image(s) for container references..."
echo ""

container_json=$($CRICTL ps -a -o json)

safe_ids=()
in_use_ids=()

for id in "${candidates[@]}"; do
    size=$(echo "$images_raw" | awk -v id="$id" '$3 == id {print $4, $5}')

    # Pipe container_json via stdin so Python has one clear input stream.
    # (Using 'python3 -' with a heredoc for code AND sys.stdin.read() for data
    # doesn't work — stdin can only be read once.)
    refs=$(echo "$container_json" | python3 -c "
import sys, json
image_id = sys.argv[1]
raw = sys.stdin.read().strip()
data = json.loads(raw) if raw else {}
for c in data.get('containers', []):
    if image_id not in c.get('imageRef', ''):
        continue
    labels = c.get('labels', {})
    pod    = labels.get('io.kubernetes.pod.name', 'unknown')
    ns     = labels.get('io.kubernetes.pod.namespace', 'unknown')
    state  = c.get('state', '').replace('CONTAINER_', '')
    cname  = c.get('metadata', {}).get('name', 'unknown')
    print(f'  {state:<12} {ns}/{pod}  (container: {cname})')
" "$id")

    if [ -z "$refs" ]; then
        printf "[SAFE]    %s  (%s)\n" "$id" "$size"
        safe_ids+=("$id")
    else
        printf "[IN USE]  %s  (%s)\n" "$id" "$size"
        echo "$refs"
        in_use_ids+=("$id")
    fi
    echo ""
done

echo "----------------------------------------"
printf "Safe to remove:          %d image(s)\n" "${#safe_ids[@]}"
printf "In use (notify users):   %d image(s)\n" "${#in_use_ids[@]}"
printf "Disk:                    %s\n" "$(disk_summary)"
echo ""

# ── Prompt & remove ─────────────────────────────────────────────────────────

if [ ${#safe_ids[@]} -eq 0 ]; then
    echo "Nothing to remove."
    exit 0
fi

if $CHECK_ONLY; then
    echo "Check-only mode — no images removed."
    exit 0
fi

if ! $AUTO_YES; then
    read -r -p "Remove ${#safe_ids[@]} safe image(s)? [y/N] " answer
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""
before=$(disk_avail_bytes)
echo "Disk before: $(disk_summary)"
echo ""

failed=0
for id in "${safe_ids[@]}"; do
    if $CRICTL rmi "$id" 2>/dev/null; then
        echo "  Removed $id"
    else
        echo "  Failed  $id (skipped)"
        (( failed++ )) || true
    fi
done

after=$(disk_avail_bytes)
freed=$(( after - before ))


echo ""
echo "Disk after:  $(disk_summary)"
printf "Space freed: %s\n" "$(human_bytes "$freed")"
[ "$failed" -gt 0 ] && echo "Warning: $failed image(s) could not be removed."
echo "Done."
