import asyncio
import logging
import os
from datetime import datetime

from kubernetes import client, config
from kubernetes.client.rest import ApiException

from llm import LLMAnalyzer
from models import ClusterStatus, NodeInfo

logger = logging.getLogger(__name__)

NODE_COUNT = int(os.getenv("NODE_COUNT", "2"))


class ClusterMonitor:
    def __init__(self):
        self.llm = LLMAnalyzer()
        self._load_k8s_config()

    def _load_k8s_config(self):
        try:
            config.load_incluster_config()
            logger.info("Using in-cluster Kubernetes config")
        except config.ConfigException:
            config.load_kube_config()
            logger.info("Using local kubeconfig")

        self.v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    async def check(self) -> ClusterStatus:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._check_sync)

    def _check_sync(self) -> ClusterStatus:
        try:
            raw = self._collect_data()
        except Exception as e:
            logger.error("Failed to collect cluster data: %s", e)
            return ClusterStatus(error=str(e))

        issues, llm_used = self.llm.analyze(raw)

        nodes = [
            NodeInfo(
                name=n["name"],
                status=n["status"],
                roles=n["roles"],
                conditions=n["conditions"],
                allocatable=n["allocatable"],
                capacity=n["capacity"],
            )
            for n in raw["nodes"]
        ]

        healthy_deps = sum(
            1 for d in raw["deployments"]
            if d["desired"] == 0 or d["available"] >= d["desired"]
        )

        return ClusterStatus(
            timestamp=datetime.now(),
            nodes=nodes,
            issues=issues,
            total_pods=raw["pod_counts"]["total"],
            running_pods=raw["pod_counts"]["running"],
            failed_pods=raw["pod_counts"]["failed"],
            pending_pods=raw["pod_counts"]["pending"],
            total_deployments=len(raw["deployments"]),
            healthy_deployments=healthy_deps,
            llm_available=llm_used,
        )

    def _collect_data(self) -> dict:
        nodes = self._get_nodes()
        pods = self._get_pods()
        deployments = self._get_deployments()
        events = self._get_warning_events()
        pod_details = self._get_problematic_pod_details(pods)

        pod_counts = {
            "total": len(pods),
            "running": sum(1 for p in pods if p["phase"] == "Running"),
            "failed": sum(1 for p in pods if p["phase"] == "Failed"),
            "pending": sum(1 for p in pods if p["phase"] == "Pending"),
            "succeeded": sum(1 for p in pods if p["phase"] == "Succeeded"),
        }

        return {
            "nodes": nodes,
            "pods": pods,
            "deployments": deployments,
            "events": events,
            "pod_counts": pod_counts,
            "pod_details": pod_details,
        }

    def _get_nodes(self) -> list:
        nodes = []
        for node in self.v1.list_node().items:
            conditions = {
                c.type: c.status
                for c in (node.status.conditions or [])
            }
            labels = node.metadata.labels or {}
            roles = [
                k.split("/")[-1]
                for k in labels
                if k.startswith("node-role.kubernetes.io/")
            ]
            if not roles:
                roles = ["worker"]

            status = "Ready" if conditions.get("Ready") == "True" else "NotReady"

            allocatable = {k: str(v) for k, v in (node.status.allocatable or {}).items()}
            capacity = {k: str(v) for k, v in (node.status.capacity or {}).items()}

            nodes.append({
                "name": node.metadata.name,
                "status": status,
                "roles": roles,
                "conditions": conditions,
                "allocatable": allocatable,
                "capacity": capacity,
            })
        return nodes

    def _get_pods(self) -> list:
        pods = []
        for pod in self.v1.list_pod_for_all_namespaces().items:
            container_statuses = []
            for cs in pod.status.container_statuses or []:
                state_info = {}
                if cs.state:
                    if cs.state.waiting:
                        state_info = {"state": "waiting", "reason": cs.state.waiting.reason}
                    elif cs.state.running:
                        state_info = {"state": "running"}
                    elif cs.state.terminated:
                        state_info = {
                            "state": "terminated",
                            "reason": cs.state.terminated.reason,
                            "exit_code": cs.state.terminated.exit_code,
                        }
                container_statuses.append({
                    "name": cs.name,
                    "ready": cs.ready,
                    "restart_count": cs.restart_count,
                    "status": state_info,
                })

            pods.append({
                "name": pod.metadata.name,
                "namespace": pod.metadata.namespace,
                "phase": pod.status.phase or "Unknown",
                "node": pod.spec.node_name,
                "container_statuses": container_statuses,
            })
        return pods

    def _get_deployments(self) -> list:
        deployments = []
        for dep in self.apps_v1.list_deployment_for_all_namespaces().items:
            deployments.append({
                "name": dep.metadata.name,
                "namespace": dep.metadata.namespace,
                "desired": dep.spec.replicas or 0,
                "available": dep.status.available_replicas or 0,
                "ready": dep.status.ready_replicas or 0,
            })
        return deployments

    def _get_problematic_pod_details(self, pods: list) -> list:
        details = []
        for pod in pods:
            is_crashloop = any(
                cs["status"].get("reason") in ("CrashLoopBackOff", "Error", "OOMKilled")
                or cs["restart_count"] > 3
                for cs in pod["container_statuses"]
            )
            if pod["phase"] in ("Running", "Succeeded") and not is_crashloop:
                continue

            name = pod["name"]
            namespace = pod["namespace"]
            detail = {"name": name, "namespace": namespace, "logs": {}, "events": [], "spec": {}}

            # Collect logs — try previous (crashed) first, then current
            for cs in pod["container_statuses"]:
                for previous in (True, False):
                    try:
                        logs = self.v1.read_namespaced_pod_log(
                            name=name,
                            namespace=namespace,
                            container=cs["name"],
                            previous=previous,
                            tail_lines=15,
                        )
                        label = f"{cs['name']} (previous)" if previous else cs["name"]
                        detail["logs"][label] = logs.strip()
                        break
                    except ApiException:
                        continue

            # Pod-scoped events
            try:
                ev_list = self.v1.list_namespaced_event(
                    namespace=namespace,
                    field_selector=f"involvedObject.name={name}",
                )
                detail["events"] = [
                    {"type": e.type, "reason": e.reason, "message": e.message, "count": e.count}
                    for e in ev_list.items
                ]
            except ApiException:
                pass

            # Container spec (image, resources, probes, env)
            try:
                pod_obj = self.v1.read_namespaced_pod(name=name, namespace=namespace)
                detail["spec"] = {
                    "containers": [
                        {
                            "name": c.name,
                            "image": c.image,
                            "command": c.command,
                            "args": c.args,
                            "env": [
                                {"name": e.name, "value": e.value}
                                for e in (c.env or []) if e.value
                            ],
                            "resources": {
                                "requests": {k: str(v) for k, v in (c.resources.requests or {}).items()} if c.resources else {},
                                "limits": {k: str(v) for k, v in (c.resources.limits or {}).items()} if c.resources else {},
                            },
                            "liveness_probe": str(c.liveness_probe) if c.liveness_probe else None,
                            "readiness_probe": str(c.readiness_probe) if c.readiness_probe else None,
                        }
                        for c in (pod_obj.spec.containers or [])
                    ],
                    "conditions": [
                        {"type": c.type, "status": c.status, "reason": c.reason, "message": c.message}
                        for c in (pod_obj.status.conditions or [])
                    ],
                }
            except ApiException:
                pass

            details.append(detail)
        return details

    def _get_warning_events(self) -> list:
        events = []
        try:
            for ev in self.v1.list_event_for_all_namespaces().items:
                if ev.type == "Warning":
                    events.append({
                        "reason": ev.reason,
                        "message": ev.message,
                        "namespace": ev.metadata.namespace,
                        "object": f"{ev.involved_object.kind}/{ev.involved_object.name}",
                        "count": ev.count or 1,
                        "last_seen": str(ev.last_timestamp) if ev.last_timestamp else None,
                    })
        except ApiException as e:
            logger.warning("Could not fetch events: %s", e)

        events.sort(key=lambda e: e["count"] or 0, reverse=True)
        return events[:50]
