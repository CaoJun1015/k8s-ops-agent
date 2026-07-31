"""Incident 与 Action Task API 测试。

场景覆盖：
- 创建 Incident 后可以创建与其关联的 Action Task；
- Task 缺少 incident_id 时拒绝创建；
- 引用不存在的 Incident 时拒绝创建；
- 任务状态只能按状态机转换；
- 相同 fingerprint 的活动告警保持幂等。
"""

import pytest

from app import create_app


@pytest.fixture()
def client():
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
        }
    )
    with application.test_client() as test_client:
        yield test_client


def create_incident(client, fingerprint="default/pod/demo:CrashLoopBackOff"):
    response = client.post(
        "/api/incidents",
        json={
            "title": "demo Pod 持续重启",
            "severity": "HIGH",
            "fingerprint": fingerprint,
            "cluster": "local",
            "namespace": "default",
            "resource_kind": "Pod",
            "resource_name": "demo-123",
            "source": "test",
        },
    )
    assert response.status_code == 201
    return response.get_json()


def test_action_task_is_created_under_incident(client):
    """运维行动项必须可以追溯到所属 Incident。"""
    incident = create_incident(client)
    response = client.post(
        "/api/tasks",
        json={
            "incident_id": incident["id"],
            "title": "检查容器退出码",
            "task_type": "MANUAL_CHECK",
            "priority": "HIGH",
        },
    )

    assert response.status_code == 201
    task = response.get_json()
    assert task["incident_id"] == incident["id"]
    assert task["status"] == "TODO"


def test_task_without_incident_is_rejected(client):
    """禁止创建无归属的通用 Todo。"""
    response = client.post("/api/tasks", json={"title": "孤立任务"})
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "validation_error"


def test_task_for_unknown_incident_is_rejected(client):
    """引用不存在的 Incident 时不能产生悬空任务。"""
    response = client.post(
        "/api/tasks",
        json={"incident_id": "missing", "title": "悬空任务"},
    )
    assert response.status_code == 404


def test_task_rejects_invalid_state_jump(client):
    """TODO 不能绕过处理中状态直接完成。"""
    incident = create_incident(client)
    task = client.post(
        "/api/tasks",
        json={"incident_id": incident["id"], "title": "检查日志"},
    ).get_json()

    response = client.patch(
        f"/api/tasks/{task['id']}",
        json={"status": "DONE"},
    )
    assert response.status_code == 409


def test_active_incident_fingerprint_is_idempotent(client):
    """重复告警复用活动 Incident，而不是制造告警风暴。"""
    first = create_incident(client)
    second_response = client.post(
        "/api/incidents",
        json={
            "title": "同一个 Pod 再次告警",
            "severity": "HIGH",
            "fingerprint": "default/pod/demo:CrashLoopBackOff",
            "namespace": "default",
            "resource_kind": "Pod",
            "resource_name": "demo-123",
            "source": "test",
        },
    )

    assert second_response.status_code == 200
    assert second_response.get_json()["id"] == first["id"]

