"""部署与监控清单的静态契约测试。"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def load_documents(relative_path):
    with (ROOT / relative_path).open(encoding="utf-8") as source:
        return [item for item in yaml.safe_load_all(source) if item]


def test_ops_agent_deployment_uses_split_health_probes_and_pullable_image():
    """生产 Deployment 必须区分存活/就绪，并允许拉取 CI 构建镜像。"""
    deployment = load_documents("k8s/app-deployment.yaml")[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]

    assert deployment["metadata"]["name"] == "ops-agent"
    assert container["name"] == "ops-agent"
    assert container["imagePullPolicy"] in {"IfNotPresent", "Always"}
    assert container["livenessProbe"]["httpGet"]["path"] == "/live"
    assert container["readinessProbe"]["httpGet"]["path"] == "/ready"


def test_service_monitor_selects_named_ops_agent_metrics_port():
    """ServiceMonitor selector 和 Service 标签/端口必须一致。"""
    service = load_documents("k8s/app-service.yaml")[0]
    monitors = load_documents("monitoring/service-monitors.yaml")
    monitor = next(item for item in monitors if item["metadata"]["name"] == "ops-agent")

    assert service["metadata"]["labels"]["app"] == "ops-agent"
    assert service["spec"]["ports"][0]["name"] == "http"
    assert monitor["spec"]["selector"]["matchLabels"]["app"] == "ops-agent"
    assert monitor["spec"]["endpoints"][0]["port"] == "http"


def test_worker_uses_dedicated_service_account_and_execution_queue():
    """后台 Worker 必须通过受限 ServiceAccount 消费两个明确队列。"""
    deployment = load_documents("k8s/worker-deployment.yaml")[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    command = pod_spec["containers"][0]["command"]

    assert pod_spec["serviceAccountName"] == "ops-agent-worker"
    assert "agent-runs" in command
    assert "executions" in command


def test_only_worker_role_can_delete_pods():
    """API 只能读取诊断证据，只有执行 Worker 可以删除 Pod。"""
    resources = load_documents("k8s/rbac.yaml")
    roles = {
        item["metadata"]["name"]: item
        for item in resources
        if item["kind"] == "Role"
    }

    api_verbs = {
        verb
        for rule in roles["ops-agent-api"]["rules"]
        for verb in rule["verbs"]
    }
    worker_pod_rule = next(
        rule
        for rule in roles["ops-agent-worker"]["rules"]
        if "pods" in rule["resources"]
    )
    assert "delete" not in api_verbs
    assert "delete" in worker_pod_rule["verbs"]


def test_monitoring_files_are_valid_multi_document_yaml():
    """所有监控 YAML 都必须至少包含一个可解析资源。"""
    for path in (
        "monitoring/service-monitors.yaml",
        "monitoring/prometheus-alert-rules.yaml",
        "monitoring/grafana-dashboards.yaml",
    ):
        assert load_documents(path), path
