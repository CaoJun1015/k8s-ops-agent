"""Application services coordinating persistence and domain rules."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ops_agent.domain import (
    AgentRunMode,
    AgentRunStatus,
    IncidentSeverity,
    IncidentStatus,
    InvalidStateTransition,
    Priority,
    PlanStatus,
    ExecutionStatus,
    RiskLevel,
    TaskStatus,
    TaskType,
    validate_transition,
)
from ops_agent.models import (
    AgentRun,
    AuditEvent,
    Evidence,
    Execution,
    Incident,
    OutboxEvent,
    Plan,
    Task,
)
from ops_agent.metrics import AGENT_RUNS, REMEDIATION_EXECUTIONS
from ops_agent.diagnosis import RuleDiagnosis
from ops_agent.evidence import EvidenceCollector, sanitize_content


class ValidationError(ValueError):
    pass


class NotFoundError(LookupError):
    pass


class PolicyDeniedError(PermissionError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def enum_value(value):
    return value.value if hasattr(value, "value") else value


def safe_error_message(error: Any, max_bytes: int = 2_000) -> str:
    clean, _ = sanitize_content({"message": str(error)}, max_bytes=max_bytes)
    return str(clean.get("message") or clean.get("data") or "operation failed")


def serialize_incident(incident: Incident) -> dict[str, Any]:
    return {
        "id": incident.id,
        "title": incident.title,
        "severity": enum_value(incident.severity),
        "status": enum_value(incident.status),
        "fingerprint": incident.fingerprint,
        "cluster": incident.cluster,
        "namespace": incident.namespace,
        "resource_kind": incident.resource_kind,
        "resource_name": incident.resource_name,
        "source": incident.source,
        "summary": incident.summary,
        "first_seen_at": incident.first_seen_at.isoformat(),
        "last_seen_at": incident.last_seen_at.isoformat(),
        "resolved_at": (
            incident.resolved_at.isoformat() if incident.resolved_at else None
        ),
        "occurrence_count": incident.occurrence_count,
        "version": incident.version,
    }


def serialize_task(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "incident_id": task.incident_id,
        "title": task.title,
        "description": task.description,
        "task_type": enum_value(task.task_type),
        "priority": enum_value(task.priority),
        "status": enum_value(task.status),
        "assignee": task.assignee,
        "created_by": task.created_by,
        "approval_required": task.approval_required,
        "related_entity_type": task.related_entity_type,
        "related_entity_id": task.related_entity_id,
        "due_at": task.due_at.isoformat() if task.due_at else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def serialize_agent_run(run: AgentRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "incident_id": run.incident_id,
        "mode": enum_value(run.mode),
        "status": enum_value(run.status),
        "diagnosis": run.diagnosis,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "created_at": run.created_at.isoformat(),
    }


def serialize_evidence(evidence: Evidence) -> dict[str, Any]:
    return {
        "id": evidence.id,
        "incident_id": evidence.incident_id,
        "agent_run_id": evidence.agent_run_id,
        "evidence_type": enum_value(evidence.evidence_type),
        "source": evidence.source,
        "content": evidence.content,
        "content_hash": evidence.content_hash,
        "redacted": evidence.redacted,
        "collected_at": evidence.collected_at.isoformat(),
        "expires_at": (
            evidence.expires_at.isoformat() if evidence.expires_at else None
        ),
    }


def serialize_plan(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "incident_id": plan.incident_id,
        "action_type": plan.action_type,
        "target": plan.target,
        "risk_level": enum_value(plan.risk_level),
        "status": enum_value(plan.status),
        "dry_run_result": plan.dry_run_result,
        "verification_spec": plan.verification_spec,
        "rollback_spec": plan.rollback_spec,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }


def serialize_execution(execution: Execution) -> dict[str, Any]:
    return {
        "id": execution.id,
        "plan_id": execution.plan_id,
        "agent_run_id": execution.agent_run_id,
        "status": enum_value(execution.status),
        "requested_by": execution.requested_by,
        "approved_by": execution.approved_by,
        "approved_at": (
            execution.approved_at.isoformat() if execution.approved_at else None
        ),
        "started_at": (
            execution.started_at.isoformat() if execution.started_at else None
        ),
        "finished_at": (
            execution.finished_at.isoformat() if execution.finished_at else None
        ),
        "result": execution.result,
        "error": execution.error,
        "verification_result": execution.verification_result,
        "rollback_result": execution.rollback_result,
        "created_at": execution.created_at.isoformat(),
    }


def serialize_audit(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "event_type": event.event_type,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "payload": event.payload,
        "occurred_at": event.occurred_at.isoformat(),
    }


class OpsService:
    def __init__(
        self,
        session_factory,
        *,
        evidence_collector=None,
        diagnosis_engine=None,
    ):
        self.session_factory = session_factory
        self.evidence_collector = evidence_collector or EvidenceCollector()
        self.diagnosis_engine = diagnosis_engine or RuleDiagnosis()

    @staticmethod
    def _audit(
        session,
        entity_type: str,
        entity_id: str,
        event_type: str,
        *,
        actor_type: str = "USER",
        actor_id: str = "anonymous",
        payload: dict[str, Any] | None = None,
    ) -> None:
        session.add(
            AuditEvent(
                entity_type=entity_type,
                entity_id=entity_id,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=actor_id,
                payload=payload or {},
            )
        )

    @staticmethod
    def _outbox(
        session,
        topic: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: dict[str, Any],
    ) -> None:
        session.add(
            OutboxEvent(
                topic=topic,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
            )
        )

    def create_incident(self, data: dict[str, Any]) -> tuple[Incident, bool]:
        required = (
            "title",
            "fingerprint",
            "namespace",
            "resource_kind",
            "resource_name",
            "source",
        )
        missing = [field for field in required if not str(data.get(field, "")).strip()]
        if missing:
            raise ValidationError(f"missing required fields: {', '.join(missing)}")

        try:
            severity = IncidentSeverity(data.get("severity", "MEDIUM"))
        except ValueError as error:
            raise ValidationError("invalid severity") from error

        safe_fields, _ = sanitize_content(
            {
                "title": str(data["title"]).strip(),
                "summary": data.get("summary"),
            },
            max_bytes=4_000,
        )
        if safe_fields.get("truncated"):
            raise ValidationError("incident title or summary exceeds safe limit")
        if len(safe_fields["title"]) > 200:
            raise ValidationError("title exceeds max length of 200")

        fingerprint = str(data["fingerprint"]).strip()

        def observe_existing() -> Incident | None:
            observed_at = utc_now()
            with self.session_factory.begin() as session:
                existing_id = session.scalar(
                    update(Incident)
                    .where(
                        Incident.fingerprint == fingerprint,
                        Incident.status.notin_(
                            [IncidentStatus.RESOLVED, IncidentStatus.CLOSED]
                        ),
                    )
                    .values(
                        last_seen_at=observed_at,
                        updated_at=observed_at,
                        occurrence_count=Incident.occurrence_count + 1,
                        version=Incident.version + 1,
                    )
                    .returning(Incident.id)
                )
                if not existing_id:
                    return None
                existing = session.get(Incident, existing_id)
                self._audit(
                    session,
                    "Incident",
                    existing.id,
                    "incident.observed_again",
                    actor_type="SYSTEM",
                    actor_id=data["source"],
                )
                return existing

        existing = observe_existing()
        if existing:
            return existing, False

        try:
            with self.session_factory.begin() as session:
                source_context, _ = sanitize_content(
                    data.get("source_context") or {}
                )
                incident = Incident(
                    title=safe_fields["title"],
                    severity=severity,
                    fingerprint=fingerprint,
                    cluster=str(data.get("cluster") or "default").strip(),
                    namespace=str(data["namespace"]).strip(),
                    resource_kind=str(data["resource_kind"]).strip(),
                    resource_name=str(data["resource_name"]).strip(),
                    source=str(data["source"]).strip(),
                    summary=safe_fields["summary"],
                    source_context=source_context,
                )
                session.add(incident)
                session.flush()
                self._audit(
                    session,
                    "Incident",
                    incident.id,
                    "incident.created",
                    actor_type="SYSTEM",
                    actor_id=incident.source,
                )
                return incident, True
        except IntegrityError:
            existing = observe_existing()
            if existing:
                return existing, False
            raise

    def list_incidents(self) -> list[Incident]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Incident).order_by(Incident.created_at.desc())
                ).all()
            )

    def get_incident(self, incident_id: str) -> Incident:
        with self.session_factory() as session:
            incident = session.get(Incident, incident_id)
            if not incident:
                raise NotFoundError("incident not found")
            return incident

    def resolve_incident_from_alert(
        self,
        fingerprint: str,
        verifier,
    ) -> tuple[Incident | None, str]:
        """Verify an Alertmanager recovery before resolving its Incident."""
        with self.session_factory.begin() as session:
            incident = session.scalar(
                select(Incident)
                .where(
                    Incident.fingerprint == fingerprint,
                    Incident.status.notin_(
                        [IncidentStatus.RESOLVED, IncidentStatus.CLOSED]
                    ),
                )
                .order_by(Incident.created_at.desc())
            )
            if not incident:
                return None, "ignored"
            if incident.status != IncidentStatus.VERIFYING:
                validate_transition(incident.status, IncidentStatus.VERIFYING)
                previous = incident.status
                incident.status = IncidentStatus.VERIFYING
            else:
                previous = IncidentStatus.VERIFYING
            self._audit(
                session,
                "Incident",
                incident.id,
                "alert.resolved_received",
                actor_type="SYSTEM",
                actor_id="alertmanager",
                payload={"from": previous.value, "fingerprint": fingerprint},
            )

        try:
            verification = verifier.verify(incident)
        except Exception as error:
            verification = {
                "healthy": False,
                "checks": [],
                "error_type": type(error).__name__,
                "reason": "verification failed unexpectedly",
            }

        with self.session_factory.begin() as session:
            incident = session.get(Incident, incident.id)
            verification_task = session.scalar(
                select(Task).where(
                    Task.incident_id == incident.id,
                    Task.task_type == TaskType.VERIFICATION,
                    Task.related_entity_type == "Incident",
                    Task.related_entity_id == incident.id,
                    Task.status.in_(
                        [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]
                    ),
                )
            )
            if verification.get("healthy"):
                validate_transition(incident.status, IncidentStatus.RESOLVED)
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = utc_now()
                outcome = "resolved"
                event_type = "incident.recovery_verified"
                if verification_task:
                    if verification_task.status in {
                        TaskStatus.TODO,
                        TaskStatus.BLOCKED,
                    }:
                        validate_transition(
                            verification_task.status, TaskStatus.IN_PROGRESS
                        )
                        verification_task.status = TaskStatus.IN_PROGRESS
                    validate_transition(
                        verification_task.status, TaskStatus.DONE
                    )
                    verification_task.status = TaskStatus.DONE
                    self._audit(
                        session,
                        "Task",
                        verification_task.id,
                        "task.completed_by_verification",
                        actor_type="AGENT",
                        actor_id="recovery-verifier",
                        payload={"incident_id": incident.id},
                    )
            else:
                validate_transition(incident.status, IncidentStatus.FAILED)
                incident.status = IncidentStatus.FAILED
                outcome = "verification_failed"
                event_type = "incident.recovery_verification_failed"
                if not verification_task:
                    task = Task(
                        incident_id=incident.id,
                        title="验证告警恢复状态",
                        description="Alertmanager 已恢复，但资源或指标验证未通过。",
                        task_type=TaskType.VERIFICATION,
                        priority=Priority.HIGH,
                        created_by="system",
                        related_entity_type="Incident",
                        related_entity_id=incident.id,
                    )
                    session.add(task)
                    session.flush()
                    self._audit(
                        session,
                        "Task",
                        task.id,
                        "task.created",
                        actor_type="SYSTEM",
                        actor_id="recovery-verifier",
                        payload={"incident_id": incident.id},
                    )
            self._audit(
                session,
                "Incident",
                incident.id,
                event_type,
                actor_type="AGENT",
                actor_id="recovery-verifier",
                payload={"verification": verification},
            )
            return incident, outcome

    def create_task(self, data: dict[str, Any]) -> Task:
        incident_id = str(data.get("incident_id", "")).strip()
        title = str(data.get("title", "")).strip()
        if not incident_id or not title:
            raise ValidationError("incident_id and title are required")
        if len(title) > 200:
            raise ValidationError("title exceeds max length of 200")

        try:
            task_type = TaskType(data.get("task_type", "MANUAL_CHECK"))
            priority = Priority(data.get("priority", "MEDIUM"))
        except ValueError as error:
            raise ValidationError("invalid task_type or priority") from error

        with self.session_factory.begin() as session:
            if not session.get(Incident, incident_id):
                raise NotFoundError("incident not found")
            task = Task(
                incident_id=incident_id,
                title=title,
                description=data.get("description"),
                task_type=task_type,
                priority=priority,
                assignee=data.get("assignee"),
                created_by=data.get("created_by") or "user",
                approval_required=bool(data.get("approval_required", False)),
                related_entity_type=data.get("related_entity_type"),
                related_entity_id=data.get("related_entity_id"),
            )
            session.add(task)
            session.flush()
            self._audit(
                session,
                "Task",
                task.id,
                "task.created",
                payload={"incident_id": incident_id},
            )
            return task

    def list_tasks(self, incident_id: str | None = None) -> list[Task]:
        with self.session_factory() as session:
            statement = select(Task)
            if incident_id:
                statement = statement.where(Task.incident_id == incident_id)
            return list(
                session.scalars(statement.order_by(Task.created_at.desc())).all()
            )

    def transition_task(self, task_id: str, status: str) -> Task:
        try:
            target = TaskStatus(status)
        except ValueError as error:
            raise ValidationError("invalid task status") from error
        with self.session_factory.begin() as session:
            task = session.get(Task, task_id)
            if not task:
                raise NotFoundError("task not found")
            validate_transition(task.status, target)
            previous = task.status
            task.status = target
            self._audit(
                session,
                "Task",
                task.id,
                "task.status_changed",
                payload={"from": enum_value(previous), "to": target.value},
            )
            return task

    def create_agent_run(
        self,
        incident_id: str,
        mode: str,
        idempotency_key: str | None,
        *,
        enqueue: bool = False,
    ) -> tuple[AgentRun, bool]:
        try:
            run_mode = AgentRunMode(mode)
        except ValueError as error:
            raise ValidationError("unsupported agent run mode") from error

        def existing_run() -> AgentRun | None:
            if not idempotency_key:
                return None
            with self.session_factory() as session:
                existing = session.scalar(
                    select(AgentRun).where(
                        AgentRun.idempotency_key == idempotency_key
                    )
                )
                if existing:
                    if existing.incident_id != incident_id:
                        raise ValidationError(
                            "Idempotency-Key belongs to another incident"
                        )
                    return existing
                return None

        existing = existing_run()
        if existing:
            return existing, False
        try:
            with self.session_factory.begin() as session:
                incident = session.get(Incident, incident_id)
                if not incident:
                    raise NotFoundError("incident not found")
                run = AgentRun(
                    incident_id=incident_id,
                    mode=run_mode,
                    idempotency_key=idempotency_key,
                )
                session.add(run)
                session.flush()
                self._audit(
                    session,
                    "AgentRun",
                    run.id,
                    "agent_run.created",
                    actor_type="SYSTEM",
                    actor_id="api",
                    payload={"mode": run_mode.value},
                )
                if enqueue:
                    self._outbox(
                        session,
                        "agent_run.requested",
                        "AgentRun",
                        run.id,
                        {"run_id": run.id},
                    )
                return run, True
        except IntegrityError:
            existing = existing_run()
            if existing:
                return existing, False
            raise

    def process_diagnosis(self, run_id: str) -> AgentRun:
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise NotFoundError("agent run not found")
            incident = session.get(Incident, run.incident_id)

            validate_transition(run.status, AgentRunStatus.COLLECTING)
            run.status = AgentRunStatus.COLLECTING
            run.started_at = utc_now()
            if incident.status in {IncidentStatus.OPEN, IncidentStatus.FAILED}:
                validate_transition(incident.status, IncidentStatus.DIAGNOSING)
                incident.status = IncidentStatus.DIAGNOSING

        collected = self.evidence_collector.collect(incident)

        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise NotFoundError("agent run not found")
            incident = session.get(Incident, run.incident_id)
            validate_transition(run.status, AgentRunStatus.DIAGNOSING)
            run.status = AgentRunStatus.DIAGNOSING
            persisted = []
            for item in collected:
                serialized = json.dumps(
                    item.content,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                record = Evidence(
                    incident_id=incident.id,
                    agent_run_id=run.id,
                    evidence_type=item.evidence_type,
                    source=item.source,
                    content=item.content,
                    content_hash=hashlib.sha256(
                        serialized.encode("utf-8")
                    ).hexdigest(),
                    redacted=item.redacted,
                )
                session.add(record)
                persisted.append(record)
            session.flush()
            run.diagnosis = self.diagnosis_engine.diagnose(
                incident, persisted
            )

            validate_transition(run.status, AgentRunStatus.COMPLETED)
            run.status = AgentRunStatus.COMPLETED
            run.finished_at = utc_now()
            if incident.status == IncidentStatus.DIAGNOSING:
                validate_transition(incident.status, IncidentStatus.DIAGNOSED)
                incident.status = IncidentStatus.DIAGNOSED
            self._audit(
                session,
                "AgentRun",
                run.id,
                "agent_run.completed",
                actor_type="AGENT",
                actor_id="diagnosis-worker",
                payload={"incident_id": incident.id},
            )
            AGENT_RUNS.labels(run.mode.value, "COMPLETED").inc()
            return run

    def get_agent_run(self, run_id: str) -> AgentRun:
        with self.session_factory() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise NotFoundError("agent run not found")
            return run

    def list_agent_runs(self, incident_id: str | None = None) -> list[AgentRun]:
        with self.session_factory() as session:
            statement = select(AgentRun)
            if incident_id:
                statement = statement.where(AgentRun.incident_id == incident_id)
            return list(
                session.scalars(
                    statement.order_by(AgentRun.created_at.desc())
                ).all()
            )

    def list_evidence(self, run_id: str) -> list[Evidence]:
        with self.session_factory() as session:
            if not session.get(AgentRun, run_id):
                raise NotFoundError("agent run not found")
            return list(
                session.scalars(
                    select(Evidence)
                    .where(Evidence.agent_run_id == run_id)
                    .order_by(Evidence.collected_at, Evidence.id)
                ).all()
            )

    def fail_agent_run(self, run_id: str, error: str) -> AgentRun:
        with self.session_factory.begin() as session:
            run = session.get(AgentRun, run_id)
            if not run:
                raise NotFoundError("agent run not found")
            if run.status not in {
                AgentRunStatus.COMPLETED,
                AgentRunStatus.FAILED,
            }:
                validate_transition(run.status, AgentRunStatus.FAILED)
                run.status = AgentRunStatus.FAILED
            run.error = safe_error_message(error)
            run.finished_at = utc_now()
            incident = session.get(Incident, run.incident_id)
            if incident.status == IncidentStatus.DIAGNOSING:
                validate_transition(incident.status, IncidentStatus.FAILED)
                incident.status = IncidentStatus.FAILED
            self._audit(
                session,
                "AgentRun",
                run.id,
                "agent_run.failed",
                actor_type="AGENT",
                actor_id="diagnosis-worker",
                payload={"error": run.error},
            )
            AGENT_RUNS.labels(run.mode.value, "FAILED").inc()
            return run

    def list_audits(
        self, entity_type: str | None, entity_id: str | None
    ) -> list[AuditEvent]:
        with self.session_factory() as session:
            statement = select(AuditEvent)
            if entity_type:
                statement = statement.where(AuditEvent.entity_type == entity_type)
            if entity_id:
                statement = statement.where(AuditEvent.entity_id == entity_id)
            return list(
                session.scalars(
                    statement.order_by(AuditEvent.occurred_at.asc())
                ).all()
            )

    def list_executions(self) -> list[Execution]:
        with self.session_factory() as session:
            return list(
                session.scalars(
                    select(Execution).order_by(Execution.created_at.desc())
                ).all()
            )

    def create_plan(self, incident_id: str, data: dict[str, Any]) -> Plan:
        action_type = str(data.get("action_type", "")).strip()
        if action_type != "RESTART_CONTROLLER_MANAGED_POD":
            raise ValidationError("unsupported action_type")
        target = data.get("target")
        if not isinstance(target, dict):
            raise ValidationError("target is required")
        required_target = ("namespace", "pod_name", "pod_uid")
        if any(not str(target.get(field, "")).strip() for field in required_target):
            raise ValidationError(
                "target requires namespace, pod_name and pod_uid"
            )

        with self.session_factory.begin() as session:
            incident = session.get(Incident, incident_id)
            if not incident:
                raise NotFoundError("incident not found")
            if incident.status != IncidentStatus.DIAGNOSED:
                raise InvalidStateTransition(
                    "incident must be DIAGNOSED before creating a plan"
                )
            plan = Plan(
                incident_id=incident_id,
                action_type=action_type,
                target=target,
                risk_level=RiskLevel.LOW,
                verification_spec={
                    "type": "WORKLOAD_READY",
                    "timeout_seconds": 120,
                },
                rollback_spec={
                    "type": "NONE",
                    "reason": "controller recreates the deleted Pod",
                },
            )
            session.add(plan)
            session.flush()
            validate_transition(incident.status, IncidentStatus.PLAN_READY)
            incident.status = IncidentStatus.PLAN_READY
            self._audit(
                session,
                "Plan",
                plan.id,
                "plan.created",
                payload={"incident_id": incident_id, "action_type": action_type},
            )
            return plan

    def dry_run_plan(
        self,
        plan_id: str,
        kubernetes_adapter,
        allowed_namespaces: set[str],
    ) -> Plan:
        denial_reason = None
        with self.session_factory.begin() as session:
            plan = session.get(Plan, plan_id)
            if not plan:
                raise NotFoundError("plan not found")
            if plan.status != PlanStatus.DRAFT:
                raise InvalidStateTransition("plan must be DRAFT for dry-run")

            namespace = plan.target["namespace"]
            if namespace not in allowed_namespaces or namespace in {
                "kube-system",
                "kube-public",
                "kube-node-lease",
            }:
                denial_reason = "namespace is not allowed"
                inspection = {}
            else:
                inspection = kubernetes_adapter.inspect_pod(
                    namespace=namespace,
                    pod_name=plan.target["pod_name"],
                )
                if not inspection.get("exists"):
                    denial_reason = "target Pod does not exist"
                elif inspection.get("uid") != plan.target["pod_uid"]:
                    denial_reason = "target Pod UID changed"
                elif inspection.get("workload_kind") not in {
                    "Deployment",
                    "StatefulSet",
                }:
                    denial_reason = (
                        "Pod is not managed by an allowed controller"
                    )
                elif not inspection.get("is_abnormal"):
                    denial_reason = "healthy Pod cannot be restarted"

            if denial_reason:
                plan.dry_run_result = {
                    "allowed": False,
                    "reason": denial_reason,
                    "inspection": inspection,
                }
                self._audit(
                    session,
                    "Plan",
                    plan.id,
                    "plan.policy_denied",
                    payload=plan.dry_run_result,
                )
                validate_transition(plan.status, PlanStatus.CANCELLED)
                plan.status = PlanStatus.CANCELLED
                incident = session.get(Incident, plan.incident_id)
                validate_transition(
                    incident.status, IncidentStatus.DIAGNOSED
                )
                incident.status = IncidentStatus.DIAGNOSED
            else:
                plan.dry_run_result = {
                    "allowed": True,
                    "reason": "controller-managed abnormal Pod",
                    "inspection": inspection,
                }
                plan.target = {
                    **plan.target,
                    "workload_kind": inspection["workload_kind"],
                    "workload_name": inspection["workload_name"],
                }
                validate_transition(plan.status, PlanStatus.DRY_RUN_READY)
                plan.status = PlanStatus.DRY_RUN_READY
                validate_transition(plan.status, PlanStatus.AWAITING_APPROVAL)
                plan.status = PlanStatus.AWAITING_APPROVAL

                incident = session.get(Incident, plan.incident_id)
                validate_transition(
                    incident.status, IncidentStatus.AWAITING_APPROVAL
                )
                incident.status = IncidentStatus.AWAITING_APPROVAL

                approval_task = Task(
                    incident_id=incident.id,
                    title=(
                        f"审批重建 Pod {namespace}/"
                        f"{plan.target['pod_name']}"
                    ),
                    description="dry-run 已通过，等待值班人员确认执行",
                    task_type=TaskType.APPROVAL,
                    priority=Priority.HIGH,
                    approval_required=True,
                    created_by="policy-engine",
                    related_entity_type="Plan",
                    related_entity_id=plan.id,
                )
                session.add(approval_task)
                self._audit(
                    session,
                    "Plan",
                    plan.id,
                    "plan.dry_run_completed",
                    actor_type="AGENT",
                    actor_id="policy-engine",
                    payload=plan.dry_run_result,
                )
        if denial_reason:
            raise PolicyDeniedError(denial_reason)
        return plan

    def approve_plan(
        self, plan_id: str, actor_id: str, comment: str | None
    ) -> Plan:
        with self.session_factory.begin() as session:
            plan = session.get(Plan, plan_id)
            if not plan:
                raise NotFoundError("plan not found")
            validate_transition(plan.status, PlanStatus.APPROVED)
            plan.status = PlanStatus.APPROVED
            validate_transition(plan.status, PlanStatus.EXECUTABLE)
            plan.status = PlanStatus.EXECUTABLE

            approval_task = session.scalar(
                select(Task).where(
                    Task.related_entity_type == "Plan",
                    Task.related_entity_id == plan.id,
                    Task.task_type == TaskType.APPROVAL,
                    Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS]),
                )
            )
            if approval_task:
                if approval_task.status == TaskStatus.TODO:
                    validate_transition(
                        approval_task.status, TaskStatus.IN_PROGRESS
                    )
                    approval_task.status = TaskStatus.IN_PROGRESS
                validate_transition(approval_task.status, TaskStatus.DONE)
                approval_task.status = TaskStatus.DONE

            self._audit(
                session,
                "Plan",
                plan.id,
                "plan.approved",
                actor_id=actor_id,
                payload={"comment": comment or ""},
            )
            return plan

    def reject_plan(
        self, plan_id: str, actor_id: str, comment: str | None
    ) -> Plan:
        with self.session_factory.begin() as session:
            plan = session.get(Plan, plan_id)
            if not plan:
                raise NotFoundError("plan not found")
            validate_transition(plan.status, PlanStatus.REJECTED)
            plan.status = PlanStatus.REJECTED
            incident = session.get(Incident, plan.incident_id)
            validate_transition(incident.status, IncidentStatus.DIAGNOSED)
            incident.status = IncidentStatus.DIAGNOSED

            approval_task = session.scalar(
                select(Task).where(
                    Task.related_entity_type == "Plan",
                    Task.related_entity_id == plan.id,
                    Task.task_type == TaskType.APPROVAL,
                    Task.status.in_(
                        [
                            TaskStatus.TODO,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.BLOCKED,
                        ]
                    ),
                )
            )
            if approval_task:
                validate_transition(
                    approval_task.status, TaskStatus.CANCELLED
                )
                approval_task.status = TaskStatus.CANCELLED
            self._audit(
                session,
                "Plan",
                plan.id,
                "plan.rejected",
                actor_id=actor_id,
                payload={"comment": comment or ""},
            )
            return plan

    def create_execution(
        self,
        plan_id: str,
        idempotency_key: str,
        requested_by: str,
        *,
        enqueue: bool = False,
    ) -> tuple[Execution, bool]:
        if not idempotency_key:
            raise ValidationError("Idempotency-Key is required")

        def existing_execution() -> Execution | None:
            with self.session_factory() as session:
                existing = session.scalar(
                select(Execution).where(
                    Execution.idempotency_key == idempotency_key
                )
            )
            if existing:
                if existing.plan_id != plan_id:
                    raise ValidationError(
                        "Idempotency-Key belongs to another plan"
                    )
                return existing
            return None

        existing = existing_execution()
        if existing:
            return existing, False
        try:
            with self.session_factory.begin() as session:
                plan = session.get(Plan, plan_id)
                if not plan:
                    raise NotFoundError("plan not found")
                if plan.status != PlanStatus.EXECUTABLE:
                    raise InvalidStateTransition(
                        "plan must be approved and executable"
                    )
                execution = Execution(
                    plan_id=plan.id,
                    status=ExecutionStatus.PREPARED,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                    approved_by=requested_by,
                    approved_at=utc_now(),
                )
                session.add(execution)
                session.flush()
                self._audit(
                    session,
                    "Execution",
                    execution.id,
                    "execution.created",
                    actor_id=requested_by,
                    payload={"plan_id": plan.id},
                )
                if enqueue:
                    self._outbox(
                        session,
                        "execution.requested",
                        "Execution",
                        execution.id,
                        {"execution_id": execution.id},
                    )
                return execution, True
        except IntegrityError:
            existing = existing_execution()
            if existing:
                return existing, False
            raise

    def process_execution(self, execution_id: str, kubernetes_adapter) -> Execution:
        """Execute and verify without holding a database transaction over I/O."""
        with self.session_factory.begin() as session:
            execution = session.get(Execution, execution_id)
            if not execution:
                raise NotFoundError("execution not found")
            plan = session.get(Plan, execution.plan_id)
            incident = session.get(Incident, plan.incident_id)

            validate_transition(execution.status, ExecutionStatus.RUNNING)
            execution.status = ExecutionStatus.RUNNING
            execution.started_at = utc_now()
            validate_transition(
                incident.status, IncidentStatus.REMEDIATING
            )
            incident.status = IncidentStatus.REMEDIATING
            self._audit(
                session,
                "Execution",
                execution.id,
                "execution.started",
                actor_type="AGENT",
                actor_id="execution-worker",
            )
            target = dict(plan.target)
            action_type = plan.action_type

        try:
            action_result = kubernetes_adapter.delete_pod(
                namespace=target["namespace"],
                pod_name=target["pod_name"],
                expected_uid=target["pod_uid"],
            )
        except Exception as error:
            with self.session_factory.begin() as session:
                execution = session.get(Execution, execution_id)
                plan = session.get(Plan, execution.plan_id)
                incident = session.get(Incident, plan.incident_id)
                validate_transition(
                    execution.status, ExecutionStatus.FAILED
                )
                execution.status = ExecutionStatus.FAILED
                execution.error = safe_error_message(error)
                execution.finished_at = utc_now()
                validate_transition(incident.status, IncidentStatus.FAILED)
                incident.status = IncidentStatus.FAILED
                session.add(
                    Task(
                        incident_id=incident.id,
                        title="修复执行失败，需要人工处理",
                        description=execution.error,
                        task_type=TaskType.FOLLOW_UP,
                        priority=Priority.CRITICAL,
                        created_by="execution-worker",
                        related_entity_type="Execution",
                        related_entity_id=execution.id,
                    )
                )
                self._audit(
                    session,
                    "Execution",
                    execution.id,
                    "execution.failed",
                    actor_type="AGENT",
                    actor_id="execution-worker",
                    payload={"error": execution.error},
                )
                REMEDIATION_EXECUTIONS.labels(
                    action_type, "FAILED"
                ).inc()
                return execution

        with self.session_factory.begin() as session:
            execution = session.get(Execution, execution_id)
            plan = session.get(Plan, execution.plan_id)
            incident = session.get(Incident, plan.incident_id)
            execution.result = action_result
            validate_transition(execution.status, ExecutionStatus.SUCCEEDED)
            execution.status = ExecutionStatus.SUCCEEDED
            execution.finished_at = utc_now()
            validate_transition(plan.status, PlanStatus.EXECUTED)
            plan.status = PlanStatus.EXECUTED
            validate_transition(incident.status, IncidentStatus.VERIFYING)
            incident.status = IncidentStatus.VERIFYING
            self._audit(
                session,
                "Execution",
                execution.id,
                "execution.succeeded",
                actor_type="AGENT",
                actor_id="execution-worker",
                payload=execution.result,
            )

        try:
            verification = kubernetes_adapter.verify_recovery(target)
        except Exception as error:
            verification = {
                "healthy": False,
                "reason": "verification raised an exception",
                "error": safe_error_message(error),
                "error_type": type(error).__name__,
            }

        with self.session_factory.begin() as session:
            execution = session.get(Execution, execution_id)
            plan = session.get(Plan, execution.plan_id)
            incident = session.get(Incident, plan.incident_id)
            execution.verification_result = verification
            if verification.get("healthy"):
                validate_transition(incident.status, IncidentStatus.RESOLVED)
                incident.status = IncidentStatus.RESOLVED
                incident.resolved_at = utc_now()
                self._audit(
                    session,
                    "Execution",
                    execution.id,
                    "execution.verified",
                    actor_type="AGENT",
                    actor_id="verification-worker",
                    payload=verification,
                )
                REMEDIATION_EXECUTIONS.labels(
                    action_type, "SUCCEEDED"
                ).inc()
            else:
                validate_transition(
                    execution.status, ExecutionStatus.VERIFICATION_FAILED
                )
                execution.status = ExecutionStatus.VERIFICATION_FAILED
                validate_transition(incident.status, IncidentStatus.FAILED)
                incident.status = IncidentStatus.FAILED
                session.add(
                    Task(
                        incident_id=incident.id,
                        title="自动修复后验证失败，需要人工复核",
                        task_type=TaskType.VERIFICATION,
                        priority=Priority.CRITICAL,
                        created_by="verification-worker",
                        related_entity_type="Execution",
                        related_entity_id=execution.id,
                    )
                )
                self._audit(
                    session,
                    "Execution",
                    execution.id,
                    "execution.verification_failed",
                    actor_type="AGENT",
                    actor_id="verification-worker",
                    payload=verification,
                )
                REMEDIATION_EXECUTIONS.labels(
                    action_type, "VERIFICATION_FAILED"
                ).inc()
            return execution

    def fail_execution(self, execution_id: str, error: str) -> Execution:
        """Persist an unexpected worker failure without retrying the action."""
        with self.session_factory.begin() as session:
            execution = session.get(Execution, execution_id)
            if not execution:
                raise NotFoundError("execution not found")
            if execution.status == ExecutionStatus.SUCCEEDED:
                validate_transition(
                    execution.status, ExecutionStatus.VERIFICATION_FAILED
                )
                execution.status = ExecutionStatus.VERIFICATION_FAILED
            elif execution.status not in {
                ExecutionStatus.FAILED,
                ExecutionStatus.VERIFICATION_FAILED,
                ExecutionStatus.ROLLED_BACK,
            }:
                validate_transition(execution.status, ExecutionStatus.FAILED)
                execution.status = ExecutionStatus.FAILED
            execution.error = safe_error_message(error)
            execution.finished_at = utc_now()
            plan = session.get(Plan, execution.plan_id)
            incident = session.get(Incident, plan.incident_id) if plan else None
            if incident and incident.status in {
                IncidentStatus.REMEDIATING,
                IncidentStatus.VERIFYING,
            }:
                incident.status = IncidentStatus.FAILED
            existing_task = session.scalar(
                select(Task).where(
                    Task.related_entity_type == "Execution",
                    Task.related_entity_id == execution.id,
                    Task.status.in_(
                        [TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED]
                    ),
                )
            )
            if incident and not existing_task:
                session.add(
                    Task(
                        incident_id=incident.id,
                        title="人工确认异常执行结果",
                        description=(
                            "Worker 异常退出，系统不会自动重试集群变更。"
                        ),
                        task_type=TaskType.VERIFICATION,
                        priority=Priority.CRITICAL,
                        created_by="execution-worker",
                        related_entity_type="Execution",
                        related_entity_id=execution.id,
                    )
                )
            self._audit(
                session,
                "Execution",
                execution.id,
                "execution.worker_failed",
                actor_type="AGENT",
                actor_id="execution-worker",
                payload={"error": execution.error},
            )
            return execution

    def get_plan(self, plan_id: str) -> Plan:
        with self.session_factory() as session:
            plan = session.get(Plan, plan_id)
            if not plan:
                raise NotFoundError("plan not found")
            return plan

    def list_plans(self, incident_id: str | None = None) -> list[Plan]:
        with self.session_factory() as session:
            statement = select(Plan)
            if incident_id:
                statement = statement.where(Plan.incident_id == incident_id)
            return list(
                session.scalars(
                    statement.order_by(Plan.created_at.desc())
                ).all()
            )

    def get_execution(self, execution_id: str) -> Execution:
        with self.session_factory() as session:
            execution = session.get(Execution, execution_id)
            if not execution:
                raise NotFoundError("execution not found")
            return execution
