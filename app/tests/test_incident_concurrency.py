"""Incident 数据库并发约束与乐观锁测试。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm.exc import StaleDataError

from ops_agent.database import Database
from ops_agent.domain import PlanStatus
from ops_agent.models import AgentRun, Execution, Incident, OutboxEvent, Plan
from ops_agent.services import OpsService


def incident_data():
    return {
        "title": "Pod crash looping",
        "severity": "HIGH",
        "fingerprint": "concurrent-fingerprint",
        "namespace": "default",
        "resource_kind": "Pod",
        "resource_name": "demo-1",
        "source": "test",
    }


def test_concurrent_alerts_create_one_active_incident(tmp_path):
    """并发相同 fingerprint 最终只能有一条活动事件且次数不丢失。"""
    database_path = (tmp_path / "concurrency.db").as_posix()
    database = Database(f"sqlite+pysqlite:///{database_path}")
    database.create_schema()
    service = OpsService(database.session_factory)
    workers = 6
    barrier = Barrier(workers)

    def create_once():
        barrier.wait()
        incident, _ = service.create_incident(incident_data())
        return incident.id

    with ThreadPoolExecutor(max_workers=workers) as executor:
        ids = list(executor.map(lambda _index: create_once(), range(workers)))

    assert len(set(ids)) == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Incident)) == 1
        incident = session.scalar(select(Incident))
        assert incident.occurrence_count == workers


def test_incident_version_detects_stale_writes(tmp_path):
    """两个操作者更新同一 Incident 时，过期写入必须被拒绝。"""
    database_path = (tmp_path / "optimistic-lock.db").as_posix()
    database = Database(f"sqlite+pysqlite:///{database_path}")
    database.create_schema()
    incident, _ = OpsService(database.session_factory).create_incident(
        incident_data()
    )

    first = database.session_factory()
    second = database.session_factory()
    try:
        first_copy = first.get(Incident, incident.id)
        second_copy = second.get(Incident, incident.id)
        first_copy.summary = "first update"
        first.commit()
        second_copy.summary = "stale update"
        with pytest.raises(StaleDataError):
            second.commit()
    finally:
        first.close()
        second.close()


def test_concurrent_idempotent_agent_runs_create_one_outbox_event(tmp_path):
    """AgentRun 的数据库唯一约束冲突应返回赢家，而不是向调用方报 500。"""
    database_path = (tmp_path / "run-idempotency.db").as_posix()
    database = Database(f"sqlite+pysqlite:///{database_path}")
    database.create_schema()
    service = OpsService(database.session_factory)
    incident, _ = service.create_incident(incident_data())
    workers = 4
    barrier = Barrier(workers)

    def create_once():
        barrier.wait()
        run, _ = service.create_agent_run(
            incident.id,
            "DIAGNOSE_ONLY",
            "same-run-request",
            enqueue=True,
        )
        return run.id

    with ThreadPoolExecutor(max_workers=workers) as executor:
        ids = list(executor.map(lambda _index: create_once(), range(workers)))

    assert len(set(ids)) == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_concurrent_idempotent_executions_create_one_outbox_event(tmp_path):
    """Execution 重复请求必须共用一个执行事实和一个待投递事件。"""
    database_path = (tmp_path / "execution-idempotency.db").as_posix()
    database = Database(f"sqlite+pysqlite:///{database_path}")
    database.create_schema()
    service = OpsService(database.session_factory)
    incident, _ = service.create_incident(incident_data())
    with database.session_factory.begin() as session:
        plan = Plan(
            incident_id=incident.id,
            action_type="RESTART_CONTROLLER_MANAGED_POD",
            target={},
            status=PlanStatus.EXECUTABLE,
        )
        session.add(plan)
        session.flush()
        plan_id = plan.id
    workers = 4
    barrier = Barrier(workers)

    def create_once():
        barrier.wait()
        execution, _ = service.create_execution(
            plan_id,
            "same-execution-request",
            "operator",
            enqueue=True,
        )
        return execution.id

    with ThreadPoolExecutor(max_workers=workers) as executor:
        ids = list(executor.map(lambda _index: create_once(), range(workers)))

    assert len(set(ids)) == 1
    with database.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Execution)) == 1
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1
