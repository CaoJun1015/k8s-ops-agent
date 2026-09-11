"""Recover work that can no longer be owned by a live worker."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ops_agent.domain import (
    AgentRunStatus,
    ExecutionStatus,
    IncidentStatus,
    Priority,
    TaskType,
)
from ops_agent.models import AgentRun, AuditEvent, Execution, Incident, Plan, Task


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _audit(session, entity_type, entity_id, event_type, payload) -> None:
    session.add(
        AuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            event_type=event_type,
            actor_type="SYSTEM",
            actor_id="timeout-recovery",
            payload=payload,
        )
    )


def recover_stuck_work_once(
    session_factory,
    *,
    now: datetime | None = None,
    agent_timeout_seconds: int = 300,
    execution_timeout_seconds: int = 300,
) -> dict[str, int]:
    now = now or utc_now()
    result = {"agent_runs_failed": 0, "executions_failed": 0}
    agent_cutoff = now - timedelta(seconds=agent_timeout_seconds)
    execution_cutoff = now - timedelta(seconds=execution_timeout_seconds)

    with session_factory.begin() as session:
        runs = list(
            session.scalars(
                select(AgentRun)
                .where(
                    AgentRun.status.in_(
                        [
                            AgentRunStatus.RUNNING,
                            AgentRunStatus.COLLECTING,
                            AgentRunStatus.DIAGNOSING,
                        ]
                    ),
                    AgentRun.started_at.is_not(None),
                    AgentRun.started_at <= agent_cutoff,
                )
                .with_for_update(skip_locked=True)
            ).all()
        )
        for run in runs:
            is_agent_core = run.status == AgentRunStatus.RUNNING
            run.status = (
                AgentRunStatus.STOPPED if is_agent_core else AgentRunStatus.FAILED
            )
            run.stop_reason = "WORKER_INTERRUPTED" if is_agent_core else "RUN_TIMEOUT"
            run.error = "agent worker lease or configured timeout expired"
            run.finished_at = now
            run.lease_owner = None
            run.lease_expires_at = None
            incident = session.get(Incident, run.incident_id)
            if incident and incident.status == IncidentStatus.DIAGNOSING:
                incident.status = IncidentStatus.FAILED
            _audit(
                session,
                "AgentRun",
                run.id,
                "agent_run.timed_out",
                {"timeout_seconds": agent_timeout_seconds},
            )
            if is_agent_core:
                session.add(
                    Task(
                        incident_id=incident.id,
                        title="Agent 中断后需要人工确认",
                        description="只读调查 Worker 中断，系统未自动重放不确定步骤。",
                        task_type=TaskType.MANUAL_CHECK,
                        priority=Priority.HIGH,
                        created_by="timeout-recovery",
                        related_entity_type="AgentRun",
                        related_entity_id=run.id,
                    )
                )
            result["agent_runs_failed"] += 1

        executions = list(
            session.scalars(
                select(Execution)
                .where(
                    Execution.status == ExecutionStatus.RUNNING,
                    Execution.started_at.is_not(None),
                    Execution.started_at <= execution_cutoff,
                )
                .with_for_update(skip_locked=True)
            ).all()
        )
        for execution in executions:
            execution.status = ExecutionStatus.FAILED
            execution.error = "execution exceeded configured timeout"
            execution.finished_at = now
            plan = session.get(Plan, execution.plan_id)
            incident = session.get(Incident, plan.incident_id) if plan else None
            if incident and incident.status in {
                IncidentStatus.REMEDIATING,
                IncidentStatus.VERIFYING,
            }:
                incident.status = IncidentStatus.FAILED
            if incident:
                task = Task(
                    incident_id=incident.id,
                    title="人工确认超时执行结果",
                    description=(
                        "执行已超时，系统不会自动重试。请确认集群实际状态。"
                    ),
                    task_type=TaskType.VERIFICATION,
                    priority=Priority.CRITICAL,
                    created_by="timeout-recovery",
                    related_entity_type="Execution",
                    related_entity_id=execution.id,
                )
                session.add(task)
            _audit(
                session,
                "Execution",
                execution.id,
                "execution.timed_out",
                {"timeout_seconds": execution_timeout_seconds},
            )
            result["executions_failed"] += 1
    return result
