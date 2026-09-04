"""Prometheus 查询适配器和指标证据测试。"""

from types import SimpleNamespace

from ops_agent.domain import IncidentSeverity
from ops_agent.prometheus_adapter import PrometheusAdapter


def test_prometheus_query_encodes_expression_and_validates_response():
    calls = []

    def transport(url, timeout):
        calls.append((url, timeout))
        return {
            "status": "success",
            "data": {
                "resultType": "vector",
                "result": [
                    {"metric": {"pod": "demo-1"}, "value": [1, "3"]}
                ],
            },
        }

    adapter = PrometheusAdapter(
        "http://prometheus.monitoring.svc:9090",
        timeout=3,
        transport=transport,
    )
    result = adapter.query(
        'kube_pod_status_ready{namespace="default",pod="demo-1"}'
    )

    assert result["result_type"] == "vector"
    assert result["result"][0]["value"][1] == "3"
    assert calls[0][1] == 3
    assert "%7B" in calls[0][0]


def test_incident_metrics_use_bounded_named_queries():
    expressions = []

    def transport(url, _timeout):
        expressions.append(url)
        return {
            "status": "success",
            "data": {"resultType": "vector", "result": []},
        }

    adapter = PrometheusAdapter("http://prometheus:9090", transport=transport)
    incident = SimpleNamespace(
        title="Pod not ready",
        summary="probe failed",
        severity=IncidentSeverity.HIGH,
        namespace="default",
        resource_kind="Pod",
        resource_name="demo-1",
    )

    evidence = adapter.collect_incident_metrics(incident)

    assert {item["name"] for item in evidence["queries"]} == {
        "pod_ready",
        "pod_restarts",
    }
    assert len(expressions) == 2
