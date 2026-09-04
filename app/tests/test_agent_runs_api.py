"""只诊断 AgentRun 编排测试。

场景覆盖：
- 第一阶段只能创建 DIAGNOSE_ONLY Run；
- Run 完成后保存结构化诊断并写入审计；
- 请求执行或修复模式时必须拒绝。
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
            "DIAGNOSIS_PROVIDER": "deterministic",
        }
    )
    with application.test_client() as test_client:
        yield test_client


def create_incident(client):
    return client.post(
        "/api/incidents",
        json={
            "title": "应用不可用",
            "severity": "CRITICAL",
            "fingerprint": "default/deployment/api:NotReady",
            "namespace": "default",
            "resource_kind": "Deployment",
            "resource_name": "api",
            "source": "test",
            "summary": "readiness probe failed",
        },
    ).get_json()


def test_diagnose_only_run_completes_and_is_audited(client):
    """内联测试队列应完成诊断，但不能创建 Execution。"""
    incident = create_incident(client)
    response = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"mode": "DIAGNOSE_ONLY"},
        headers={"Idempotency-Key": "diagnose-incident-once"},
    )

    assert response.status_code == 202
    run = client.get(response.get_json()["location"]).get_json()
    assert run["mode"] == "DIAGNOSE_ONLY"
    assert run["status"] == "COMPLETED"
    assert run["diagnosis"]["summary"]

    audits = client.get(
        f"/api/audit-events?entity_type=AgentRun&entity_id={run['id']}"
    ).get_json()
    assert {event["event_type"] for event in audits} >= {
        "agent_run.created",
        "agent_run.completed",
    }

    executions = client.get("/api/executions").get_json()
    assert executions == []


def test_non_diagnostic_agent_mode_is_rejected(client):
    """第一阶段禁止通过 AgentRun 请求集群变更。"""
    incident = create_incident(client)
    response = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"mode": "REMEDIATE"},
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "unsupported_mode"


def test_agent_runs_can_be_listed_by_incident(client):
    """控制台应能按 Incident 获取诊断历史并继续读取 Evidence。"""
    incident = create_incident(client)
    accepted = client.post(f"/api/incidents/{incident['id']}/agent-runs")

    response = client.get(f"/api/agent-runs?incident_id={incident['id']}")

    assert response.status_code == 200
    runs = response.get_json()
    assert [item["id"] for item in runs] == [accepted.get_json()["id"]]
    assert runs[0]["diagnosis"]["evidence_ids"]
