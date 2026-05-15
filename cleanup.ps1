# cleanup.ps1 — Remove all cluster resources and local Docker image before redeploying
# Usage: .\cleanup.ps1

$ErrorActionPreference = "Stop"

$NAMESPACE  = "cluster-monitor"
$IMAGE_NAME = "mkhamisi2007/cluster-monitor"
$IMAGE_TAG  = "latest"

# Delete cluster-scoped resources (not removed by namespace delete)
Write-Host "==> Deleting ClusterRole and ClusterRoleBinding..."
kubectl delete clusterrolebinding cluster-monitor-reader --ignore-not-found
kubectl delete clusterrole cluster-monitor-reader --ignore-not-found

# Delete the entire namespace — cascades to all namespaced resources:
# deployments, services, pods, configmaps, serviceaccounts, roles, rolebindings
Write-Host "==> Deleting namespace '$NAMESPACE' (this deletes everything inside it)..."
kubectl delete namespace $NAMESPACE --ignore-not-found

Write-Host "==> Waiting for namespace to be fully removed..."
$timeout = 60
$elapsed = 0
while ($elapsed -lt $timeout) {
    $exists = kubectl get namespace $NAMESPACE --ignore-not-found 2>$null
    if (-not $exists) { break }
    Start-Sleep -Seconds 3
    $elapsed += 3
    Write-Host "    Still terminating... ($elapsed s)"
}
if ($elapsed -ge $timeout) {
    Write-Host "    WARNING: namespace did not terminate within $timeout seconds."
}

# Remove local Docker image
Write-Host "==> Removing local Docker image..."
$imageExists = docker images -q "${IMAGE_NAME}:${IMAGE_TAG}"
if ($imageExists) {
    docker rmi "${IMAGE_NAME}:${IMAGE_TAG}" --force
    Write-Host "    Image removed."
} else {
    Write-Host "    Image not found locally, skipping."
}

Write-Host ""
Write-Host "================================================================"
Write-Host "  Cleanup complete. Run .\deploy.ps1 to redeploy."
Write-Host "================================================================"
Write-Host ""
