"""Prometheus 指标与 Alertmanager 入站测试。"""

import pytest
from unittest.mock import patch

from app import create_app
from ops_agent.metrics import render_metrics


class VerifiableKubernetesAdapter:
    def __init__(self, healthy=True):
        self.healthy = healthy

    def collect_pod_evidence(self, **_kwargs):
        return {
            "pod_status": {
                "exists": True,
                "phase": "Running",
                "ready": self.healthy,
                "containers": [],
            }
        }

    def inspect_pod(self, **_kwargs):
        return {
            "exists": True,
            "ready": self.healthy,
            "is_abnormal": not self.healthy,
            "phase": "Running" if self.healthy else "Pending",
        }


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


def test_metrics_endpoint_exposes_http_and_agent_metrics(client):
    """Prometheus 必须能抓取应用自身的标准文本指标。"""
    client.get("/live")
    response = client.get("/metrics")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert "ops_agent_info" in body
    assert "ops_agent_http_requests_total" in body


def test_metrics_renderer_uses_multiprocess_collector_when_configured(
    monkeypatch,
):
    """Gunicorn 多 worker 模式必须聚合所有 worker 的指标文件。"""
    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", "C:/tmp/prometheus-test")
    with patch("ops_agent.metrics.multiprocess.MultiProcessCollector") as collector:
        render_metrics()

    collector.assert_called_once()


def test_firing_alert_creates_incident_and_diagnostic_run(client):
    """Alertmanager firing 告警应生成 Incident 并自动启动只诊断 Run。"""
    response = client.post(
        "/api/alerts/prometheus",
        json={
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "alert-fingerprint-1",
                    "labels": {
                        "alertname": "KubePodCrashLooping",
                        "severity": "warning",
                        "cluster": "local",
                        "namespace": "default",
                        "pod": "demo-123",
                    },
                    "annotations": {
                        "summary": "Pod is crash looping",
                        "description": "demo-123 restarted repeatedly",
                    },
                }
            ],
        },
    )

    assert response.status_code == 202
    assert response.get_json()["accepted"] == 1
    incidents = client.get("/api/incidents").get_json()
    assert len(incidents) == 1
    assert incidents[0]["status"] == "DIAGNOSED"


def test_duplicate_alert_reuses_active_incident(client):
    """同一 fingerprint 的重复通知不能创建重复 Incident。"""
    payload = {
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "same-alert",
                "labels": {
                    "alertname": "PodNotReady",
                    "severity": "critical",
                    "namespace": "default",
                    "pod": "demo-456",
                },
                "annotations": {"summary": "Pod not ready"},
            }
        ],
    }
    client.post("/api/alerts/prometheus", json=payload)
    client.post("/api/alerts/prometheus", json=payload)

    incidents = client.get("/api/incidents").get_json()
    assert len(incidents) == 1
    assert incidents[0]["occurrence_count"] == 2


def test_alert_webhook_accepts_only_configured_bearer_token():
    """公网可达的告警入口不能被任意请求伪造 Incident。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "ALERT_WEBHOOK_TOKEN": "alert-secret",
        }
    )
    test_client = application.test_client()
    payload = {"status": "firing", "alerts": []}

    unauthorized = test_client.post("/api/alerts/prometheus", json=payload)
    authorized = test_client.post(
        "/api/alerts/prometheus",
        json=payload,
        headers={"Authorization": "Bearer alert-secret"},
    )

    assert unauthorized.status_code == 401
    assert authorized.status_code == 202


def alert_payload(status="firing", fingerprint="resolved-alert"):
    return {
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "labels": {
                    "alertname": "PodNotReady",
                    "severity": "warning",
                    "namespace": "default",
                    "pod": "demo-789",
                },
                "annotations": {"summary": "Pod not ready"},
            }
        ],
    }


def test_resolved_alert_requires_and_passes_resource_verification():
    """恢复通知必须读取真实资源状态，健康后才关闭 Incident。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "KUBERNETES_ADAPTER": VerifiableKubernetesAdapter(healthy=True),
        }
    )
    test_client = application.test_client()
    test_client.post("/api/alerts/prometheus", json=alert_payload())

    response = test_client.post(
        "/api/alerts/prometheus", json=alert_payload("resolved")
    )
    incident = test_client.get("/api/incidents").get_json()[0]

    assert response.status_code == 202
    assert response.get_json()["resolved"] == 1
    assert incident["status"] == "RESOLVED"
    assert incident["resolved_at"] is not None


def test_failed_resolved_verification_creates_action_task():
    """资源仍异常时不得关闭事件，并要留下人工验证任务。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "KUBERNETES_ADAPTER": VerifiableKubernetesAdapter(healthy=False),
        }
    )
    test_client = application.test_client()
    test_client.post(
        "/api/alerts/prometheus",
        json=alert_payload(fingerprint="verification-failed"),
    )

    response = test_client.post(
        "/api/alerts/prometheus",
        json=alert_payload("resolved", "verification-failed"),
    )
    incident = test_client.get("/api/incidents").get_json()[0]
    tasks = test_client.get(
        f"/api/tasks?incident_id={incident['id']}"
    ).get_json()

    assert response.get_json()["verification_failed"] == 1
    assert incident["status"] == "FAILED"
    assert len(tasks) == 1
    assert tasks[0]["task_type"] == "VERIFICATION"
    assert tasks[0]["related_entity_type"] == "Incident"


def test_repeated_failed_verification_deduplicates_task_and_later_closes_it():
    """重复 resolved 通知不能制造任务风暴，后续验证成功应完成原任务。"""
    adapter = VerifiableKubernetesAdapter(healthy=False)
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "KUBERNETES_ADAPTER": adapter,
        }
    )
    test_client = application.test_client()
    firing = alert_payload("firing", "repeated-resolved")
    resolved = alert_payload("resolved", "repeated-resolved")
    test_client.post("/api/alerts/prometheus", json=firing)
    test_client.post("/api/alerts/prometheus", json=resolved)
    test_client.post("/api/alerts/prometheus", json=resolved)
    incident = test_client.get("/api/incidents").get_json()[0]

    tasks = test_client.get(
        f"/api/tasks?incident_id={incident['id']}"
    ).get_json()
    assert len(tasks) == 1

    adapter.healthy = True
    result = test_client.post("/api/alerts/prometheus", json=resolved)
    tasks = test_client.get(
        f"/api/tasks?incident_id={incident['id']}"
    ).get_json()

    assert result.get_json()["resolved"] == 1
    assert tasks[0]["status"] == "DONE"


def test_resolved_unknown_fingerprint_is_ignored():
    """找不到活动事件的恢复通知应安全忽略且不新建 Incident。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
        }
    )
    test_client = application.test_client()

    result = test_client.post(
        "/api/alerts/prometheus", json=alert_payload("resolved", "missing")
    )

    assert result.get_json()["ignored"] == 1
    assert test_client.get("/api/incidents").get_json() == []


def test_same_alert_can_open_a_new_incident_after_verified_resolution():
    """一次事件关闭后，同 fingerprint 再次 firing 必须形成新的诊断周期。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "KUBERNETES_ADAPTER": VerifiableKubernetesAdapter(healthy=True),
        }
    )
    test_client = application.test_client()
    test_client.post(
        "/api/alerts/prometheus",
        json=alert_payload("firing", "recurring-alert"),
    )
    test_client.post(
        "/api/alerts/prometheus",
        json=alert_payload("resolved", "recurring-alert"),
    )

    response = test_client.post(
        "/api/alerts/prometheus",
        json=alert_payload("firing", "recurring-alert"),
    )
    incidents = test_client.get("/api/incidents").get_json()

    assert response.status_code == 202
    assert response.get_json()["accepted"] == 1
    assert len(incidents) == 2
    assert {item["status"] for item in incidents} == {"RESOLVED", "DIAGNOSED"}
