"""超时恢复与 Worker 异常落库测试。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app import create_app
from ops_agent.domain import (
    AgentRunStatus,
    ExecutionStatus,
    IncidentStatus,
    PlanStatus,
)
from ops_agent.models import AgentRun, AuditEvent, Execution, Incident, Plan, Task
from ops_agent.recovery import recover_stuck_work_once


def test_recovery_fails_stuck_run_and_execution_without_reexecuting():
    """超时作业只能失败并转人工，不能自动重放集群变更。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
        }
    )
    session_factory = application.extensions["database"].session_factory
    old = datetime.now(timezone.utc) - timedelta(hours=1)
    with session_factory.begin() as session:
        diagnosing_incident = Incident(
            title="stuck diagnosis",
            fingerprint="stuck-diagnosis",
            namespace="default",
            resource_kind="Pod",
            resource_name="api-1",
            source="test",
            status=IncidentStatus.DIAGNOSING,
        )
        remediating_incident = Incident(
            title="stuck execution",
            fingerprint="stuck-execution",
            namespace="default",
            resource_kind="Pod",
            resource_name="api-2",
            source="test",
            status=IncidentStatus.REMEDIATING,
        )
        session.add_all([diagnosing_incident, remediating_incident])
        session.flush()
        run = AgentRun(
            incident_id=diagnosing_incident.id,
            status=AgentRunStatus.COLLECTING,
            started_at=old,
        )
        plan = Plan(
            incident_id=remediating_incident.id,
            action_type="RESTART_CONTROLLER_MANAGED_POD",
            status=PlanStatus.EXECUTABLE,
            target={},
        )
        session.add_all([run, plan])
        session.flush()
        execution = Execution(
            plan_id=plan.id,
            status=ExecutionStatus.RUNNING,
            idempotency_key="stuck-execution-once",
            requested_by="operator",
            started_at=old,
        )
        session.add(execution)
        session.flush()
        run_id = run.id
        execution_id = execution.id

    result = recover_stuck_work_once(
        session_factory,
        now=datetime.now(timezone.utc),
        agent_timeout_seconds=300,
        execution_timeout_seconds=300,
    )

    assert result == {"agent_runs_failed": 1, "executions_failed": 1}
    with session_factory() as session:
        assert session.get(AgentRun, run_id).status == AgentRunStatus.FAILED
        assert (
            session.get(Execution, execution_id).status
            == ExecutionStatus.FAILED
        )
        task = session.scalar(
            select(Task).where(Task.related_entity_id == execution_id)
        )
        assert task is not None
        assert task.task_type.value == "VERIFICATION"
        events = set(session.scalars(select(AuditEvent.event_type)).all())
        assert "agent_run.timed_out" in events
        assert "execution.timed_out" in events


@patch("ops_agent.jobs.OpsService")
@patch("ops_agent.jobs.Database")
@patch("ops_agent.jobs.KubernetesAdapter")
def test_execution_worker_persists_unhandled_failure(
    _adapter_class, database_class, service_class
):
    """Worker 入口遇到未处理异常时必须调用失败落库，再向队列抛错。"""
    service = MagicMock()
    service.process_execution.side_effect = RuntimeError("connection reset")
    service_class.return_value = service
    database_class.return_value.session_factory = MagicMock()

    from ops_agent.jobs import process_execution_job

    try:
        process_execution_job("postgresql://db", "execution-1")
    except RuntimeError:
        pass
    else:
        raise AssertionError("worker should re-raise the original error")

    service.fail_execution.assert_called_once_with(
        "execution-1", "connection reset"
    )
