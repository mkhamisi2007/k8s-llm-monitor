# deploy.ps1 — Build and deploy the cluster monitor to Kubernetes (Docker Desktop)
# Ollama runs on the host at localhost:11434 — no in-cluster deployment needed.
# Usage: .\deploy.ps1 [-SkipBuild]

param(
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"

$NAMESPACE  = "cluster-monitor"
$IMAGE_NAME = "mkhamisi2007/cluster-monitor"
$IMAGE_TAG  = "latest"

Write-Host "==> Creating namespace..."
kubectl apply -f k8s/namespace.yaml

Write-Host "==> Applying RBAC..."
kubectl apply -f k8s/rbac.yaml

Write-Host "==> Applying ConfigMap..."
kubectl apply -f k8s/configmap.yaml

if (-not $SkipBuild) {
    Write-Host "==> Building Docker image..."
    docker build -t "${IMAGE_NAME}:${IMAGE_TAG}" .
    Write-Host "==> Pushing image to Docker Hub..."
    docker push "${IMAGE_NAME}:${IMAGE_TAG}"
    Write-Host "    Image pushed to Docker Hub."
} else {
    Write-Host "==> Skipping build (-SkipBuild)"
}

Write-Host "==> Deploying cluster-monitor..."
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml

Write-Host ""
Write-Host "==> Waiting for cluster-monitor pod to be ready..."
kubectl rollout status deployment/cluster-monitor -n $NAMESPACE --timeout=120s

Write-Host ""
Write-Host "================================================================"
Write-Host "  Deployment complete!"
Write-Host "================================================================"
Write-Host ""
Write-Host "  Web UI:  http://localhost:30080"
Write-Host ""
Write-Host "  Monitor logs:"
Write-Host "    kubectl logs -n $NAMESPACE deploy/cluster-monitor -f"
Write-Host ""
Write-Host "  Trigger a manual check:"
Write-Host "    curl -X POST http://localhost:30080/api/check"
Write-Host ""
