# 监控体系说明

基于 `kube-prometheus-stack` 接通 Prometheus、Alertmanager、Grafana、
kube-state-metrics、Ops Agent 指标和 Redis exporter。

## 文件列表

| 文件 | 用途 |
|------|------|
| [namespace.yaml](namespace.yaml) | 创建 monitoring 命名空间 |
| [kube-prometheus-stack-values.yaml](kube-prometheus-stack-values.yaml) | Helm values 与 Alertmanager 路由 |
| [service-monitors.yaml](service-monitors.yaml) | Ops Agent/Redis exporter 抓取资源 |
| [prometheus-alert-rules.yaml](prometheus-alert-rules.yaml) | Prometheus 告警规则（PrometheusRule） |
| [grafana-dashboards.yaml](grafana-dashboards.yaml) | Grafana 仪表盘配置（ConfigMap） |

## 部署步骤

### 1. 安装 kube-prometheus-stack

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f monitoring/kube-prometheus-stack-values.yaml
```

### 2. 创建项目监控资源

```bash
kubectl apply -f monitoring/prometheus-alert-rules.yaml
kubectl apply -f monitoring/service-monitors.yaml
kubectl apply -f monitoring/grafana-dashboards.yaml
```

## 告警规则说明

| 告警名称 | 严重级别 | 触发条件 |
|----------|----------|----------|
| OpsAgentDown | critical | Ops Agent 指标目标不可用 |
| OpsAgentHigh5xxRate | warning | 5xx 比例持续超过 5% |
| OpsAgentRunFailures | warning | AgentRun 诊断失败 |
| OpsAgentRemediationFailure | critical | 修复或验证失败 |
| RedisDown | critical | Redis 不可用 |

## Grafana 面板说明

- **Pod 状态**：显示 Ops Agent 就绪 Pod 数量
- **CPU 使用率**：各 Pod 的 CPU 使用趋势
- **内存使用率**：各 Pod 的内存使用趋势
- **HTTP 请求速率**：请求速率变化
- **Redis 连接状态**：Redis 连接状态指示
- **Pod 重启次数**：累计重启次数
