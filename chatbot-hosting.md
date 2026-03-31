# Hosting an External Docker Image on the Lobot Cluster

Aaron Visser — Created: March 2026

---

## Overview

Steps to deploy an arbitrary Docker image (e.g. an LLM chatbot) on the Lobot
Kubernetes cluster and expose it publicly via the existing nginx reverse proxy.

The pattern mirrors how Longhorn is exposed: the Kubernetes Service uses a
NodePort, direct access to that port is blocked by iptables, and nginx on the
control plane proxies requests through.

---

## Step 1 — Create the Namespace and Deployment

Create a manifest file, e.g. `chatbot-deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chatbot
  namespace: chatbot
spec:
  replicas: 1
  selector:
    matchLabels:
      app: chatbot
  template:
    metadata:
      labels:
        app: chatbot
    spec:
      nodeSelector:
        lab: lobot_a16        # pin to a specific lab node; change as needed
      containers:
      - name: chatbot
        image: <registry>/<image>:<tag>
        ports:
        - containerPort: 8080   # match the port the app listens on
        resources:
          limits:
            nvidia.com/gpu: 1   # number of GPU slots to request
---
apiVersion: v1
kind: Service
metadata:
  name: chatbot
  namespace: chatbot
spec:
  type: NodePort
  selector:
    app: chatbot
  ports:
  - port: 8080
    targetPort: 8080
    nodePort: 30002             # pick an unused port (30000–32767)
```

Apply it:

```bash
kubectl create namespace chatbot
kubectl apply -f chatbot-deployment.yaml
```

### Node pinning

The `nodeSelector` field pins the pod to nodes with the matching label. The
cluster already uses `lab=mulab` style labels applied during node setup:

```bash
kubectl label nodes $nodename lab=mulab
```

To pin to an exact node by hostname instead of lab label:

```yaml
      nodeSelector:
        kubernetes.io/hostname: <exact-node-name>
```

To move the pod to a different node, either update the label on the node or
change the `nodeSelector` value and re-apply the manifest.

---

## Step 2 — Block Direct NodePort Access

Prevent outside traffic from hitting the NodePort directly, forcing all access
through nginx:

```bash
sudo iptables -t raw -A PREROUTING -p tcp --dport 30002 ! -s 127.0.0.1 -j DROP
```

Persist the rule the same way the Longhorn rule (port 30001) is persisted on
the control plane.

---

## Step 3 — Add the Nginx Location Block

On the control plane, add a location block to the nginx site config (same file
that contains the Longhorn block):

```nginx
location /chatbot/ {
    proxy_pass http://127.0.0.1:30002/;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-Host $host;
    proxy_set_header X-Forwarded-Prefix /chatbot;

    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_redirect off;

    # Required for LLM streaming (SSE / token-by-token responses)
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

Reload nginx:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

The app will be accessible at `https://<cluster-hostname>/chatbot/`.

---

## Notes

### GPU resources and accounting

Kubernetes tracks `nvidia.com/gpu` as an integer resource. When a pod requests
1 GPU slot, that slot is subtracted from the node's available count and cannot
be scheduled to another pod — so your mental model of "8 GPUs, chatbot takes 1,
7 left for JupyterHub pods" is correct **as far as Kubernetes is concerned**.

**Important caveat — time slicing on the A16 node:** The A16 node uses GPU time
slicing (`replicas: 4` in `time-slicing-config-fine`), meaning each physical
GPU is advertised as 4 schedulable slots. So `kubectl describe node` may show
e.g. 16 allocatable `nvidia.com/gpu` for 4 physical GPUs. Kubernetes still
subtracts slots correctly, but:

- Time-sliced slots are **not isolated** — multiple pods sharing a physical GPU
  run concurrently and compete for VRAM and compute.
- There is **no VRAM enforcement**. If the chatbot model is large and fills the
  physical GPU's VRAM, other pods on the same physical GPU will OOM.

If the chatbot needs a dedicated physical GPU, either:
1. Request all 4 slots for one physical GPU (`nvidia.com/gpu: 4`), or
2. Disable time slicing for the node and use exclusive GPU assignment.

To check current GPU allocation:

```bash
kubectl describe node <nodename> | grep -A5 "Allocated resources"
```

---

### Streaming / LLM-specific settings
- **`proxy_buffering off`** — nginx buffers responses by default; this must be
  disabled or streamed tokens will be held until the full response is complete.
- **`proxy_read_timeout 300s`** — the default 60 s timeout will kill slow
  inferences. Adjust as needed for the model being served.

### Subpath compatibility
The `proxy_pass` trailing slash rewrites `/chatbot/foo` → `/foo` on the
upstream. This works if the app is path-agnostic. If the app hardcodes absolute
paths in its HTML/JS (e.g. `src="/static/app.js"`), the assets will 404.
Options:
- Ask the developers to set a base-path environment variable (common in React/
  Next.js apps via `NEXT_PUBLIC_BASE_PATH=/chatbot`).
- Or expose the app on a subdomain instead (avoids all subpath issues):
  `chatbot.lobot-dev.cs.queensu.ca`.

### Access control
This config is open to the public. To restrict to campus IPs (like Longhorn),
add the allow/deny block before `proxy_pass`:

```nginx
    allow 130.15.0.0/16;
    deny all;
```

To add HTTP basic auth (like Longhorn):

```nginx
    auth_basic "Chatbot";
    auth_basic_user_file /etc/nginx/.htpasswd-chatbot;
```

Create the password file:

```bash
sudo htpasswd -c /etc/nginx/.htpasswd-chatbot <username>
```
