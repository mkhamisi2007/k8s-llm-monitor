#!/usr/bin/env bash
# deploy.sh — Build and deploy the cluster monitor to Kubernetes (Docker Desktop)
# Ollama runs on the host at localhost:11434 — no in-cluster deployment needed.
# Usage: ./deploy.sh [--skip-build]

set -euo pipefail

NAMESPACE="cluster-monitor"
IMAGE_NAME="cluster-monitor"
IMAGE_TAG="latest"
SKIP_BUILD=false

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
  esac
done

echo "==> Creating namespace..."
kubectl apply -f k8s/namespace.yaml

echo "==> Applying RBAC..."
kubectl apply -f k8s/rbac.yaml

echo "==> Applying ConfigMap..."
kubectl apply -f k8s/configmap.yaml

if [ "$SKIP_BUILD" = false ]; then
  echo "==> Building Docker image..."
  docker build -t ${IMAGE_NAME}:${IMAGE_TAG} .
  echo "    Image built and available to Docker Desktop Kubernetes."
else
  echo "==> Skipping build (--skip-build)"
fi

echo "==> Deploying cluster-monitor..."
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

echo ""
echo "==> Waiting for cluster-monitor pod to be ready..."
kubectl rollout status deployment/cluster-monitor -n $NAMESPACE --timeout=120s

echo ""
echo "================================================================"
echo "  Deployment complete!"
echo "================================================================"
echo ""
echo "  Web UI:  http://localhost:30080"
echo ""
echo "  Monitor logs:"
echo "    kubectl logs -n $NAMESPACE deploy/cluster-monitor -f"
echo ""
echo "  Trigger a manual check:"
echo "    curl -X POST http://localhost:30080/api/check"
echo ""
