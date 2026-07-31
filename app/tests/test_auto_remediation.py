"""有限自动修复策略测试。"""

from unittest.mock import MagicMock

from app import create_app


def alert_payload():
    return {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "auto-remediate-alert",
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "severity": "warning",
                    "namespace": "default",
                    "pod": "demo-123",
                },
                "annotations": {"summary": "Pod crash looping"},
            }
        ],
    }


def adapter(abnormal=True):
    kubernetes_adapter = MagicMock()
    kubernetes_adapter.inspect_pod.return_value = {
        "exists": True,
        "uid": "uid-auto-1",
        "owner_kind": "ReplicaSet",
        "workload_kind": "Deployment",
        "workload_name": "demo",
        "phase": "Running",
        "is_abnormal": abnormal,
        "reason": "CrashLoopBackOff" if abnormal else None,
    }
    kubernetes_adapter.delete_pod.return_value = {
        "deleted": True,
        "uid": "uid-auto-1",
    }
    kubernetes_adapter.verify_recovery.return_value = {
        "healthy": True,
        "ready_replicas": 1,
        "expected_replicas": 1,
    }
    return kubernetes_adapter


def make_client(auto_enabled, kubernetes_adapter):
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "EXECUTION_MODE": "inline",
            "AUTO_REMEDIATE_ENABLED": auto_enabled,
            "ALLOWED_NAMESPACES": {"default"},
            "KUBERNETES_ADAPTER": kubernetes_adapter,
        }
    )
    return application.test_client()


def test_auto_remediation_is_disabled_by_default():
    """功能开关关闭时，告警只能触发诊断，不能产生 Execution。"""
    client = make_client(False, adapter())
    client.post("/api/alerts/prometheus", json=alert_payload())

    assert client.get("/api/executions").get_json() == []


def test_enabled_policy_restarts_only_abnormal_controller_managed_pod():
    """显式开启后，符合全部策略的异常 Pod 可以无人值守重建。"""
    kubernetes_adapter = adapter()
    client = make_client(True, kubernetes_adapter)
    client.post("/api/alerts/prometheus", json=alert_payload())

    executions = client.get("/api/executions").get_json()
    assert len(executions) == 1
    assert executions[0]["status"] == "SUCCEEDED"
    assert executions[0]["requested_by"] == "policy-engine"
    kubernetes_adapter.delete_pod.assert_called_once()


def test_enabled_policy_still_refuses_healthy_pod():
    """功能开关不能绕过健康状态检查。"""
    kubernetes_adapter = adapter(abnormal=False)
    client = make_client(True, kubernetes_adapter)
    client.post("/api/alerts/prometheus", json=alert_payload())

    assert client.get("/api/executions").get_json() == []
    kubernetes_adapter.delete_pod.assert_not_called()
