"""Policy-gated automatic remediation orchestration."""

from ops_agent.domain import IncidentStatus


def attempt_auto_remediation(
    *,
    service,
    incident_id: str,
    kubernetes_adapter,
    allowed_namespaces: set[str],
    execution_mode: str,
    redis_connection=None,
    database_url: str | None = None,
):
    incident = service.get_incident(incident_id)
    if (
        incident.status != IncidentStatus.DIAGNOSED
        or incident.resource_kind != "Pod"
        or incident.namespace not in allowed_namespaces
    ):
        return None

    inspection = kubernetes_adapter.inspect_pod(
        namespace=incident.namespace,
        pod_name=incident.resource_name,
    )
    if (
        not inspection.get("exists")
        or not inspection.get("is_abnormal")
        or inspection.get("workload_kind")
        not in {"Deployment", "StatefulSet"}
    ):
        return None

    plan = service.create_plan(
        incident.id,
        {
            "action_type": "RESTART_CONTROLLER_MANAGED_POD",
            "target": {
                "namespace": incident.namespace,
                "pod_name": incident.resource_name,
                "pod_uid": inspection["uid"],
            },
        },
    )
    plan = service.dry_run_plan(
        plan.id, kubernetes_adapter, allowed_namespaces
    )
    plan = service.approve_plan(
        plan.id,
        actor_id="policy-engine",
        comment="低风险自动修复策略批准",
    )
    execution, created = service.create_execution(
        plan.id,
        idempotency_key=f"auto:{incident.id}:{inspection['uid']}",
        requested_by="policy-engine",
        enqueue=execution_mode != "inline",
    )
    if not created:
        return execution
    if execution_mode == "inline":
        return service.process_execution(execution.id, kubernetes_adapter)
    return execution
