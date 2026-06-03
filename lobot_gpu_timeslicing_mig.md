# Lobot GPU Sharing: Time-Slicing & MIG Configuration

**Cluster:** Lobot (lobot.cs.queensu.ca)
**Last updated:** 2026-06-03

---

## Overview

Lobot uses two GPU sharing strategies depending on the hardware:

| Strategy | Hardware | Nodes | Allocatable per node |
|---|---|---|---|
| Time-slicing | NVIDIA A16 (16GB × 4 per card) | `floppy` | 96 (`nvidia.com/gpu`) |
| MIG (`all-1g.24gb`) | RTX PRO 6000 Blackwell 96GB | `duotronic` | 8 (`nvidia.com/gpu`) |
| MIG (`all-2g.48gb`) | RTX PRO 6000 Blackwell 96GB | `metroidblackwelltest` | 12 (`nvidia.com/gpu`) |

Both strategies are managed by the **NVIDIA GPU Operator** (`gpu-operator` namespace). Time-slicing is configured via a device plugin ConfigMap; MIG is configured via the MIG manager ConfigMap and node labels.

---

## ConfigMaps

### Time-Slicing: `time-slicing-config-fine`

**Namespace:** `gpu-operator`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: time-slicing-config-fine
  namespace: gpu-operator
data:
  a16: |-
    version: v1
    flags:
      migStrategy: none
    sharing:
      timeSlicing:
        resources:
        - name: nvidia.com/gpu
          replicas: 4
```

Each key in `data` is a named profile. The node label `nvidia.com/device-plugin.config=<key>` selects which profile applies to that node. `replicas: 4` on an A16 node with 24 physical GPUs yields 96 allocatable slices.

To add a new time-slicing profile, add a new key to this ConfigMap and label the target node accordingly.

### MIG: `mig-config`

**Namespace:** `gpu-operator`

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: mig-config
  namespace: gpu-operator
data:
  config.yaml: |-
    version: v1
    mig-configs:
      all-1g.24gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "1g.24gb": 4
      all-2g.48gb:
        - devices: all
          mig-enabled: true
          mig-devices:
            "2g.48gb": 2
      all-disabled:
        - devices: all
          mig-enabled: false
          mig-devices: {}
```

This is the **custom** MIG config for Blackwell nodes. It lives alongside `default-mig-parted-config` (the operator's built-in config covering A100/H100/B200/etc.), but Blackwell nodes use `mig-config` via the cluster policy.

The MIG manager selects a profile via the node label `nvidia.com/mig.config=<profile-name>`.

---

## Available MIG Profiles (RTX PRO 6000 Blackwell 96GB)

| Profile | Slices per GPU | Memory per slice | Notes |
|---|---|---|---|
| `1g.24gb` | 4 | 23.62 GiB | Matches `all-1g.24gb` — used on `duotronic` |
| `1g.24gb+gfx` | 4 | 23.62 GiB | With graphics engine |
| `1g.24gb+me` | 1 | 23.62 GiB | With media engine (only 1 per GPU) |
| `2g.48gb` | 2 | 47.38 GiB | Used on `metroidblackwelltest` |
| `2g.48gb+gfx` | 2 | 47.38 GiB | With graphics engine |
| `4g.96gb` | 1 | 95.00 GiB | Full GPU, MIG mode enabled |

---

## Setting Up a New Node from Scratch

### Time-Slicing Node (e.g. A16)

```bash
# 1. Add the profile to the ConfigMap if it doesn't exist
kubectl edit configmap time-slicing-config-fine -n gpu-operator

# 2. Label the node
kubectl label node <nodename> nvidia.com/device-plugin.config=a16

# 3. Restart device plugin to pick up changes
kubectl rollout restart -n gpu-operator daemonset/nvidia-device-plugin-daemonset

# 4. Verify
kubectl get node <nodename> -o json | jq '.status.allocatable | with_entries(select(.key | startswith("nvidia")))'
```

### MIG Node (Blackwell)

```bash
# 1. Ensure the desired profile exists in mig-config ConfigMap
kubectl get configmap mig-config -n gpu-operator -o yaml

# 2. Check available hardware profiles on the node
kubectl exec -n gpu-operator -it \
  $(kubectl get pod -n gpu-operator -l app=nvidia-mig-manager \
    -o jsonpath='{.items[?(@.spec.nodeName=="<nodename>")].metadata.name}') \
  -- nvidia-smi mig -lgip

# 3. Apply the MIG profile label
kubectl label node <nodename> nvidia.com/mig.config=all-2g.48gb

# 4. Watch for success
kubectl get node <nodename> -w \
  -o jsonpath='{.metadata.labels.nvidia\.com/mig\.config\.state}{"\n"}'

# 5. Verify allocatable resources
kubectl get node <nodename> -o json | \
  jq '.status.allocatable | with_entries(select(.key | startswith("nvidia")))'
```

Expected allocatable for `all-2g.48gb` on a 6-GPU node: `nvidia.com/gpu: 12`

---

## Checking Current State

```bash
# See which config profile each node is using
kubectl get nodes -o custom-columns=\
'NAME:.metadata.name,\
MIG-CONFIG:.metadata.labels.nvidia\.com/mig\.config,\
MIG-STATE:.metadata.labels.nvidia\.com/mig\.config\.state,\
PLUGIN-CONFIG:.metadata.labels.nvidia\.com/device-plugin\.config'

# See allocatable GPUs across all nodes
kubectl get nodes -o custom-columns=\
'NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu'

# Verify MIG instances on a node directly
ssh <nodename> "sudo nvidia-smi mig -lgi"
```

---

## Gotchas

### 1. Legacy `kube-system` device plugin daemonset

A legacy `nvidia-device-plugin-daemonset` in `kube-system` (620+ days old) predated the GPU Operator. It ran on all nodes and held GPU device handles open, blocking MIG reconfiguration with `ERROR_IN_USE`. It has been deleted. **Do not recreate it** — the GPU Operator's device plugin in `gpu-operator` namespace handles everything.

To verify it's gone:
```bash
kubectl get daemonset -n kube-system | grep nvidia
```

### 2. LightDM/Xorg on compute nodes

If a Blackwell node was installed from a desktop Ubuntu image, LightDM starts on boot and grabs all GPUs via DRM (`/dev/dri/*`), blocking MIG configuration. This manifests as `ERROR_IN_USE` with no visible process in `nvidia-smi`, `fuser`, or `lsof`.

**Diagnosis:**
```bash
sudo fuser /dev/dri/*
# If PID appears, check it:
cat /proc/<PID>/cmdline | tr '\0' ' '
```

**Fix:**
```bash
sudo systemctl stop lightdm
sudo systemctl disable lightdm
```

**Prevention:** Always provision GPU compute nodes from **Ubuntu Server** images, not desktop images. Add `sudo systemctl disable lightdm` to the node provisioning checklist.

### 3. MIG mode requires no active GPU clients to reconfigure

The MIG manager must drain all GPU clients before applying a new config. It handles this automatically for Kubernetes pods, but will fail if anything outside Kubernetes holds a device handle open. Clients that can block MIG reconfiguration:

- `nvidia-persistenced` (systemd service)
- `containerd` shim processes (if pods were recently deleted but shims haven't exited)
- Xorg / LightDM (see above)
- Stale kernel references from `nvidia_uvm` / `nvidia_drm`

If the MIG config state is stuck at `failed`, check the mig-manager logs:
```bash
kubectl logs -n gpu-operator <mig-manager-pod> | tail -50
```

### 4. MIG config state label is not self-healing

If the mig-manager sets `nvidia.com/mig.config.state=failed`, it will not retry automatically. You must bounce the label to trigger a fresh attempt:
```bash
kubectl label node <nodename> nvidia.com/mig.config=all-disabled --overwrite
kubectl label node <nodename> nvidia.com/mig.config=all-2g.48gb --overwrite
```

### 5. `unable to get device name: failed to find device with id '2bb5'`

This warning appears in mig-manager logs for RTX PRO 6000 Blackwell nodes. The PCI device ID `0x2BB5` is not in the mig-manager's internal name database (new GPU). It is **harmless** and does not affect functionality.

### 6. MIG mode enablement may require a reboot

Enabling MIG mode for the first time on a GPU (`mig-enabled: true`) can require a node reboot to fully take effect at the kernel level. If MIG instances cannot be created immediately after enabling MIG mode, reboot the node and try again.

### 7. Time-slicing and MIG are mutually exclusive per node

A node cannot use both strategies simultaneously. Time-slicing nodes should not have `nvidia.com/mig.config` labels set; MIG nodes should not have `nvidia.com/device-plugin.config` labels pointing at a time-slicing profile.

---

## Cluster Policy Reference

The GPU Operator cluster policy is named `cluster-policy`. To see the current device plugin config name:
```bash
kubectl get clusterpolicies.nvidia.com/cluster-policy -n gpu-operator \
  -o jsonpath='{.spec.devicePlugin.config.name}'
```

To update which ConfigMap the device plugin uses cluster-wide:
```bash
kubectl patch clusterpolicies.nvidia.com/cluster-policy \
  -n gpu-operator --type merge \
  -p '{"spec": {"devicePlugin": {"config": {"name": "time-slicing-config-fine"}}}}'
```

---

## Quick Reference: Node Labels

| Label | Purpose | Example value |
|---|---|---|
| `nvidia.com/device-plugin.config` | Selects time-slicing profile | `a16` |
| `nvidia.com/mig.config` | Selects MIG profile | `all-2g.48gb` |
| `nvidia.com/mig.config.state` | MIG apply status (set by operator) | `success` / `failed` |
| `nvidia.com/mig.strategy` | MIG exposure strategy | `single` |
| `nvidia.com/gpu.product` | GPU model string | `NVIDIA-RTX-PRO-6000-Blackwell-Server-Edition-MIG-2g.48gb` |
