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

    @staticmethod
    def _resource_map(resources) -> dict:
        if resources is None:
            return {"requests": {}, "limits": {}}
        return {
            "requests": dict(resources.requests or {}),
            "limits": dict(resources.limits or {}),
        }

    def collect_pod_evidence(
        self, *, namespace: str, pod_name: str, log_tail_lines: int = 200
    ) -> dict:
        """Collect bounded, read-only evidence for diagnosis."""
        from kubernetes.client.exceptions import ApiException

        self._load()
        pod = self._core_api.read_namespaced_pod(pod_name, namespace)
        status_by_name = {
            item.name: item for item in (pod.status.container_statuses or [])
        }
        containers = []
        current_logs = {}
        previous_logs = {}
        resource_limits = {}

        for container in pod.spec.containers or []:
            status = status_by_name.get(container.name)
            state = status.state if status else None
            last_state = status.last_state if status else None
            containers.append(
                {
                    "name": container.name,
                    "ready": bool(status.ready) if status else False,
                    "restart_count": status.restart_count if status else 0,
                    "waiting_reason": (
                        state.waiting.reason
                        if state and state.waiting
                        else None
                    ),
                    "terminated_reason": (
                        state.terminated.reason
                        if state and state.terminated
                        else None
                    ),
                    "last_exit_code": (
                        last_state.terminated.exit_code
                        if last_state and last_state.terminated
                        else None
                    ),
                }
            )
            resource_limits[container.name] = self._resource_map(
                container.resources
            )
            try:
                current_logs[container.name] = (
                    self._core_api.read_namespaced_pod_log(
                        pod_name,
                        namespace,
                        container=container.name,
                        tail_lines=log_tail_lines,
                        timestamps=True,
                    )
                    or ""
                )
            except ApiException:
                current_logs[container.name] = "[log unavailable]"

            if status and (status.restart_count or 0) > 0:
                try:
                    previous_logs[container.name] = (
                        self._core_api.read_namespaced_pod_log(
                            pod_name,
                            namespace,
                            container=container.name,
                            previous=True,
                            tail_lines=log_tail_lines,
                            timestamps=True,
                        )
                        or ""
                    )
                except ApiException:
                    previous_logs[container.name] = "[previous log unavailable]"

        ready = any(
            condition.type == "Ready" and condition.status == "True"
            for condition in pod.status.conditions or []
        )
        pod_status = {
            "exists": True,
            "uid": pod.metadata.uid,
            "phase": pod.status.phase,
            "reason": pod.status.reason,
            "ready": ready,
            "pod_ip": pod.status.pod_ip,
            "node_name": pod.spec.node_name,
            "containers": containers,
        }

        event_list = self._core_api.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.uid={pod.metadata.uid}",
            limit=50,
        )
        events = [
            {
                "type": event.type,
                "reason": event.reason,
                "message": event.message,
                "count": event.count,
                "last_timestamp": str(
                    event.last_timestamp
                    or event.event_time
                    or event.first_timestamp
                    or ""
                ),
            }
            for event in (event_list.items or [])
        ]

        workload_status = self._collect_owner_workload_status(pod, namespace)
        return {
            "pod_status": pod_status,
            "current_logs": current_logs,
            "previous_logs": previous_logs,
            "events": events,
            "workload_status": workload_status,
            "resource_limits": resource_limits,
        }

    def _collect_owner_workload_status(self, pod, namespace: str) -> dict:
        owner_references = pod.metadata.owner_references or []
        if not owner_references:
            return {"kind": None, "name": None, "managed": False}

        owner = owner_references[0]
        if owner.kind == "StatefulSet":
            workload = self._apps_api.read_namespaced_stateful_set_status(
                owner.name, namespace
            )
            return {
                "kind": "StatefulSet",
                "name": owner.name,
                "managed": True,
                "desired_replicas": workload.spec.replicas or 0,
                "ready_replicas": workload.status.ready_replicas or 0,
            }

        if owner.kind == "ReplicaSet":
            replica_set = self._apps_api.read_namespaced_replica_set(
                owner.name, namespace
            )
            deployment_owner = next(
                (
                    parent
                    for parent in replica_set.metadata.owner_references or []
                    if parent.kind == "Deployment"
                ),
                None,
            )
            if deployment_owner:
                workload = self._apps_api.read_namespaced_deployment_status(
                    deployment_owner.name, namespace
                )
                return {
                    "kind": "Deployment",
                    "name": deployment_owner.name,
                    "managed": True,
                    "desired_replicas": workload.spec.replicas or 0,
                    "ready_replicas": workload.status.ready_replicas or 0,
                    "available_replicas": workload.status.available_replicas or 0,
                }

        return {"kind": owner.kind, "name": owner.name, "managed": False}

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
