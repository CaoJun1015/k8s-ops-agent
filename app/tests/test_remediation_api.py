"""受控 Pod 重建流程测试。

场景覆盖：
- Action Catalog 之外的动作被拒绝；
- dry-run 仅允许控制器管理的异常 Pod；
- 未审批计划不能执行；
- 审批、执行和结果验证形成完整审计链；
- 健康 Pod 不允许进入执行阶段。
"""

from unittest.mock import MagicMock

import pytest

from app import create_app


@pytest.fixture()
def kubernetes_adapter():
    adapter = MagicMock()
    adapter.inspect_pod.return_value = {
        "exists": True,
        "uid": "pod-uid-1",
        "owner_kind": "ReplicaSet",
        "workload_kind": "Deployment",
        "workload_name": "demo",
        "phase": "Running",
        "is_abnormal": True,
        "reason": "CrashLoopBackOff",
    }
    adapter.delete_pod.return_value = {
        "deleted": True,
        "uid": "pod-uid-1",
    }
    adapter.verify_recovery.return_value = {
        "healthy": True,
        "ready_replicas": 1,
        "expected_replicas": 1,
    }
    return adapter


@pytest.fixture()
def client(kubernetes_adapter):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "EXECUTION_MODE": "inline",
            "KUBERNETES_ADAPTER": kubernetes_adapter,
            "AUTO_REMEDIATE_ENABLED": False,
            "ALLOWED_NAMESPACES": {"default"},
        }
    )
    with application.test_client() as test_client:
        yield test_client


def diagnosed_incident(client):
    incident = client.post(
        "/api/incidents",
        json={
            "title": "Pod 持续重启",
            "severity": "HIGH",
            "fingerprint": "default/pod/demo-123:CrashLoopBackOff",
            "namespace": "default",
            "resource_kind": "Pod",
            "resource_name": "demo-123",
            "source": "test",
            "summary": "CrashLoopBackOff",
        },
    ).get_json()
    client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"mode": "DIAGNOSE_ONLY"},
    )
    return incident


def create_plan(client, incident_id):
    response = client.post(
        f"/api/incidents/{incident_id}/plans",
        json={
            "action_type": "RESTART_CONTROLLER_MANAGED_POD",
            "target": {
                "namespace": "default",
                "pod_name": "demo-123",
                "pod_uid": "pod-uid-1",
            },
        },
    )
    assert response.status_code == 201
    return response.get_json()


def test_unknown_action_is_rejected(client):
    """LLM 或用户不能提交任意 Shell 命令作为修复动作。"""
    incident = diagnosed_incident(client)
    response = client.post(
        f"/api/incidents/{incident['id']}/plans",
        json={"action_type": "RUN_SHELL", "target": {"command": "anything"}},
    )
    assert response.status_code == 400


def test_safe_dry_run_creates_approval_task(client):
    """安全检查通过后仍需生成人工审批任务。"""
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])

    response = client.post(f"/api/plans/{plan['id']}/dry-run")

    assert response.status_code == 200
    dry_run_plan = response.get_json()
    assert dry_run_plan["status"] == "AWAITING_APPROVAL"
    assert dry_run_plan["dry_run_result"]["allowed"] is True

    tasks = client.get(f"/api/tasks?incident_id={incident['id']}").get_json()
    assert any(
        task["task_type"] == "APPROVAL" and task["approval_required"]
        for task in tasks
    )


def test_api_can_capture_target_uid_before_plan_creation(
    client, kubernetes_adapter
):
    """控制台创建计划时可由只读适配器捕获当前 Pod UID，避免要求用户手填。"""
    incident = diagnosed_incident(client)
    response = client.post(
        f"/api/incidents/{incident['id']}/plans",
        json={
            "action_type": "RESTART_CONTROLLER_MANAGED_POD",
            "target": {
                "namespace": "default",
                "pod_name": "demo-123",
            },
        },
    )

    assert response.status_code == 201
    assert response.get_json()["target"]["pod_uid"] == "pod-uid-1"


def test_unapproved_plan_cannot_execute(client):
    """执行端点不能绕过 dry-run 和人工审批。"""
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])
    client.post(f"/api/plans/{plan['id']}/dry-run")

    response = client.post(
        f"/api/plans/{plan['id']}/execute",
        headers={"Idempotency-Key": "execute-once", "X-Actor-ID": "operator"},
    )
    assert response.status_code == 409


def test_approved_plan_executes_verifies_and_audits(
    client, kubernetes_adapter
):
    """批准后的低风险动作应执行一次，并在验证成功后关闭事件。"""
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])
    client.post(f"/api/plans/{plan['id']}/dry-run")

    approval = client.post(
        f"/api/plans/{plan['id']}/approve",
        json={"comment": "批准重建异常 Pod"},
        headers={"X-Actor-ID": "oncall-user"},
    )
    assert approval.status_code == 200
    assert approval.get_json()["status"] == "EXECUTABLE"

    execution_response = client.post(
        f"/api/plans/{plan['id']}/execute",
        headers={
            "Idempotency-Key": "restart-demo-pod-once",
            "X-Actor-ID": "oncall-user",
        },
    )
    assert execution_response.status_code == 202
    execution = client.get(execution_response.get_json()["location"]).get_json()
    assert execution["status"] == "SUCCEEDED"
    assert execution["verification_result"]["healthy"] is True
    kubernetes_adapter.delete_pod.assert_called_once_with(
        namespace="default",
        pod_name="demo-123",
        expected_uid="pod-uid-1",
    )

    updated_incident = client.get(f"/api/incidents/{incident['id']}").get_json()
    assert updated_incident["status"] == "RESOLVED"

    audits = client.get(
        f"/api/audit-events?entity_type=Execution&entity_id={execution['id']}"
    ).get_json()
    assert {event["event_type"] for event in audits} >= {
        "execution.created",
        "execution.started",
        "execution.succeeded",
        "execution.verified",
    }


def test_healthy_pod_is_rejected_by_dry_run(client, kubernetes_adapter):
    """即使动作类型在白名单中，也不能重建健康 Pod。"""
    kubernetes_adapter.inspect_pod.return_value["is_abnormal"] = False
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])

    response = client.post(f"/api/plans/{plan['id']}/dry-run")
    assert response.status_code == 409
    assert response.get_json()["error"]["code"] == "policy_denied"


def test_rejected_plan_returns_incident_to_diagnosed(client):
    """人工拒绝计划后，事件应回到已诊断状态以便制定新方案。"""
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])
    client.post(f"/api/plans/{plan['id']}/dry-run")

    response = client.post(
        f"/api/plans/{plan['id']}/reject",
        json={"comment": "当前变更窗口不允许重建"},
        headers={"X-Actor-ID": "oncall-user"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "REJECTED"
    updated = client.get(f"/api/incidents/{incident['id']}").get_json()
    assert updated["status"] == "DIAGNOSED"


def test_execution_error_is_persisted_and_creates_manual_task(
    client, kubernetes_adapter
):
    """集群调用失败不能回滚掉执行事实，必须留下失败审计和人工任务。"""
    kubernetes_adapter.delete_pod.side_effect = RuntimeError("API timeout")
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])
    client.post(f"/api/plans/{plan['id']}/dry-run")
    client.post(
        f"/api/plans/{plan['id']}/approve",
        headers={"X-Actor-ID": "oncall-user"},
    )

    response = client.post(
        f"/api/plans/{plan['id']}/execute",
        headers={
            "Idempotency-Key": "failed-execution-once",
            "X-Actor-ID": "oncall-user",
        },
    )

    assert response.status_code == 202
    execution = client.get(response.get_json()["location"]).get_json()
    assert execution["status"] == "FAILED"
    assert "API timeout" in execution["error"]
    tasks = client.get(f"/api/tasks?incident_id={incident['id']}").get_json()
    assert any(task["task_type"] == "FOLLOW_UP" for task in tasks)


def test_operator_token_is_required_when_auth_is_enabled(kubernetes_adapter):
    """生产审批接口不能只信任可伪造的操作者请求头。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "EXECUTION_MODE": "inline",
            "KUBERNETES_ADAPTER": kubernetes_adapter,
            "ALLOWED_NAMESPACES": {"default"},
            "REQUIRE_OPERATOR_AUTH": True,
            "OPERATOR_API_TOKEN": "test-operator-token",
        }
    )
    client = application.test_client()
    incident = diagnosed_incident(client)
    plan = create_plan(client, incident["id"])
    client.post(f"/api/plans/{plan['id']}/dry-run")

    unauthorized = client.post(f"/api/plans/{plan['id']}/approve")
    authorized = client.post(
        f"/api/plans/{plan['id']}/approve",
        headers={
            "Authorization": "Bearer test-operator-token",
            "X-Actor-ID": "oncall-user",
        },
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 200
