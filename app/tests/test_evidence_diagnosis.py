"""真实证据采集、脱敏和规则诊断测试。"""

import pytest
from types import SimpleNamespace

from app import create_app
from ops_agent.diagnosis import (
    DiagnosisPipeline,
    OpenAIStructuredDiagnosis,
    RuleDiagnosis,
)
from ops_agent.domain import EvidenceType, IncidentSeverity
from ops_agent.evidence import sanitize_content


class FakeDiagnosticKubernetesAdapter:
    def __init__(self):
        self.calls = []

    def collect_pod_evidence(self, *, namespace, pod_name, log_tail_lines):
        self.calls.append((namespace, pod_name, log_tail_lines))
        return {
            "pod_status": {
                "exists": True,
                "phase": "Running",
                "ready": False,
                "containers": [
                    {
                        "name": "api",
                        "restart_count": 7,
                        "waiting_reason": "CrashLoopBackOff",
                        "last_exit_code": 1,
                    }
                ],
            },
            "current_logs": {
                "api": "startup failed password=super-secret",
            },
            "previous_logs": {"api": "Authorization: Bearer abc.def.ghi"},
            "events": [
                {"reason": "BackOff", "message": "Back-off restarting container"}
            ],
            "workload_status": {
                "kind": "Deployment",
                "name": "demo",
                "ready_replicas": 0,
                "desired_replicas": 1,
            },
            "resource_limits": {
                "api": {
                    "requests": {"cpu": "100m"},
                    "limits": {"memory": "128Mi"},
                }
            },
        }


@pytest.fixture()
def diagnostic_app():
    adapter = FakeDiagnosticKubernetesAdapter()
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "KUBERNETES_ADAPTER": adapter,
            "DIAGNOSIS_PROVIDER": "rules",
        }
    )
    application.extensions["test_kubernetes_adapter"] = adapter
    return application


def create_pod_incident(client):
    response = client.post(
        "/api/incidents",
        json={
            "title": "demo Pod 持续重启",
            "severity": "HIGH",
            "fingerprint": "default:Pod:demo-7:CrashLoopBackOff",
            "cluster": "local",
            "namespace": "default",
            "resource_kind": "Pod",
            "resource_name": "demo-7",
            "source": "prometheus",
            "summary": "容器反复退出",
        },
    )
    assert response.status_code == 201
    return response.get_json()


def test_diagnosis_collects_persists_and_references_real_evidence(diagnostic_app):
    """规则结论必须引用本次运行持久化的证据，而不是拼接固定文案。"""
    client = diagnostic_app.test_client()
    incident = create_pod_incident(client)

    accepted = client.post(
        f"/api/incidents/{incident['id']}/agent-runs",
        json={"mode": "DIAGNOSE_ONLY"},
    )
    run = client.get(accepted.get_json()["location"]).get_json()
    evidence_response = client.get(
        f"/api/agent-runs/{run['id']}/evidence"
    )
    evidence = evidence_response.get_json()

    assert evidence_response.status_code == 200
    assert run["status"] == "COMPLETED"
    assert run["diagnosis"]["provider"] == "rules"
    assert run["diagnosis"]["diagnosis_code"] == "CRASH_LOOP"
    assert set(run["diagnosis"]["evidence_ids"]).issubset(
        {item["id"] for item in evidence}
    )
    assert {
        "ALERT_PAYLOAD",
        "POD_STATUS",
        "CURRENT_LOGS",
        "PREVIOUS_LOGS",
        "K8S_EVENTS",
        "WORKLOAD_STATUS",
        "RESOURCE_LIMITS",
    }.issubset({item["evidence_type"] for item in evidence})
    assert diagnostic_app.extensions["test_kubernetes_adapter"].calls == [
        ("default", "demo-7", 200)
    ]

    serialized = str(evidence)
    assert "super-secret" not in serialized
    assert "abc.def.ghi" not in serialized
    assert "[REDACTED]" in serialized


def test_sanitize_content_redacts_sensitive_fields_and_limits_payload():
    """敏感字段与日志内凭据都必须脱敏，过大文本必须显式截断。"""
    content, redacted = sanitize_content(
        {
            "authorization": "Bearer top-secret-token",
            "cookie": "session=private-value",
            "log": "password=hunter2\n" + ("x" * 70_000),
        },
        max_bytes=2_048,
    )

    rendered = str(content)
    assert redacted is True
    assert "top-secret-token" not in rendered
    assert "private-value" not in rendered
    assert "hunter2" not in rendered
    assert content["truncated"] is True
    assert len(rendered.encode("utf-8")) < 3_000


def test_unknown_diagnosis_only_recommends_investigation():
    """证据不足时必须保守降级，不能产生可执行修复建议。"""
    application = create_app(
        {
            "TESTING": True,
            "DATABASE_URL": "sqlite+pysqlite:///:memory:",
            "QUEUE_MODE": "inline",
            "DIAGNOSIS_PROVIDER": "rules",
        }
    )
    client = application.test_client()
    incident = client.post(
        "/api/incidents",
        json={
            "title": "未知告警",
            "fingerprint": "unknown-1",
            "namespace": "default",
            "resource_kind": "Deployment",
            "resource_name": "unknown",
            "source": "test",
        },
    ).get_json()

    accepted = client.post(f"/api/incidents/{incident['id']}/agent-runs")
    run = client.get(accepted.get_json()["location"]).get_json()

    assert run["diagnosis"]["diagnosis_code"] == "UNKNOWN"
    assert run["diagnosis"]["recommended_actions"] == [
        {
            "action_type": "INVESTIGATE",
            "reason": "证据不足，需要人工补充检查",
        }
    ]


class FakeResponses:
    def __init__(self, output_text):
        self.output_text = output_text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(output_text=self.output_text)


def diagnosis_input():
    incident = SimpleNamespace(
        id="incident-1",
        title="Pod not ready",
        summary="readiness probe failed",
        severity=IncidentSeverity.HIGH,
        namespace="default",
        resource_kind="Pod",
        resource_name="demo-1",
    )
    evidence = [
        SimpleNamespace(
            id="evidence-1",
            evidence_type=EvidenceType.POD_STATUS,
            source="kubernetes",
            content={"ready": False, "containers": []},
        )
    ]
    return incident, evidence


def test_optional_llm_uses_strict_schema_and_persisted_evidence_ids():
    """LLM 只允许返回固定 Schema，并且只能引用真实 Evidence ID。"""
    responses = FakeResponses(
        '{"summary":"探针失败","probable_cause":"端口配置错误",'
        '"confidence":0.86,"severity":"HIGH",'
        '"evidence_ids":["evidence-1"],'
        '"recommended_actions":[{"action_type":"INVESTIGATE",'
        '"reason":"核对 readinessProbe 端口"}],'
        '"diagnosis_code":"POD_NOT_READY"}'
    )
    client = SimpleNamespace(responses=responses)
    pipeline = DiagnosisPipeline(
        RuleDiagnosis(),
        OpenAIStructuredDiagnosis(client=client, model="test-model"),
    )
    incident, evidence = diagnosis_input()

    result = pipeline.diagnose(incident, evidence)

    assert result["provider"] == "openai"
    assert result["evidence_ids"] == ["evidence-1"]
    request = responses.calls[0]
    assert request["store"] is False
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True


def test_invalid_llm_result_falls_back_to_rule_diagnosis():
    """非法引用或非 Schema 输出不能污染规则诊断结果。"""
    responses = FakeResponses(
        '{"summary":"执行 rm -rf","probable_cause":"unknown",'
        '"confidence":2,"severity":"HIGH",'
        '"evidence_ids":["invented"],"recommended_actions":[],'
        '"diagnosis_code":"UNKNOWN"}'
    )
    pipeline = DiagnosisPipeline(
        RuleDiagnosis(),
        OpenAIStructuredDiagnosis(
            client=SimpleNamespace(responses=responses), model="test-model"
        ),
    )
    incident, evidence = diagnosis_input()

    result = pipeline.diagnose(incident, evidence)

    assert result["provider"] == "rules"
    assert result["diagnosis_code"] == "POD_NOT_READY"
    assert result["llm_fallback"] is True


def test_redis_rule_reads_real_prometheus_vector_result():
    """Redis 诊断必须理解 Prometheus HTTP API 的真实 vector 结构。"""
    incident = SimpleNamespace(
        severity=IncidentSeverity.CRITICAL,
        title="cache dependency alert",
        summary="dependency unavailable",
    )
    evidence = [
        SimpleNamespace(
            id="metric-1",
            evidence_type=EvidenceType.PROMETHEUS_METRICS,
            content={
                "queries": [
                    {
                        "name": "redis_up",
                        "result_type": "vector",
                        "result": [
                            {"metric": {"instance": "redis"}, "value": [1, "0"]}
                        ],
                    }
                ]
            },
        )
    ]

    result = RuleDiagnosis().diagnose(incident, evidence)

    assert result["diagnosis_code"] == "REDIS_UNAVAILABLE"
    assert result["evidence_ids"] == ["metric-1"]


def test_alert_payload_is_persisted_after_redaction(diagnostic_app):
    """Alertmanager annotations 也属于不可信输入，必须在 Evidence 入库前脱敏。"""
    client = diagnostic_app.test_client()
    response = client.post(
        "/api/alerts/prometheus",
        json={
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "fingerprint": "sensitive-alert",
                    "labels": {
                        "alertname": "PodNotReady",
                        "namespace": "default",
                        "pod": "demo-7",
                        "token": "label-secret",
                    },
                    "annotations": {
                        "summary": "password=annotation-secret"
                    },
                }
            ],
        },
    )
    assert response.status_code == 202
    incident_id = client.get("/api/incidents").get_json()[0]["id"]
    incident = client.get(f"/api/incidents/{incident_id}").get_json()
    run = client.post(f"/api/incidents/{incident_id}/agent-runs").get_json()
    evidence = client.get(f"{run['location']}/evidence").get_json()
    alert = next(
        item for item in evidence if item["evidence_type"] == "ALERT_PAYLOAD"
    )

    rendered = str(alert["content"])
    assert "annotation-secret" not in str(incident)
    assert alert["content"]["labels"]["token"] == "[REDACTED]"
    assert "label-secret" not in rendered
    assert "annotation-secret" not in rendered
    assert "[REDACTED]" in rendered
