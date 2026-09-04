"""HTTP API for incidents, action tasks, diagnosis runs and audit events."""

import hmac
from functools import wraps

from flask import Blueprint, current_app, jsonify, request

from ops_agent.domain import InvalidStateTransition
from ops_agent.metrics import INCIDENTS_CREATED
from ops_agent.auto_remediation import attempt_auto_remediation
from ops_agent.services import (
    NotFoundError,
    OpsService,
    PolicyDeniedError,
    ValidationError,
    serialize_agent_run,
    serialize_audit,
    serialize_incident,
    serialize_execution,
    serialize_evidence,
    serialize_plan,
    serialize_task,
)

api = Blueprint("api", __name__, url_prefix="/api")


def service() -> OpsService:
    return current_app.extensions["ops_service"]


def error_response(code: str, message: str, status: int):
    return jsonify({"error": {"code": code, "message": message}}), status


def operator_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_app.config.get("REQUIRE_OPERATOR_AUTH"):
            expected = current_app.config.get("OPERATOR_API_TOKEN", "")
            authorization = request.headers.get("Authorization", "")
            supplied = (
                authorization.removeprefix("Bearer ").strip()
                if authorization.startswith("Bearer ")
                else ""
            )
            if not expected or not hmac.compare_digest(supplied, expected):
                return error_response(
                    "unauthorized", "valid operator token is required", 401
                )
        return view(*args, **kwargs)

    return wrapped


@api.errorhandler(ValidationError)
def handle_validation(error):
    code = (
        "unsupported_mode"
        if str(error) == "unsupported agent run mode"
        else "validation_error"
    )
    return error_response(code, str(error), 400)


@api.errorhandler(NotFoundError)
def handle_not_found(error):
    return error_response("not_found", str(error), 404)


@api.errorhandler(InvalidStateTransition)
def handle_invalid_transition(error):
    return error_response("invalid_state_transition", str(error), 409)


@api.errorhandler(PolicyDeniedError)
def handle_policy_denied(error):
    return error_response("policy_denied", str(error), 409)


@api.post("/incidents")
def create_incident():
    incident, created = service().create_incident(request.get_json(silent=True) or {})
    return jsonify(serialize_incident(incident)), 201 if created else 200


@api.get("/incidents")
def list_incidents():
    return jsonify([serialize_incident(item) for item in service().list_incidents()])


@api.get("/incidents/<incident_id>")
def get_incident(incident_id):
    return jsonify(serialize_incident(service().get_incident(incident_id)))


@api.post("/tasks")
def create_task():
    task = service().create_task(request.get_json(silent=True) or {})
    return jsonify(serialize_task(task)), 201


@api.get("/tasks")
def list_tasks():
    tasks = service().list_tasks(request.args.get("incident_id"))
    return jsonify([serialize_task(task) for task in tasks])


@api.patch("/tasks/<task_id>")
def update_task(task_id):
    body = request.get_json(silent=True) or {}
    if "status" not in body:
        raise ValidationError("status is required")
    return jsonify(serialize_task(service().transition_task(task_id, body["status"])))


@api.post("/incidents/<incident_id>/agent-runs")
def create_agent_run(incident_id):
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "DIAGNOSE_ONLY")
    run, created = service().create_agent_run(
        incident_id,
        mode,
        request.headers.get("Idempotency-Key"),
        enqueue=current_app.config["QUEUE_MODE"] != "inline",
    )
    if created and current_app.config["QUEUE_MODE"] == "inline":
        run = service().process_diagnosis(run.id)
    location = f"/api/agent-runs/{run.id}"
    return jsonify({"id": run.id, "location": location}), 202


@api.get("/agent-runs/<run_id>")
def get_agent_run(run_id):
    return jsonify(serialize_agent_run(service().get_agent_run(run_id)))


@api.get("/agent-runs")
def list_agent_runs():
    return jsonify(
        [
            serialize_agent_run(item)
            for item in service().list_agent_runs(request.args.get("incident_id"))
        ]
    )


@api.get("/agent-runs/<run_id>/evidence")
def list_agent_run_evidence(run_id):
    return jsonify(
        [serialize_evidence(item) for item in service().list_evidence(run_id)]
    )


@api.get("/audit-events")
def list_audit_events():
    events = service().list_audits(
        request.args.get("entity_type"),
        request.args.get("entity_id"),
    )
    return jsonify([serialize_audit(event) for event in events])


@api.get("/executions")
def list_executions():
    return jsonify(
        [
            serialize_execution(execution)
            for execution in service().list_executions()
        ]
    )


@api.post("/alerts/prometheus")
def receive_prometheus_alerts():
    expected_token = current_app.config.get("ALERT_WEBHOOK_TOKEN")
    if expected_token:
        authorization = request.headers.get("Authorization", "")
        bearer_token = (
            authorization.removeprefix("Bearer ").strip()
            if authorization.startswith("Bearer ")
            else ""
        )
        supplied_token = request.headers.get("X-Webhook-Token") or bearer_token
        if not hmac.compare_digest(supplied_token, expected_token):
            return error_response("unauthorized", "invalid webhook token", 401)

    body = request.get_json(silent=True) or {}
    alerts = body.get("alerts")
    if not isinstance(alerts, list):
        raise ValidationError("alerts must be a list")

    accepted = 0
    resolved = 0
    verification_failed = 0
    ignored = 0
    for alert in alerts:
        labels = alert.get("labels") or {}
        annotations = alert.get("annotations") or {}
        alert_name = labels.get("alertname", "PrometheusAlert")
        namespace = labels.get("namespace", "default")
        resource_kind = "Pod" if labels.get("pod") else "Deployment"
        resource_name = (
            labels.get("pod")
            or labels.get("deployment")
            or labels.get("statefulset")
            or "unknown"
        )
        severity_map = {
            "info": "LOW",
            "warning": "HIGH",
            "critical": "CRITICAL",
        }
        severity = severity_map.get(
            str(labels.get("severity", "warning")).lower(), "MEDIUM"
        )
        fingerprint = alert.get("fingerprint") or (
            f"{labels.get('cluster', 'default')}:{namespace}:"
            f"{resource_kind}:{resource_name}:{alert_name}"
        )
        alert_status = alert.get("status") or body.get("status")
        if alert_status == "resolved":
            _, outcome = service().resolve_incident_from_alert(
                fingerprint,
                current_app.extensions["incident_verifier"],
            )
            if outcome == "resolved":
                resolved += 1
            elif outcome == "verification_failed":
                verification_failed += 1
            else:
                ignored += 1
            continue
        if alert_status != "firing":
            ignored += 1
            continue
        incident, created = service().create_incident(
            {
                "title": annotations.get("summary") or alert_name,
                "severity": severity,
                "fingerprint": fingerprint,
                "cluster": labels.get("cluster", "default"),
                "namespace": namespace,
                "resource_kind": resource_kind,
                "resource_name": resource_name,
                "source": "prometheus",
                "summary": annotations.get("description")
                or annotations.get("summary"),
                "source_context": {
                    "status": alert_status,
                    "labels": labels,
                    "annotations": annotations,
                },
            }
        )
        accepted += 1
        if created:
            INCIDENTS_CREATED.labels(severity, "prometheus").inc()
            run, _ = service().create_agent_run(
                incident.id,
                "DIAGNOSE_ONLY",
                f"alert:{incident.id}:diagnosis",
                enqueue=current_app.config["QUEUE_MODE"] != "inline",
            )
            if current_app.config["QUEUE_MODE"] == "inline":
                service().process_diagnosis(run.id)
                if current_app.config["AUTO_REMEDIATE_ENABLED"]:
                    attempt_auto_remediation(
                        service=service(),
                        incident_id=incident.id,
                        kubernetes_adapter=current_app.extensions[
                            "kubernetes_adapter"
                        ],
                        allowed_namespaces=current_app.config[
                            "ALLOWED_NAMESPACES"
                        ],
                        execution_mode=current_app.config[
                            "EXECUTION_MODE"
                        ],
                        redis_connection=current_app.extensions["redis"],
                        database_url=current_app.config["DATABASE_URL"],
                    )

    return jsonify(
        {
            "accepted": accepted,
            "resolved": resolved,
            "verification_failed": verification_failed,
            "ignored": ignored,
        }
    ), 202


@api.post("/incidents/<incident_id>/plans")
def create_plan(incident_id):
    body = request.get_json(silent=True) or {}
    target = body.get("target") or {}
    if (
        body.get("action_type") == "RESTART_CONTROLLER_MANAGED_POD"
        and not target.get("pod_uid")
    ):
        incident = service().get_incident(incident_id)
        namespace = target.get("namespace") or incident.namespace
        pod_name = target.get("pod_name") or incident.resource_name
        inspection = current_app.extensions[
            "kubernetes_adapter"
        ].inspect_pod(namespace=namespace, pod_name=pod_name)
        if not inspection.get("exists"):
            raise ValidationError("target Pod does not exist")
        body = {
            **body,
            "target": {
                **target,
                "namespace": namespace,
                "pod_name": pod_name,
                "pod_uid": inspection["uid"],
            },
        }
    plan = service().create_plan(incident_id, body)
    return jsonify(serialize_plan(plan)), 201


@api.get("/plans")
def list_plans():
    return jsonify(
        [
            serialize_plan(plan)
            for plan in service().list_plans(
                request.args.get("incident_id")
            )
        ]
    )


@api.get("/plans/<plan_id>")
def get_plan(plan_id):
    return jsonify(serialize_plan(service().get_plan(plan_id)))


@api.post("/plans/<plan_id>/dry-run")
def dry_run_plan(plan_id):
    plan = service().dry_run_plan(
        plan_id,
        current_app.extensions["kubernetes_adapter"],
        current_app.config["ALLOWED_NAMESPACES"],
    )
    return jsonify(serialize_plan(plan))


@api.post("/plans/<plan_id>/approve")
@operator_required
def approve_plan(plan_id):
    body = request.get_json(silent=True) or {}
    plan = service().approve_plan(
        plan_id,
        request.headers.get("X-Actor-ID", "anonymous"),
        body.get("comment"),
    )
    return jsonify(serialize_plan(plan))


@api.post("/plans/<plan_id>/reject")
@operator_required
def reject_plan(plan_id):
    body = request.get_json(silent=True) or {}
    plan = service().reject_plan(
        plan_id,
        request.headers.get("X-Actor-ID", "anonymous"),
        body.get("comment"),
    )
    return jsonify(serialize_plan(plan))


@api.post("/plans/<plan_id>/execute")
@operator_required
def execute_plan(plan_id):
    execution, created = service().create_execution(
        plan_id,
        request.headers.get("Idempotency-Key", ""),
        request.headers.get("X-Actor-ID", "anonymous"),
        enqueue=current_app.config["EXECUTION_MODE"] != "inline",
    )
    if created and current_app.config["EXECUTION_MODE"] == "inline":
        execution = service().process_execution(
            execution.id,
            current_app.extensions["kubernetes_adapter"],
        )
    location = f"/api/executions/{execution.id}"
    return jsonify({"id": execution.id, "location": location}), 202


@api.get("/executions/<execution_id>")
def get_execution(execution_id):
    return jsonify(
        serialize_execution(service().get_execution(execution_id))
    )
