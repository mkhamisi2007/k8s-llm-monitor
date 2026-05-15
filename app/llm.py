import json
import logging
import os
import re
import uuid
from typing import Any

import requests

from models import Issue, Severity

logger = logging.getLogger(__name__)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://ollama-service:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
MASTER_NODE_IP = os.getenv("MASTER_NODE_IP", "172.28.101.197")
NODE_COUNT = int(os.getenv("NODE_COUNT", "2"))


class LLMAnalyzer:
    def __init__(self):
        self.base_url = OLLAMA_BASE_URL
        self.model = OLLAMA_MODEL

    def is_available(self) -> bool:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return resp.status_code == 200
        except Exception:
            return False

    def analyze(self, cluster_data: dict) -> tuple[list[Issue], bool]:
        """Returns (issues, llm_was_used)."""
        if not self.is_available():
            logger.warning("Ollama unavailable, using rule-based fallback")
            return self._fallback_analysis(cluster_data), False

        prompt = self._build_prompt(cluster_data)
        if not prompt:
            return self._fallback_analysis(cluster_data), True
        try:
            response = requests.post(
                f"{self.base_url}/api/generate",
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.1,
                        "num_ctx": 4096,
                        "num_predict": 1024,
                    },
                },
                timeout=300,
            )
            response.raise_for_status()
            raw = response.json().get("response", "{}")
            issues = self._parse_response(raw)
            if issues is None:
                return self._fallback_analysis(cluster_data), False
            return issues, True
        except Exception as e:
            logger.error("LLM call failed: %s", e)
            return self._fallback_analysis(cluster_data), False

    def _build_prompt(self, data: dict) -> str:
        # Send only problematic pods with their diagnostics — nothing else
        pod_details = data.get("pod_details", [])
        if not pod_details:
            # No problematic pods — check nodes and degraded deployments only
            bad_nodes = [n for n in data["nodes"] if n["status"] != "Ready"]
            bad_deps = [d for d in data["deployments"] if d["desired"] > 0 and d["available"] < d["desired"]]
            if not bad_nodes and not bad_deps:
                return ""  # cluster healthy, skip LLM call entirely
            problems = {"bad_nodes": bad_nodes, "bad_deployments": bad_deps}
        else:
            problems = []
            for d in pod_details:
                logs_text = ""
                for label, text in d.get("logs", {}).items():
                    last_lines = "\n".join(text.splitlines()[-8:])
                    logs_text += f"[{label}]\n{last_lines}\n"
                events_text = "; ".join(
                    f"{e['reason']}: {e['message']}" for e in d.get("events", [])[:3]
                )
                spec = d.get("spec", {})
                images = [c.get("image", "") for c in spec.get("containers", [])]
                problems.append({
                    "pod": d["name"],
                    "namespace": d["namespace"],
                    "images": images,
                    "events": events_text,
                    "logs": logs_text.strip(),
                })

        return f"""Kubernetes SRE. Return JSON only.

PROBLEMS:
{json.dumps(problems, indent=2)}

Return ONLY:
{{"issues":[{{"severity":"error|warning|info","title":"...","description":"root cause from logs/events","solution":"kubectl commands","resource":"name or null","namespace":"ns or null"}}]}}"""

    def _parse_response(self, text: str) -> list[Issue] | None:
        try:
            # Ollama with format=json should return clean JSON, but try to extract if not
            json_match = re.search(r'\{.*\}', text, re.DOTALL)
            if not json_match:
                logger.error("No JSON found in LLM response")
                return None
            data = json.loads(json_match.group())
            issues = []
            for item in data.get("issues", []):
                try:
                    severity_raw = item.get("severity", "info").lower()
                    if severity_raw not in ("info", "warning", "error"):
                        severity_raw = "info"
                    issues.append(
                        Issue(
                            severity=Severity(severity_raw),
                            title=item.get("title", "Unknown issue"),
                            description=item.get("description", ""),
                            solution=item.get("solution", ""),
                            resource=item.get("resource") or None,
                            namespace=item.get("namespace") or None,
                        )
                    )
                except Exception as e:
                    logger.warning("Skipping malformed issue: %s", e)
            return issues
        except json.JSONDecodeError as e:
            logger.error("JSON parse error: %s | text: %s", e, text[:300])
            return None

    def _pod_root_cause(self, pod: dict, details: list) -> str:
        """Extract specific root cause from pod events and logs."""
        detail = next((d for d in details if d["name"] == pod["name"]), None)
        if not detail:
            return ""

        # Check events first — most informative
        for ev in detail.get("events", []):
            msg = (ev.get("message") or "").lower()
            reason = (ev.get("reason") or "").lower()
            if "failed to pull image" in msg or "errimagepull" in reason:
                return f"Image pull failed: {ev.get('message', '')}"
            if "oomkilled" in msg or "oomkilled" in reason:
                return f"Container killed due to out-of-memory (OOMKilled): {ev.get('message', '')}"
            if "insufficient" in msg:
                return f"Cannot schedule — insufficient resources: {ev.get('message', '')}"
            if "crashloopbackoff" in msg or "backoff" in reason:
                return f"Container keeps crashing: {ev.get('message', '')}"
            if "liveness" in msg or "readiness" in msg:
                return f"Probe failed: {ev.get('message', '')}"
            if ev.get("type") == "Warning" and ev.get("message"):
                return ev["message"]

        # Fall back to last log lines
        for label, text in detail.get("logs", {}).items():
            if text:
                last = "\n".join(text.splitlines()[-5:])
                return f"Last log lines ({label}):\n{last}"

        return ""

    def _fallback_analysis(self, data: dict) -> list[Issue]:
        """Rule-based analysis used when Ollama is unavailable."""
        issues: list[Issue] = []

        for node in data["nodes"]:
            if node["status"] != "Ready":
                issues.append(Issue(
                    severity=Severity.ERROR,
                    title=f"Node {node['name']} is NotReady",
                    description=(
                        f"Node {node['name']} is not in Ready state. "
                        f"Conditions: {node['conditions']}"
                    ),
                    solution=(
                        f"1. SSH to the node and check kubelet:\n"
                        f"   systemctl status kubelet\n"
                        f"   journalctl -u kubelet -n 50\n\n"
                        f"2. From master ({MASTER_NODE_IP}):\n"
                        f"   kubectl describe node {node['name']}\n\n"
                        f"3. If kubelet is stopped:\n"
                        f"   systemctl restart kubelet"
                    ),
                    resource=node["name"],
                ))

            for cond, status in node["conditions"].items():
                if cond in ("MemoryPressure", "DiskPressure", "PIDPressure") and status == "True":
                    issues.append(Issue(
                        severity=Severity.WARNING,
                        title=f"Node {node['name']} has {cond}",
                        description=f"Node {node['name']} is reporting {cond}.",
                        solution=(
                            f"1. Check node resources:\n"
                            f"   kubectl describe node {node['name']}\n\n"
                            f"2. If DiskPressure: free disk space on the node\n"
                            f"   df -h\n"
                            f"   docker system prune\n\n"
                            f"3. If MemoryPressure: check for memory-hungry pods:\n"
                            f"   kubectl top pods --all-namespaces --sort-by=memory"
                        ),
                        resource=node["name"],
                    ))

        pod_details = data.get("pod_details", [])

        for pod in data["pods"]:
            root_cause = self._pod_root_cause(pod, pod_details)

            if pod["phase"] == "Failed":
                issues.append(Issue(
                    severity=Severity.ERROR,
                    title=f"Pod {pod['name']} has Failed",
                    description=(
                        root_cause or
                        f"Pod {pod['name']} in namespace {pod['namespace']} is in Failed state."
                    ),
                    solution=(
                        f"1. Check logs:\n"
                        f"   kubectl logs {pod['name']} -n {pod['namespace']} --previous\n\n"
                        f"2. Describe pod:\n"
                        f"   kubectl describe pod {pod['name']} -n {pod['namespace']}\n\n"
                        f"3. Delete to allow controller to reschedule:\n"
                        f"   kubectl delete pod {pod['name']} -n {pod['namespace']}"
                    ),
                    resource=pod["name"],
                    namespace=pod["namespace"],
                ))
            elif pod["phase"] == "Pending":
                issues.append(Issue(
                    severity=Severity.WARNING,
                    title=f"Pod {pod['name']} is Pending",
                    description=(
                        root_cause or
                        f"Pod {pod['name']} in namespace {pod['namespace']} cannot be scheduled."
                    ),
                    solution=(
                        f"1. Check why pod is pending:\n"
                        f"   kubectl describe pod {pod['name']} -n {pod['namespace']}\n\n"
                        f"2. Check node resources:\n"
                        f"   kubectl describe nodes | grep -A5 Allocated\n\n"
                        f"3. Check for taints:\n"
                        f"   kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints"
                    ),
                    resource=pod["name"],
                    namespace=pod["namespace"],
                ))

            for cs in pod["container_statuses"]:
                if cs["restart_count"] > 10:
                    issues.append(Issue(
                        severity=Severity.ERROR,
                        title=f"CrashLoopBackOff: {pod['name']}/{cs['name']}",
                        description=(
                            root_cause or
                            f"Container {cs['name']} in pod {pod['name']} "
                            f"({pod['namespace']}) has restarted {cs['restart_count']} times."
                        ),
                        solution=(
                            f"1. Check container logs:\n"
                            f"   kubectl logs {pod['name']} -c {cs['name']} -n {pod['namespace']} --previous\n\n"
                            f"2. Describe pod for events:\n"
                            f"   kubectl describe pod {pod['name']} -n {pod['namespace']}\n\n"
                            f"3. Common causes: misconfigured env vars, missing ConfigMap/Secret, OOMKilled"
                        ),
                        resource=pod["name"],
                        namespace=pod["namespace"],
                    ))
                elif cs["restart_count"] > 5:
                    issues.append(Issue(
                        severity=Severity.WARNING,
                        title=f"High restarts: {pod['name']}/{cs['name']}",
                        description=(
                            root_cause or
                            f"Container {cs['name']} in pod {pod['name']} "
                            f"({pod['namespace']}) has restarted {cs['restart_count']} times."
                        ),
                        solution=(
                            f"1. Check logs:\n"
                            f"   kubectl logs {pod['name']} -c {cs['name']} -n {pod['namespace']}\n\n"
                            f"2. Check resource limits and liveness probes:\n"
                            f"   kubectl describe pod {pod['name']} -n {pod['namespace']}"
                        ),
                        resource=pod["name"],
                        namespace=pod["namespace"],
                    ))

        for dep in data["deployments"]:
            if dep["desired"] > 0 and dep["available"] < dep["desired"]:
                issues.append(Issue(
                    severity=Severity.WARNING,
                    title=f"Deployment {dep['name']} degraded",
                    description=(
                        f"Deployment {dep['name']} in {dep['namespace']}: "
                        f"{dep['available']}/{dep['desired']} replicas available."
                    ),
                    solution=(
                        f"1. Check rollout status:\n"
                        f"   kubectl rollout status deployment/{dep['name']} -n {dep['namespace']}\n\n"
                        f"2. Check pod events:\n"
                        f"   kubectl get pods -n {dep['namespace']} -l app={dep['name']}\n"
                        f"   kubectl describe pod <pod-name> -n {dep['namespace']}\n\n"
                        f"3. Check deployment events:\n"
                        f"   kubectl describe deployment {dep['name']} -n {dep['namespace']}"
                    ),
                    resource=dep["name"],
                    namespace=dep["namespace"],
                ))

        if len(data["nodes"]) < NODE_COUNT:
            issues.append(Issue(
                severity=Severity.ERROR,
                title=f"Missing nodes: {len(data['nodes'])}/{NODE_COUNT} found",
                description=f"Expected {NODE_COUNT} nodes but only {len(data['nodes'])} are registered.",
                solution=(
                    f"1. Check node status from master ({MASTER_NODE_IP}):\n"
                    f"   kubectl get nodes -o wide\n\n"
                    f"2. Check if kubelet is running on missing nodes:\n"
                    f"   systemctl status kubelet\n\n"
                    f"3. Check network connectivity between nodes:\n"
                    f"   ping {MASTER_NODE_IP}"
                ),
            ))

        if not issues:
            issues.append(Issue(
                severity=Severity.INFO,
                title="Cluster appears healthy",
                description="No critical issues detected by rule-based analysis. Enable Ollama for deeper AI-powered analysis.",
                solution=(
                    "No action needed. To enable LLM analysis:\n"
                    f"1. Ensure Ollama is running and accessible at {OLLAMA_BASE_URL}\n"
                    f"2. Pull the model: ollama pull {OLLAMA_MODEL}\n"
                    f"3. Verify: curl {OLLAMA_BASE_URL}/api/tags"
                ),
            ))

        return issues
