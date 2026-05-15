# K8s Cluster Monitor

A real-time Kubernetes cluster monitoring dashboard powered by a local LLM (Ollama). It continuously watches your cluster, collects logs and events from problematic pods, and uses AI to explain the root cause in plain language — not just generic kubectl commands.

![Dashboard](https://img.shields.io/badge/FastAPI-0.115-green) ![Kubernetes](https://img.shields.io/badge/Kubernetes-1.35-blue) ![Ollama](https://img.shields.io/badge/Ollama-llama3.2-orange) ![Docker](https://img.shields.io/badge/Docker-Hub-blue)

---

## Features

- **Live dashboard** — nodes, pods, deployments, and issues updated every 5 minutes
- **AI root-cause analysis** — sends pod logs, events, and container spec to Ollama to explain what is actually wrong (e.g. *"Image pull failed: nginx:wrongtag does not exist on Docker Hub"*)
- **Smart fallback** — when Ollama is offline or slow, rule-based analysis still reads pod events and logs to extract specific error messages
- **Issue detail modal** — click any issue to see the full root cause and step-by-step fix with exact `kubectl` commands
- **LLM status badge** — shows whether Ollama is active or offline in real time

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Namespace: cluster-monitor                     │
│                                                 │
│  ┌──────────────────────────────────────────┐   │
│  │  cluster-monitor (FastAPI, port 8080)    │   │
│  │  NodePort 30080                          │   │
│  └────────────┬─────────────────────────────┘   │
│               │ Kubernetes API (read-only)       │
└───────────────┼─────────────────────────────────┘
                │                       │
                ▼                       ▼
     Cluster resources         host.docker.internal:11434
  (nodes, pods, events)          Ollama (on your laptop)
```

**Data flow:**
```
monitoring loop
    → collect nodes / pods / deployments / events
    → for each failing pod: fetch logs + events + container spec
    → send compact diagnostic prompt to Ollama
    → parse JSON response into Issue list
    → store in memory deque (48 checks = 4 hours)
           ↓
    frontend polls /api/status every 15 seconds
```

---

## Prerequisites

| Tool | Notes |
|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | Enable Kubernetes in Settings → Kubernetes |
| [Ollama](https://ollama.com) | Install and pull a model (see below) |
| `kubectl` | Comes bundled with Docker Desktop |
| PowerShell | For the deploy/cleanup scripts |
| Docker Hub account | To host the image (`mkhamisi2007/cluster-monitor`) |

---

## Quick Start

### 1. Install Ollama and pull a model

Download Ollama from [ollama.com](https://ollama.com), then:

```powershell
ollama pull llama3.2:1b
```

Make Ollama listen on all interfaces so the cluster pod can reach it:

```powershell
# Set permanently
[System.Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0:11434", "User")
```

Restart Ollama from the system tray after setting this variable.

### 2. Enable Kubernetes in Docker Desktop

**Settings → Kubernetes → Enable Kubernetes → Apply & Restart**

### 3. Clone and deploy

```powershell
git clone https://github.com/mkhamisi2007/k8s-llm-monitor.git
cd k8s-cluster-monitor

.\deploy.ps1
```

The script builds the Docker image, pushes it to Docker Hub, applies all Kubernetes manifests, and waits for the pod to be ready.

### 4. Open the dashboard

```
http://localhost:30080
```

---

## Configuration

All settings are in `k8s/configmap.yaml`:

| Variable | Default | Notes |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama address reachable from inside the cluster |
| `OLLAMA_MODEL` | `llama3.2:1b` | Must match a pulled model — check with `ollama list` |
| `MASTER_NODE_IP` | `localhost` | Embedded in kubectl commands shown in the UI |
| `NODE_COUNT` | `1` | Expected node count — alerts if fewer are found |
| `CHECK_INTERVAL_SECONDS` | `300` | How often the monitor runs |
| `HISTORY_SIZE` | `48` | Checks kept in memory (48 × 5 min = 4 hours) |

Apply changes after editing:

```powershell
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/cluster-monitor -n cluster-monitor
```

---

## Scripts

| Script | Description |
|---|---|
| `.\deploy.ps1` | Build image, push to Docker Hub, deploy everything |
| `.\deploy.ps1 -SkipBuild` | Deploy without rebuilding (image already on Hub) |
| `.\cleanup.ps1` | Delete all cluster resources + ClusterRole + local Docker image |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web dashboard |
| `GET` | `/api/status` | Latest cluster status as JSON |
| `GET` | `/api/history` | All stored check results |
| `POST` | `/api/check` | Trigger an immediate check |
| `GET` | `/api/health` | Liveness probe endpoint |

Trigger a manual check without waiting 5 minutes:

```powershell
curl -X POST http://localhost:30080/api/check
```

---

## What Is Monitored

| Category | Details |
|---|---|
| **Nodes** | Ready / NotReady, MemoryPressure, DiskPressure, PIDPressure |
| **Pods** | Failed, Pending, CrashLoopBackOff, OOMKilled, high restart counts |
| **Deployments** | Desired vs. available replica counts |
| **Events** | Warning events from all namespaces, sorted by frequency |
| **Node count** | Alerts if fewer nodes than `NODE_COUNT` are registered |

---

## Project Structure

```
├── app/
│   ├── main.py           FastAPI app + async monitoring loop
│   ├── monitor.py        Kubernetes API data collection + pod diagnostics
│   ├── llm.py            Ollama integration + smart fallback analysis
│   ├── models.py         Pydantic models (Issue, ClusterStatus, NodeInfo)
│   ├── requirements.txt
│   └── static/
│       └── index.html    Frontend dashboard (dark theme, no framework)
├── k8s/
│   ├── namespace.yaml
│   ├── rbac.yaml         ClusterRole with read-only access to all resources
│   ├── configmap.yaml    Runtime configuration
│   ├── deployment.yaml   cluster-monitor pod spec
│   └── service.yaml      NodePort 30080
├── Dockerfile
├── deploy.ps1            Windows deploy script
└── cleanup.ps1           Windows full cleanup script
```

---

## Troubleshooting

**LLM badge shows "offline"**

Verify Ollama is reachable from inside the cluster:
```powershell
kubectl exec -n cluster-monitor deploy/cluster-monitor -- curl http://host.docker.internal:11434/api/tags
```
If this fails, make sure `OLLAMA_HOST=0.0.0.0:11434` is set and Ollama has been restarted.

**LLM call times out**

The model may be too slow on CPU. Either use a smaller model or increase the timeout in `app/llm.py`. The fallback still provides specific root causes from pod events.

**Pod stuck in `ErrImageNeverPull`**

The image was not found locally. Run `.\deploy.ps1` to build and push to Docker Hub, then re-apply the deployment.

**Stream app logs**
```powershell
kubectl logs -n cluster-monitor deploy/cluster-monitor -f
```

---

## photo
<img width="1132" height="654" alt="image" src="https://github.com/user-attachments/assets/40436f78-e6f4-49ed-9363-47efaab6a8e6" />



