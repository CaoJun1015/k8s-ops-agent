"""Narrow Kubernetes adapter used by the approved action catalog."""

from __future__ import annotations

import time


ABNORMAL_WAITING_REASONS = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
    "RunContainerError",
}


class KubernetesAdapter:
    """Lazy-load Kubernetes credentials and expose only approved operations."""

    def __init__(self):
        self._core_api = None
        self._apps_api = None

    def _load(self) -> None:
        if self._core_api is not None:
            return
        from kubernetes import client, config

        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self._core_api = client.CoreV1Api()
        self._apps_api = client.AppsV1Api()

    def inspect_pod(self, *, namespace: str, pod_name: str) -> dict:
        from kubernetes.client.exceptions import ApiException

        self._load()
        try:
            pod = self._core_api.read_namespaced_pod(pod_name, namespace)
        except ApiException as error:
            if error.status == 404:
                return {"exists": False}
            raise

        owner_kind = None
        workload_kind = None
        workload_name = None
        owner_references = pod.metadata.owner_references or []
        if owner_references:
            owner = owner_references[0]
            owner_kind = owner.kind
            if owner.kind == "StatefulSet":
                workload_kind = "StatefulSet"
                workload_name = owner.name
            elif owner.kind == "ReplicaSet":
                replica_set = self._apps_api.read_namespaced_replica_set(
                    owner.name, namespace
                )
                for parent in replica_set.metadata.owner_references or []:
                    if parent.kind == "Deployment":
                        workload_kind = "Deployment"
                        workload_name = parent.name
                        break

        waiting_reasons = {
            state.waiting.reason
            for container in pod.status.container_statuses or []
            if (state := container.state) and state.waiting and state.waiting.reason
        }
        ready = any(
            condition.type == "Ready" and condition.status == "True"
            for condition in pod.status.conditions or []
        )
        abnormal_reason = next(
            (
                reason
                for reason in waiting_reasons
                if reason in ABNORMAL_WAITING_REASONS
            ),
            None,
        )
        is_abnormal = (
            pod.status.phase not in {"Running", "Succeeded"}
            or not ready
            or abnormal_reason is not None
        )
        return {
            "exists": True,
            "uid": pod.metadata.uid,
            "owner_kind": owner_kind,
            "workload_kind": workload_kind,
            "workload_name": workload_name,
            "phase": pod.status.phase,
            "ready": ready,
            "is_abnormal": is_abnormal,
            "reason": abnormal_reason or pod.status.reason,
        }

    def delete_pod(
        self, *, namespace: str, pod_name: str, expected_uid: str
    ) -> dict:
        from kubernetes import client

        self._load()
        options = client.V1DeleteOptions(
            grace_period_seconds=30,
            preconditions=client.V1Preconditions(uid=expected_uid),
            propagation_policy="Background",
        )
        self._core_api.delete_namespaced_pod(
            pod_name,
            namespace,
            body=options,
        )
        return {"deleted": True, "uid": expected_uid}

    def verify_recovery(self, target: dict) -> dict:
        self._load()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if target.get("workload_kind") == "Deployment":
                workload = self._apps_api.read_namespaced_deployment_status(
                    target["workload_name"], target["namespace"]
                )
            elif target.get("workload_kind") == "StatefulSet":
                workload = self._apps_api.read_namespaced_stateful_set_status(
                    target["workload_name"], target["namespace"]
                )
            else:
                return {
                    "healthy": False,
                    "reason": "unsupported workload kind",
                }
            expected = workload.spec.replicas or 0
            ready = workload.status.ready_replicas or 0
            if expected > 0 and ready >= expected:
                return {
                    "healthy": True,
                    "ready_replicas": ready,
                    "expected_replicas": expected,
                }
            time.sleep(2)
        return {
            "healthy": False,
            "reason": "workload did not become ready before timeout",
        }

