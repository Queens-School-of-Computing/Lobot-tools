#!/bin/bash
# jhub-pod-diag.sh — JupyterHub frozen pod diagnostics for Lobot
# Usage: ./jhub-pod-diag.sh <username|pod-name>
#   e.g. ./jhub-pod-diag.sh 23wm13
#   e.g. ./jhub-pod-diag.sh jupyter-23wm13
#
# Collects everything needed to diagnose a 504/frozen pod without
# touching the pod or restarting anything.

set -euo pipefail

ARG="${1:-}"
NAMESPACE="jhub"
PROXY_TOKEN=""
POD_NAME=""
USERNAME=""

if [[ -z "$ARG" ]]; then
  echo "Usage: $0 <jupyterhub-username|pod-name>"
  echo "  e.g. $0 23wm13"
  echo "  e.g. $0 jupyter-23wm13"
  exit 1
fi

# If arg looks like a pod name, use it directly and derive the username from labels.
if [[ "$ARG" == jupyter-* ]]; then
  POD_NAME="$ARG"
  USERNAME=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
    -o jsonpath='{.metadata.labels.hub\.jupyter\.org/username}' 2>/dev/null || true)
  [[ -z "$USERNAME" ]] && USERNAME="${POD_NAME#jupyter-}"
else
  USERNAME="$ARG"
fi

# ── Colours (suppressed when stdout is not a TTY, e.g. when called from a TUI) ─
if [ -t 1 ]; then
  RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'
  CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
else
  RED=''; YELLOW=''; GREEN=''; CYAN=''; BOLD=''; RESET=''
fi

section()  { echo -e "\n${BOLD}${CYAN}══ $* ══${RESET}"; }
ok()       { echo -e "  ${GREEN}✓${RESET} $*"; }
warn()     { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
bad()      { echo -e "  ${RED}✗${RESET} $*"; }
info()     { echo -e "  ${RESET}  $*"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
get_proxy_token() {
  PROXY_POD=$(kubectl get pods -n "$NAMESPACE" -l component=proxy \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)
  if [[ -n "$PROXY_POD" ]]; then
    PROXY_TOKEN=$(kubectl exec -n "$NAMESPACE" "$PROXY_POD" -- \
      env 2>/dev/null | grep CONFIGPROXY_AUTH_TOKEN | cut -d= -f2)
  fi
}

# ── Banner ────────────────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║        JupyterHub Pod Diagnostics — Lobot            ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${RESET}"
echo "  Input     : $ARG"
echo "  Username  : $USERNAME"
echo "  Namespace : $NAMESPACE"
echo "  Timestamp : $(date -u '+%Y-%m-%d %H:%M:%S UTC')"

# ── 1. Find pod ───────────────────────────────────────────────────────────────
section "1. Pod lookup"

if [[ -z "$POD_NAME" ]]; then
  POD_NAME=$(kubectl get pods -n "$NAMESPACE" \
    -l "hub.jupyter.org/username=${USERNAME}" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
fi

if [[ -z "$POD_NAME" ]]; then
  bad "No pod found with label hub.jupyter.org/username=${USERNAME}"
  info "Trying name-based lookup..."
  # Try common escaped variants
  for candidate in "jupyter-${USERNAME}" "jupyter-x-${USERNAME}"; do
    if kubectl get pod -n "$NAMESPACE" "$candidate" &>/dev/null; then
      POD_NAME="$candidate"
      break
    fi
  done
  # Fuzzy match on escaped name
  if [[ -z "$POD_NAME" ]]; then
    POD_NAME=$(kubectl get pods -n "$NAMESPACE" \
      -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | \
      grep -i "${USERNAME}" | head -1 || true)
  fi
fi

if [[ -z "$POD_NAME" ]]; then
  bad "Could not find any pod for user '$USERNAME'. Is the server running?"
  exit 1
fi

ok "Found pod: $POD_NAME"

POD_INFO=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" -o wide 2>/dev/null)
POD_IP=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.status.podIP}' 2>/dev/null)
POD_NODE=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.spec.nodeName}' 2>/dev/null)
POD_AGE=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.status.startTime}' 2>/dev/null)
POD_RESTARTS=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null)
POD_STATUS=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.status.phase}' 2>/dev/null)

info "Status   : $POD_STATUS"
info "Pod IP   : $POD_IP"
info "Node     : $POD_NODE"
info "Started  : $POD_AGE"
info "Restarts : $POD_RESTARTS"

[[ "$POD_STATUS" == "Running" ]] && ok "Pod is Running" || bad "Pod is NOT Running (phase: $POD_STATUS)"

# ── 2. Resource limits ────────────────────────────────────────────────────────
section "2. Resource limits"

RESOURCES=$(kubectl get pod -n "$NAMESPACE" "$POD_NAME" \
  -o jsonpath='{.spec.containers[0].resources}' 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "{}")
info "Container resource spec:"
echo "$RESOURCES" | sed 's/^/    /'

# ── 3. Process check ──────────────────────────────────────────────────────────
section "3. Process state inside pod"

PS_OUT=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -- \
  ps aux --sort=-%cpu 2>/dev/null || echo "EXEC_FAILED")

if [[ "$PS_OUT" == "EXEC_FAILED" ]]; then
  bad "Could not exec into pod"
else
  ok "Process list (top by CPU):"
  echo "$PS_OUT" | head -20 | sed 's/^/    /'

  # Check jupyter process
  JUPYTER_PID=$(echo "$PS_OUT" | grep jupyterhub-singleuser | grep -v grep | awk '{print $2}' | head -1)
  if [[ -n "$JUPYTER_PID" ]]; then
    ok "Jupyter singleuser process found (PID $JUPYTER_PID)"

    JUPYTER_CPU=$(echo "$PS_OUT" | grep jupyterhub-singleuser | grep -v grep | awk '{print $3}' | head -1)
    JUPYTER_MEM=$(echo "$PS_OUT" | grep jupyterhub-singleuser | grep -v grep | awk '{print $4}' | head -1)
    JUPYTER_TIME=$(echo "$PS_OUT" | grep jupyterhub-singleuser | grep -v grep | awk '{print $10}' | head -1)
    info "CPU: ${JUPYTER_CPU}%  MEM: ${JUPYTER_MEM}%  Cumulative CPU time: ${JUPYTER_TIME}"

    # Thread states
    section "3a. Jupyter thread states (PID $JUPYTER_PID)"
    THREAD_STATES=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -- \
      sh -c "for t in /proc/${JUPYTER_PID}/task/*; do
               tid=\$(basename \$t)
               state=\$(cat \$t/status 2>/dev/null | grep '^State' | awk '{print \$2,\$3}')
               wchan=\$(cat \$t/wchan 2>/dev/null)
               echo \"  TID \$tid: \$state  wchan=\$wchan\"
             done" 2>/dev/null || echo "  Could not read thread states")
    echo "$THREAD_STATES"

    D_COUNT=$(echo "$THREAD_STATES" | grep -c "D (disk" || true)
    if [[ "$D_COUNT" -gt 0 ]]; then
      bad "$D_COUNT thread(s) in uninterruptible D-state — likely I/O block"
    else
      ok "No threads in D-state"
    fi

    # Check if event loop is spinning (wchan=0 on all threads = userspace)
    ZERO_COUNT=$(echo "$THREAD_STATES" | grep -c "wchan=0" || true)
    TOTAL_THREADS=$(echo "$THREAD_STATES" | grep -c "TID" || true)
    if [[ "$ZERO_COUNT" -eq "$TOTAL_THREADS" ]] && [[ "$TOTAL_THREADS" -gt 0 ]]; then
      warn "All $TOTAL_THREADS threads have wchan=0 (running in userspace) — possible event loop spin/deadlock"
    fi

  else
    bad "No jupyterhub-singleuser process found!"
  fi

  # Flag heavy non-jupyter processes
  section "3b. Notable non-system processes"
  echo "$PS_OUT" | awk 'NR>1 && $3+0 > 5 && !/jupyterhub/ && !/ps/ {print "    "$0}'
  HEAVY=$(echo "$PS_OUT" | awk 'NR>1 && $3+0 > 5 && !/jupyterhub/ && !/ps/' | wc -l)
  [[ "$HEAVY" -gt 0 ]] && warn "$HEAVY process(es) consuming >5% CPU besides Jupyter" \
                        || ok "No other processes consuming >5% CPU"
fi

# ── 4. GPU state ──────────────────────────────────────────────────────────────
section "4. GPU state"

GPU_OUT=$(kubectl exec -n "$NAMESPACE" "$POD_NAME" -- \
  sh -c 'nvidia-smi 2>/dev/null || echo "NO_GPU"' 2>/dev/null)

if echo "$GPU_OUT" | grep -q "NO_GPU\|not found"; then
  info "No GPU / nvidia-smi not available in this pod"
else
  echo "$GPU_OUT" | sed 's/^/    /'
  if echo "$GPU_OUT" | grep -q "No running processes found"; then
    ok "No GPU processes (GPU idle)"
  else
    GPU_PROCS=$(echo "$GPU_OUT" | grep -E "^\| +[0-9]+ +[0-9]" | wc -l || true)
    [[ "$GPU_PROCS" -gt 0 ]] && warn "$GPU_PROCS GPU process(es) running" \
                               || ok "No GPU processes (GPU idle)"
  fi
fi

# ── 5. HTTP reachability ──────────────────────────────────────────────────────
section "5. HTTP reachability (hub → pod)"

HUB_POD=$(kubectl get pods -n "$NAMESPACE" -l component=hub \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [[ -z "$HUB_POD" ]]; then
  bad "Could not find hub pod"
else
  HTTP_CODE=$(kubectl exec -n "$NAMESPACE" "$HUB_POD" -- \
    curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
    "http://${POD_IP}:8888/user/${USERNAME}/api" 2>/dev/null) || true
  [[ -z "$HTTP_CODE" || "$HTTP_CODE" == "000" ]] && HTTP_CODE="TIMEOUT"

  case "$HTTP_CODE" in
    200|401) ok "Hub → pod HTTP: ${HTTP_CODE} (reachable)" ;;
    TIMEOUT) bad "Hub → pod HTTP: TIMED OUT — Jupyter event loop is likely frozen" ;;
    *)       warn "Hub → pod HTTP: ${HTTP_CODE}" ;;
  esac
fi

# ── 6. Proxy route ────────────────────────────────────────────────────────────
section "6. Proxy route registration"

get_proxy_token

PROXY_POD=$(kubectl get pods -n "$NAMESPACE" -l component=proxy \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

ROUTE_FOUND=false

if [[ -z "$PROXY_TOKEN" ]] || [[ -z "$PROXY_POD" ]]; then
  warn "Could not get proxy token or pod — skipping route check"
else
  ROUTES_RAW=$(kubectl exec -n "$NAMESPACE" "$PROXY_POD" -- \
    curl -s -H "Authorization: token ${PROXY_TOKEN}" \
    http://localhost:8001/api/routes 2>/dev/null || echo "{}")

  ROUTE_PARSE=$(echo "$ROUTES_RAW" | python3 -c "
import sys, json
try:
    routes = json.load(sys.stdin)
    key = '/user/${USERNAME}'
    if key in routes:
        r = routes[key]
        print('FOUND')
        print(f'  target       : {r.get(\"target\",\"?\")}')
        print(f'  last_activity: {r.get(\"last_activity\",\"?\")}')
        print(f'  jupyterhub   : {r.get(\"jupyterhub\",\"?\")}')
        print(f'TARGET={r.get(\"target\",\"\")}')
    else:
        print('MISSING')
except Exception as e:
    print(f'PARSE_ERROR: {e}')
" 2>/dev/null || echo "PARSE_ERROR")

  if echo "$ROUTE_PARSE" | grep -q "^MISSING"; then
    bad "No proxy route found for /user/${USERNAME} — this will cause 504"
  elif echo "$ROUTE_PARSE" | grep -q "PARSE_ERROR"; then
    warn "Could not parse proxy routes"
  else
    ROUTE_FOUND=true
    ok "Proxy route exists:"
    echo "$ROUTE_PARSE" | grep -v "^FOUND\|^TARGET=" 
    ROUTE_TARGET=$(echo "$ROUTE_PARSE" | grep "^TARGET=" | cut -d= -f2)
    if [[ "${ROUTE_TARGET:-}" == "http://${POD_IP}:8888" ]]; then
      ok "Route target matches current pod IP"
    elif [[ -n "${ROUTE_TARGET:-}" ]]; then
      bad "Route target (${ROUTE_TARGET}) does NOT match pod IP (${POD_IP}) — stale route!"
    fi
  fi
fi

# ── 7. Recent pod logs ────────────────────────────────────────────────────────
section "7. Recent pod logs (last 30 lines)"

kubectl logs -n "$NAMESPACE" "$POD_NAME" --tail=30 2>/dev/null | sed 's/^/    /'

# Check for log silence (last log timestamp vs now)
LAST_LOG_TIME=$(kubectl logs -n "$NAMESPACE" "$POD_NAME" --tail=1 2>/dev/null | \
  grep -oP '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}' | head -1 || true)

if [[ -n "$LAST_LOG_TIME" ]]; then
  LAST_EPOCH=$(date -d "$LAST_LOG_TIME UTC" +%s 2>/dev/null || echo 0)
  NOW_EPOCH=$(date +%s)
  SILENCE=$(( (NOW_EPOCH - LAST_EPOCH) / 60 ))
  if [[ "$SILENCE" -gt 10 ]]; then
    warn "Last log entry was ${SILENCE} minutes ago — event loop may be frozen"
  else
    ok "Last log entry ${SILENCE} minute(s) ago (looks active)"
  fi
fi

# ── 8. YDoc / collaboration file watchers ────────────────────────────────────
section "8. YDoc file watcher state"

YDOC_LOGS=$(kubectl logs -n "$NAMESPACE" "$POD_NAME" --tail=200 2>/dev/null | \
  grep -E "YDoc|YStore|collaboration|Watching file|FileLoader|out-of-sync" || true)

if [[ -z "$YDOC_LOGS" ]]; then
  info "No recent YDoc log entries"
else
  WATCH_COUNT=$(echo "$YDOC_LOGS" | grep -c "Watching file" || true)
  SYNC_ISSUES=$(echo "$YDOC_LOGS" | grep -c "out-of-sync" || true)
  info "Files being watched by YDoc: $WATCH_COUNT"
  [[ "$SYNC_ISSUES" -gt 0 ]] && warn "$SYNC_ISSUES out-of-sync YDoc entries" \
                               || ok "No out-of-sync YDoc entries"
  echo "$YDOC_LOGS" | tail -20 | sed 's/^/    /'
fi

# ── 9. Node pressure ──────────────────────────────────────────────────────────
section "9. Node resource pressure ($POD_NODE)"

kubectl describe node "$POD_NODE" 2>/dev/null | \
  grep -A6 "Allocated resources" | sed 's/^/    /'

NODE_CONDITIONS_JSON=$(kubectl get node "$POD_NODE" \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status}{"\n"}{end}' 2>/dev/null)
info "Node conditions:"
echo "$NODE_CONDITIONS_JSON" | sed 's/^/    /'

# Check each condition correctly (True = problem for pressure conditions, True = good for Ready)
echo "$NODE_CONDITIONS_JSON" | grep -q "^MemoryPressure=True" && bad "Node MemoryPressure!" || true
echo "$NODE_CONDITIONS_JSON" | grep -q "^DiskPressure=True"   && bad "Node DiskPressure!"   || true
echo "$NODE_CONDITIONS_JSON" | grep -q "^PIDPressure=True"    && bad "Node PIDPressure!"    || true
echo "$NODE_CONDITIONS_JSON" | grep -q "^Ready=True"          && ok  "Node is Ready"        || bad "Node NOT Ready"

# ── 10. Summary ───────────────────────────────────────────────────────────────
section "10. Summary & recommended action"

FROZEN=false
NO_ROUTE=false
STALE_ROUTE=false

[[ "$HTTP_CODE" == "TIMEOUT" ]] && FROZEN=true
$ROUTE_FOUND || NO_ROUTE=true
[[ -n "${ROUTE_TARGET:-}" ]] && [[ "${ROUTE_TARGET}" != "http://${POD_IP}:8888" ]] && STALE_ROUTE=true

# FROZEN takes priority over NO_ROUTE if both are true (route may have been lost after freeze)
if $FROZEN && $NO_ROUTE; then
  bad "DIAGNOSIS: Jupyter event loop frozen AND proxy route missing"
  info "Fix step 1 — Restart jupyter process:"
  info "  kubectl exec -n jhub ${POD_NAME} -- kill -3 ${JUPYTER_PID:-<jupyter-pid>}"
  info ""
  info "Fix step 2 — After restart (~15s), re-register proxy route:"
  info "  PROXY_POD=\$(kubectl get pods -n jhub -l component=proxy -o jsonpath='{.items[0].metadata.name}')"
  info "  TOKEN=\"${PROXY_TOKEN}\""
  info "  kubectl exec -n jhub \$PROXY_POD -- curl -s -X POST \\"
  info "    -H \"Authorization: token \$TOKEN\" \\"
  info "    -H \"Content-Type: application/json\" \\"
  info "    -d '{\"target\": \"http://${POD_IP}:8888\"}' \\"
  info "    http://localhost:8001/api/routes/user/${USERNAME}"
  info ""
  info "⚠  Tell user: close any .log files open in JupyterLab before reconnecting"
elif $NO_ROUTE; then
  bad "DIAGNOSIS: Missing proxy route (pod is reachable, just not routed)"
  info "Fix: re-register route with CHP proxy API"
  info "  PROXY_POD=\$(kubectl get pods -n jhub -l component=proxy -o jsonpath='{.items[0].metadata.name}')"
  info "  TOKEN=\"${PROXY_TOKEN}\""
  info "  kubectl exec -n jhub \$PROXY_POD -- curl -s -X POST \\"
  info "    -H \"Authorization: token \$TOKEN\" \\"
  info "    -H \"Content-Type: application/json\" \\"
  info "    -d '{\"target\": \"http://${POD_IP}:8888\"}' \\"
  info "    http://localhost:8001/api/routes/user/${USERNAME}"
elif $STALE_ROUTE; then
  bad "DIAGNOSIS: Stale proxy route (wrong target IP)"
  info "Fix: delete and re-add the route"
  info "  TOKEN=\"${PROXY_TOKEN}\""
  info "  kubectl exec -n jhub \$PROXY_POD -- curl -s -X DELETE \\"
  info "    -H \"Authorization: token \$TOKEN\" \\"
  info "    http://localhost:8001/api/routes/user/${USERNAME}"
  info "  # then re-add with correct IP: ${POD_IP}"
elif $FROZEN; then
  bad "DIAGNOSIS: Jupyter event loop frozen (route OK, pod running, HTTP unresponsive)"
  info "Likely cause: YDoc file watcher blocking tornado IOLoop (check section 8)"
  info ""
  info "Tell user: close any actively-written .log or data files open in JupyterLab"
  info ""
  info "Fix — restart jupyter process (preserves running jobs/terminals):"
  info "  kubectl exec -n jhub ${POD_NAME} -- kill -3 ${JUPYTER_PID:-<jupyter-pid>}"
  info "  # SIGQUIT restarts jupyter on this image; wait ~15s then verify with this script"
  HEAVY_PIDS=$(echo "${PS_OUT:-}" | awk 'NR>1 && $3+0 > 5 && !/jupyterhub/ && !/ps/ {print $2}' | tr '\n' ' ' || true)
  [[ -n "$HEAVY_PIDS" ]] && info "  Consider also killing heavy background PIDs first: $HEAVY_PIDS"
else
  ok "DIAGNOSIS: No obvious issue detected"
  info "Pod is running, route is correct, HTTP is reachable."
  info "If user is still getting 504, check ingress/nginx timeout annotations."
fi

echo -e "\n${BOLD}Diagnostics complete.${RESET}\n"