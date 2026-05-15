# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Build container image
docker build -t cluster-monitor:latest .

# Run locally (outside cluster, needs kubeconfig)
cd app && pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8080 --reload

# Deploy to cluster (run on master node)
chmod +x deploy.sh && ./deploy.sh

# Skip Ollama deployment (use external Ollama)
./deploy.sh --skip-ollama

# Skip Docker build (image already on nodes)
./deploy.sh --skip-build

# Apply config changes and restart
kubectl apply -f k8s/configmap.yaml
kubectl rollout restart deployment/cluster-monitor -n cluster-monitor

# Trigger an immediate check (bypasses the 5-minute timer)
curl -X POST http://172.28.101.197:30080/api/check

# Stream monitor logs
kubectl logs -n cluster-monitor deploy/cluster-monitor -f

# Stream Ollama logs (watch model download progress)
kubectl logs -n cluster-monitor deploy/ollama -f
```

## Architecture

The app has three layers that work together:

**1. Kubernetes data collection (`app/monitor.py`)**  
`ClusterMonitor._collect_data()` calls the Kubernetes API (nodes, pods, deployments, warning events) synchronously and wraps it in `run_in_executor` so it doesn't block the async event loop. The raw data dict is structured for direct use in the LLM prompt.

**2. LLM analysis (`app/llm.py`)**  
`LLMAnalyzer.analyze()` sends the cluster data to Ollama's `/api/generate` endpoint with `format: "json"` forced, which instructs Ollama to return strict JSON. The prompt includes the master node IP and node count so solutions in the response contain cluster-specific `kubectl` commands. If Ollama is unreachable or returns invalid JSON, `_fallback_analysis()` runs rule-based checks (node Ready condition, pod phase, container restart counts, deployment replicas) and returns `Issue` objects with the same shape.

**3. FastAPI app + web UI (`app/main.py`, `app/static/index.html`)**  
The monitoring loop runs as an `asyncio` background task started via the FastAPI `lifespan` context manager. Results are kept in a `collections.deque` (in-memory, configurable size). The frontend polls `/api/status` every 15 seconds to detect when a check is in progress; the 5-minute progress bar is purely cosmetic (CSS `transition`). Clicking an issue row opens a modal populated from `window._sortedIssues[idx]` — issues are sorted by severity before rendering.

**Data flow:**
```
lifespan task → monitor.check() → _collect_data() → llm.analyze()
     → ClusterStatus (Pydantic) → stored in history deque
                                        ↓
GET /api/status ← frontend polls ← JSON response
```

## Key design decisions

- **No database.** History is in-memory only; the deque is lost on pod restart. `HISTORY_SIZE` defaults to 48 (4 hours at 5-min intervals).
- **Fallback always runs.** When Ollama is down, `ClusterStatus.llm_available` is `False` and the UI shows "LLM offline" badge — but issues still populate from rule-based analysis. There is never an empty issue list unless the cluster is genuinely healthy.
- **`imagePullPolicy: Never`** in `k8s/deployment.yaml` — the image must be built and loaded onto cluster nodes manually. For a registry-based workflow, change this and update the `image:` field.
- **Ollama hostPath volume** (`ollama-deployment.yaml`) persists the downloaded model at `/data/ollama` on the node. Without it, the ~2 GB `llama3.2:3b` re-downloads on every restart. Because hostPath is node-local, the pod must land on the same node each time — pin it with a `nodeSelector` if the affinity preference is not strong enough.
- **Ollama pod affinity** prefers the worker node over the control-plane to avoid memory contention with the Kubernetes control plane components.

## Configuration

All runtime config lives in `k8s/configmap.yaml` and is injected via `envFrom`. The relevant env vars are read at module import time in `llm.py` and `monitor.py` — changing them requires a pod restart.

| Var | Default | Notes |
|---|---|---|
| `OLLAMA_MODEL` | `llama3.2:3b` | Must also be changed in `ollama-deployment.yaml` args |
| `OLLAMA_BASE_URL` | `http://ollama-service.cluster-monitor.svc.cluster.local:11434` | Override for external Ollama |
| `MASTER_NODE_IP` | `172.28.101.197` | Embedded in LLM prompt and solution text |
| `NODE_COUNT` | `2` | Triggers an error issue if fewer nodes are found |
| `CHECK_INTERVAL_SECONDS` | `300` | Also update the JS `CHECK_INTERVAL` constant in `index.html` if changed |
