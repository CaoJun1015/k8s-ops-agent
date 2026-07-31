"""Prometheus 指标与 Alertmanager 入站测试。"""

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


def test_metrics_endpoint_exposes_http_and_agent_metrics(client):
    """Prometheus 必须能抓取应用自身的标准文本指标。"""
    client.get("/live")
    response = client.get("/metrics")
    body = response.get_data(as_text=True)

    assert response.status_code == 200
    assert response.mimetype == "text/plain"
    assert "ops_agent_info" in body
    assert "ops_agent_http_requests_total" in body


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
    assert incidents[0]["version"] == 2


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
