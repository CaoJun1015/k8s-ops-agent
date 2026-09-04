"""事务 Outbox 的创建、投递和失败恢复测试。"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app import create_app
from ops_agent.domain import OutboxStatus
from ops_agent.models import OutboxEvent
from ops_agent.outbox import dispatch_pending_once


def create_incident(client):
    return client.post(
        "/api/incidents",
        json={
            "title": "异步诊断",
            "fingerprint": "outbox-incident",
            "namespace": "default",
            "resource_kind": "Deployment",
            "resource_name": "api",
            "source": "test",
        },
    ).get_json()


def test_async_agent_run_and_outbox_are_committed_together():
    """API 返回成功时，Run 与待投递事件必须已在同一数据库中。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "rq",
        }
    )
    client = application.test_client()
    incident = create_incident(client)

    response = client.post(f"/api/incidents/{incident['id']}/agent-runs")
    run_id = response.get_json()["id"]

    with application.extensions["database"].session_factory() as session:
        event = session.scalar(select(OutboxEvent))
        assert event.status == OutboxStatus.PENDING
        assert event.topic == "agent_run.requested"
        assert event.aggregate_id == run_id
        assert event.payload == {"run_id": run_id}


def test_dispatcher_publishes_with_outbox_id_as_queue_job_id():
    """重复扫描 Outbox 时必须复用稳定 job_id，避免重复业务作业。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "rq",
        }
    )
    client = application.test_client()
    incident = create_incident(client)
    run_id = client.post(
        f"/api/incidents/{incident['id']}/agent-runs"
    ).get_json()["id"]
    redis_connection = MagicMock()

    with application.extensions["database"].session_factory() as session:
        event_id = session.scalar(select(OutboxEvent.id))

    with patch("ops_agent.outbox.enqueue_diagnosis") as enqueue:
        result = dispatch_pending_once(
            application.extensions["database"].session_factory,
            redis_connection,
            application.config["DATABASE_URL"],
        )

    assert result == {"published": 1, "failed": 0}
    enqueue.assert_called_once_with(
        redis_connection,
        application.config["DATABASE_URL"],
        run_id,
        job_id=event_id,
    )
    with application.extensions["database"].session_factory() as session:
        event = session.get(OutboxEvent, event_id)
        assert event.status == OutboxStatus.PUBLISHED
        assert event.published_at is not None
def test_dispatch_failure_is_redacted_and_remains_retryable():
    """临时队列故障不得丢消息，错误中也不能保留凭据。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "rq",
        }
    )
    client = application.test_client()
    incident = create_incident(client)
    client.post(f"/api/incidents/{incident['id']}/agent-runs")

    with patch(
        "ops_agent.outbox.enqueue_diagnosis",
        side_effect=RuntimeError("password=queue-secret connection failed"),
    ):
        result = dispatch_pending_once(
            application.extensions["database"].session_factory,
            MagicMock(),
            application.config["DATABASE_URL"],
            now=datetime.now(timezone.utc) + timedelta(seconds=1),
        )

    assert result == {"published": 0, "failed": 1}
    with application.extensions["database"].session_factory() as session:
        event = session.scalar(select(OutboxEvent))
        assert event.status == OutboxStatus.PENDING
        assert event.attempts == 1
        assert "queue-secret" not in event.last_error
        assert "[REDACTED]" in event.last_error
        available_at = event.available_at
        if available_at.tzinfo is None:
            available_at = available_at.replace(tzinfo=timezone.utc)
        assert available_at > datetime.now(timezone.utc)
