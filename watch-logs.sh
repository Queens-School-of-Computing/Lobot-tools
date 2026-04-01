#!/usr/bin/env bash
set -e

POD="$1"
NAMESPACE="${2:-jhub}"
CONTAINER="${3:-notebook}"
MAX_RETRIES="${WATCH_LOGS_MAX_RETRIES:-60}"

if [ -z "$POD" ]; then
  echo "Usage: $0 <pod-name> [namespace] [container]" >&2
  exit 1
fi

echo "[watch-logs] Watching logs for pod=$POD ns=$NAMESPACE container=$CONTAINER"

retries=0
while true; do
  # Try to attach; if it fails (e.g. PodInitializing), retry
  if kubectl logs -n "$NAMESPACE" "$POD" -c "$CONTAINER" -f; then
    # logs exited normally (container finished); break
    break
  else
    retries=$((retries + 1))
    if [ "$retries" -ge "$MAX_RETRIES" ]; then
      echo "[watch-logs] Max retries ($MAX_RETRIES) reached. Giving up." >&2
      exit 1
    fi
    echo "[watch-logs] Pod or container not ready yet, retrying in 2s... ($retries/$MAX_RETRIES)"
    sleep 2
  fi
done
