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

Create a manifest file, e.g. `chatbot-deployment.yaml`. Include every resource
in one file — Deployment, Service, and any companion services (see Step 1b).

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
        env:
        - name: SOME_REQUIRED_VAR
          value: "value"        # check the app's docs for required env vars
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

> **Important:** Every `kind: Service` that the app needs must be explicitly
> defined in the yaml. Deployments do not create Services automatically. If a
> Service is missing, in-cluster DNS will fail with `ENOTFOUND` even if the pod
> is running.

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

## Step 1b — Companion Services (e.g. a Database)

Some apps require a database or other companion service. Deploy it in the same
namespace and same manifest file.

### MongoDB example (with Longhorn persistent storage)

```yaml
# ── PVC ────────────────────────────────────────────────────────────────────────
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mongodb-data
  namespace: chatbot
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  storageClassName: longhorn
---
# ── MongoDB Deployment ─────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mongodb
  namespace: chatbot
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mongodb
  template:
    metadata:
      labels:
        app: mongodb
    spec:
      nodeSelector:
        lab: lobot_a16
      containers:
      - name: mongodb
        image: mongo:4.4        # see MongoDB AVX note below
        ports:
        - containerPort: 27017
        volumeMounts:
        - name: mongo-data
          mountPath: /data/db
      volumes:
      - name: mongo-data
        persistentVolumeClaim:
          claimName: mongodb-data
---
# ── MongoDB Service ────────────────────────────────────────────────────────────
apiVersion: v1
kind: Service
metadata:
  name: mongodb
  namespace: chatbot
spec:
  selector:
    app: mongodb
  ports:
  - port: 27017
    targetPort: 27017
```

Connect to it from the app using the **fully qualified in-cluster DNS name**:

```
mongodb://mongodb.<namespace>.svc.cluster.local:27017/<dbname>
```

> **Do not use just `mongodb`** as the hostname — short names can fail to
> resolve depending on the pod's DNS search path. The full FQDN always works.

> **MongoDB AVX:** MongoDB 5.0+ requires AVX CPU support. The Lobot cluster
> nodes do not have AVX. Use `mongo:4.4` — it is the last version that works
> without AVX.

---

## Step 2 — Block Direct NodePort Access

Prevent outside traffic from hitting the NodePort directly, forcing all access
through nginx:

```bash
sudo iptables -t raw -A PREROUTING -p tcp --dport 30002 ! -s 127.0.0.1 -j DROP
```

Persist the rule the same way the Longhorn rule (port 30001) is persisted on
the control plane.

> **Only apply this rule to Kubernetes NodePorts**, not to nginx listener ports.
> See the Subpath Compatibility note below.

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
be scheduled to another pod — so "8 GPUs, chatbot takes 1, 7 left for JupyterHub
pods" is correct **as far as Kubernetes is concerned**.

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

---

### Subpath compatibility

The `proxy_pass` trailing slash rewrites `/chatbot/foo` → `/foo` on the
upstream. This works if the app is path-agnostic. If the app hardcodes absolute
paths in its HTML/JS (e.g. `src="/static/app.js"`), the assets will 404 — this
is common with Vite/React SPAs where asset paths are baked in at build time.

**If the app does not support subpath deployment, use a dedicated port instead.**
Add a separate `server` block in the same nginx config file (not inside the
existing one) listening on a different port, e.g. 8443:

```nginx
server {
    listen 8443 ssl;
    server_name lobot-dev.cs.queensu.ca;

    ssl_certificate /etc/letsencrypt/live/lobot-dev.cs.queensu.ca/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lobot-dev.cs.queensu.ca/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        allow 130.15.1.0/24;
        allow 130.15.2.0/24;
        allow 130.15.3.0/24;
        allow 130.15.4.0/24;
        allow 130.15.5.0/24;
        allow 130.15.6.0/24;
        allow 130.15.7.0/24;
        deny all;

        proxy_pass http://127.0.0.1:<nodePort>/;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_redirect off;

        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

**Do NOT add an iptables DROP rule for this port.** Unlike NodePort rules
(30001, 32720, etc.), this port is nginx itself and must accept external traffic.
If you accidentally add a DROP rule for it, remove it with:

```bash
sudo iptables -t raw -D PREROUTING -p tcp --dport 8443 ! -s 127.0.0.1 -j DROP
```

The app will be accessible at `https://<cluster-hostname>:8443`.

---

### Access control

To restrict to campus IPs, add the allow/deny block before `proxy_pass`:

```nginx
    allow 130.15.1.0/24;
    allow 130.15.2.0/24;
    allow 130.15.3.0/24;
    allow 130.15.4.0/24;
    allow 130.15.5.0/24;
    allow 130.15.6.0/24;
    allow 130.15.7.0/24;
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

---

## Appendix — LibreChat Deployment

LibreChat is a Vite/React SPA and does not support subpath deployment. It is
exposed on port 8443 using the dedicated port pattern above. It requires MongoDB
and several mandatory environment variables.

### Complete manifest (`librechat-deployment.yaml`)

```yaml
# ── MongoDB PVC ────────────────────────────────────────────────────────────────
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: mongodb-data
  namespace: librechat
spec:
  accessModes:
  - ReadWriteOnce
  resources:
    requests:
      storage: 10Gi
  storageClassName: longhorn
---
# ── MongoDB ────────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mongodb
  namespace: librechat
spec:
  replicas: 1
  selector:
    matchLabels:
      app: mongodb
  template:
    metadata:
      labels:
        app: mongodb
    spec:
      nodeSelector:
        lab: lobot_a16
      containers:
      - name: mongodb
        image: mongo:4.4
        ports:
        - containerPort: 27017
#        resources:
#          requests:
#            cpu: "250m"
#            memory: "256Mi"
#          limits:
#            memory: "1Gi"
        volumeMounts:
        - name: mongo-data
          mountPath: /data/db
      volumes:
      - name: mongo-data
        persistentVolumeClaim:
          claimName: mongodb-data
---
apiVersion: v1
kind: Service
metadata:
  name: mongodb
  namespace: librechat
spec:
  selector:
    app: mongodb
  ports:
  - port: 27017
    targetPort: 27017
---
# ── LibreChat ──────────────────────────────────────────────────────────────────
apiVersion: apps/v1
kind: Deployment
metadata:
  name: librechat
  namespace: librechat
spec:
  replicas: 1
  selector:
    matchLabels:
      app: librechat
  template:
    metadata:
      labels:
        app: librechat
    spec:
      nodeSelector:
        lab: lobot_a16
      containers:
      - name: librechat
        image: librechat/librechat:latest
        ports:
        - containerPort: 3080
        resources:
#          requests:
#            cpu: "500m"
#            memory: "512Mi"
#          limits:
#            memory: "2Gi"
#             nvidia.com/gpu: 1   # uncomment if a GPU is required
        env:
        - name: MONGO_URI
          value: "mongodb://mongodb.librechat.svc.cluster.local:27017/LibreChat"
        - name: JWT_SECRET
          value: "<generate: openssl rand -hex 32>"
        - name: JWT_REFRESH_SECRET
          value: "<generate: openssl rand -hex 32>"
        - name: ALLOW_REGISTRATION
          value: "true"          # set to "false" after creating your admin account
        - name: OPENAI_API_KEY
          value: "<your-openai-api-key>"   # enables GPT models in the model selector
---
apiVersion: v1
kind: Service
metadata:
  name: librechat
  namespace: librechat
spec:
  type: NodePort
  selector:
    app: librechat
  ports:
  - port: 3080
    targetPort: 3080
    nodePort: 32720
```

### Apply

```bash
kubectl create namespace librechat
kubectl apply -f librechat-deployment.yaml

# block direct NodePort access
sudo iptables -t raw -A PREROUTING -p tcp --dport 32720 ! -s 127.0.0.1 -j DROP

# do NOT add a rule for 8443 — that is the nginx listener port
```

### nginx (separate server block on port 8443)

```nginx
server {
    listen 8443 ssl;
    server_name lobot-dev.cs.queensu.ca;

    ssl_certificate /etc/letsencrypt/live/lobot-dev.cs.queensu.ca/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/lobot-dev.cs.queensu.ca/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        allow 130.15.1.0/24;
        allow 130.15.2.0/24;
        allow 130.15.3.0/24;
        allow 130.15.4.0/24;
        allow 130.15.5.0/24;
        allow 130.15.6.0/24;
        allow 130.15.7.0/24;
        deny all;

        proxy_pass http://127.0.0.1:32720/;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_redirect off;

        proxy_buffering off;
        proxy_read_timeout 300s;
    }
}
```

Accessible at `https://lobot-dev.cs.queensu.ca:8443`.

### Adding AI providers

LibreChat picks up provider API keys via environment variables. Add them to the
`env` section of the LibreChat deployment and re-apply.

| Provider  | Env var           |
|-----------|-------------------|
| OpenAI    | `OPENAI_API_KEY`  |
| Anthropic | `ANTHROPIC_API_KEY` |

> **Never paste API keys into chat or commit them to git.** Generate keys from
> the provider's dashboard, add them to the deployment yaml on the server, and
> keep that file out of version control.

After adding a key, restart the deployment:

```bash
kubectl rollout restart deployment/librechat -n librechat
```

### First-time setup

1. Register an account at `/register`
2. Once your admin account is created, set `ALLOW_REGISTRATION: "false"` in the
   deployment and re-apply to prevent others from self-registering
