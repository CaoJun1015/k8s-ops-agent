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


def test_runtime_image_uses_numeric_non_root_identity():
    """Kubernetes 必须能在启动前验证镜像用户不是 root。"""
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")

    assert "useradd --uid 10001 --gid 10001" in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_api_configures_gunicorn_multiprocess_metrics_directory():
    """API Pod 必须为多进程 Prometheus 指标提供独立临时目录。"""
    deployment = load_documents("k8s/app-deployment.yaml")[0]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    environment = {item["name"]: item for item in container.get("env", [])}

    assert environment["PROMETHEUS_MULTIPROC_DIR"]["value"] == "/tmp/prometheus"
    assert container["command"] == ["python", "run_gunicorn.py"]


def test_gunicorn_config_marks_exited_worker_metrics_dead():
    """退出 worker 的指标文件必须由 Gunicorn hook 清理。"""
    config = (ROOT / "app" / "gunicorn.conf.py").read_text(encoding="utf-8")

    assert "def child_exit" in config
    assert "mark_process_dead(worker.pid)" in config


def test_service_monitor_selects_named_ops_agent_metrics_port():
    """ServiceMonitor selector 和 Service 标签/端口必须一致。"""
    service = load_documents("k8s/app-service.yaml")[0]
    monitors = load_documents("monitoring/service-monitors.yaml")
    monitor = next(item for item in monitors if item["metadata"]["name"] == "ops-agent")

    assert service["metadata"]["labels"]["app"] == "ops-agent"
    assert service["spec"]["ports"][0]["name"] == "http"
    assert monitor["spec"]["selector"]["matchLabels"]["app"] == "ops-agent"
    assert monitor["spec"]["endpoints"][0]["port"] == "http"


def test_agent_and_execution_workers_use_separate_queues_and_identities():
    """只读调查和变更执行必须使用不同身份与队列。"""
    deployment = load_documents("k8s/worker-deployment.yaml")[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    command = pod_spec["containers"][0]["command"]

    assert pod_spec["serviceAccountName"] == "ops-agent-agent"
    assert "agent-runs" in command
    assert "executions" not in command

    executor = load_documents("k8s/execution-worker-deployment.yaml")[0]
    executor_spec = executor["spec"]["template"]["spec"]
    assert executor_spec["serviceAccountName"] == "ops-agent-executor"
    assert "executions" in executor_spec["containers"][0]["command"]


def test_outbox_dispatcher_has_dedicated_identity_and_no_cluster_role():
    """Outbox dispatcher 只访问数据库和 Redis，不应获得集群资源权限。"""
    deployment = load_documents("k8s/dispatcher-deployment.yaml")[0]
    pod_spec = deployment["spec"]["template"]["spec"]
    resources = load_documents("k8s/rbac.yaml")
    bound_accounts = {
        subject["name"]
        for item in resources
        if item["kind"] == "RoleBinding"
        for subject in item["subjects"]
    }

    assert pod_spec["serviceAccountName"] == "ops-agent-dispatcher"
    assert "ops-agent-dispatcher" not in bound_accounts
    assert pod_spec["containers"][0]["command"] == [
        "python",
        "-m",
        "ops_agent.outbox",
    ]


def test_only_execution_worker_role_can_delete_pods():
    """API 和 Agent 只能读取，只有 Execution Worker 可以删除 Pod。"""
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
    agent_pod_rule = next(
        rule
        for rule in roles["ops-agent-agent"]["rules"]
        if "pods" in rule["resources"]
    )
    executor_pod_rule = next(
        rule
        for rule in roles["ops-agent-executor"]["rules"]
        if "pods" in rule["resources"]
    )
    assert "delete" not in api_verbs
    assert "delete" not in agent_pod_rule["verbs"]
    assert "delete" in executor_pod_rule["verbs"]


def test_monitoring_files_are_valid_multi_document_yaml():
    """所有监控 YAML 都必须至少包含一个可解析资源。"""
    for path in (
        "monitoring/service-monitors.yaml",
        "monitoring/prometheus-alert-rules.yaml",
        "monitoring/grafana-dashboards.yaml",
    ):
        assert load_documents(path), path


def test_real_secret_files_are_ignored_and_only_templates_are_tracked():
    """仓库不得保留可直接部署的固定凭据清单。"""
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert not (ROOT / "k8s" / "secret.yaml").exists()
    assert not (ROOT / "monitoring" / "alertmanager-secret.yaml").exists()
    assert "k8s/secret.yaml" in gitignore
    assert "monitoring/alertmanager-secret.yaml" in gitignore
    assert load_documents("k8s/secret.example.yaml")
    assert load_documents("monitoring/alertmanager-secret.example.yaml")


def test_alertmanager_reads_webhook_token_from_secret_selector():
    """Alertmanager 路由不得在 Helm values 中硬编码 webhook token。"""
    values = (ROOT / "monitoring" / "kube-prometheus-stack-values.yaml").read_text(
        encoding="utf-8"
    )
    config = load_documents("monitoring/alertmanager-config.yaml")[0]
    authorization = config["spec"]["receivers"][0]["webhookConfigs"][0][
        "httpConfig"
    ]["authorization"]

    assert "credentials:" not in values
    assert authorization["credentials"] == {
        "name": "ops-agent-alertmanager",
        "key": "token",
    }
    assert config["metadata"]["namespace"] == "monitoring"


def test_kind_e2e_covers_fault_diagnosis_evidence_and_recovery():
    """kind 门禁必须覆盖故障触发、证据诊断和恢复验证，而非只检查 YAML。"""
    script = (ROOT / "ci" / "kind-e2e.sh").read_text(encoding="utf-8")

    for contract in (
        "kind create cluster",
        "scenario-crashloop.yaml",
        '"status":"firing"',
        "/evidence",
        "/steps",
        "ops-agent-agent",
        '"diagnosis_code":"CRASH_LOOP"',
        '"status":"resolved"',
        '"status":"RESOLVED"',
    ):
        assert contract in script
    assert "busybox:1.36 busybox:e2e" in script
    assert "image: busybox:e2e" in script
    assert "k8s/secret.yaml" not in script


def test_recoverable_crashloop_scenario_uses_projected_mode_config():
    """演练故障必须能在不替换 Pod 的情况下恢复，以验证同一目标。"""
    resources = load_documents("examples/demo-app/k8s/scenario-crashloop.yaml")
    config = next(item for item in resources if item["kind"] == "ConfigMap")
    deployment = next(item for item in resources if item["kind"] == "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]

    assert config["metadata"]["name"] == "demo-crashloop-mode"
    assert config["data"]["mode"] == "crash"
    assert pod_spec["volumes"][0]["configMap"]["name"] == "demo-crashloop-mode"


def test_console_uses_occurrence_count_and_exposes_evidence_panel():
    """控制台不能把乐观锁版本误显示成告警次数。"""
    console = (ROOT / "app" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "i.occurrence_count" in console
    assert "诊断与证据" in console
    assert "/evidence`" in console
    assert "timeline-step" in console
    assert "/cancel" in console


def test_agent_dashboard_uses_bounded_metric_labels():
    dashboard = (ROOT / "monitoring" / "grafana-dashboards.yaml").read_text(
        encoding="utf-8"
    )
    for metric in (
        "agent_runs_total",
        "agent_run_duration_seconds_bucket",
        "agent_tool_calls_total",
        "agent_budget_exhausted_total",
        "agent_human_handoff_total",
    ):
        assert metric in dashboard
    assert "incident_id" not in dashboard
