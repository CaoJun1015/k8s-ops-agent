# 监控体系说明

响应简历中「搭建 Prometheus + Grafana 监控体系」的描述。

## 文件列表

| 文件 | 用途 |
|------|------|
| [namespace.yaml](namespace.yaml) | 创建 monitoring 命名空间 |
| [prometheus-config.yaml](prometheus-config.yaml) | Prometheus 抓取配置（ConfigMap） |
| [prometheus-alert-rules.yaml](prometheus-alert-rules.yaml) | Prometheus 告警规则（PrometheusRule） |
| [grafana-dashboards.yaml](grafana-dashboards.yaml) | Grafana 仪表盘配置（ConfigMap） |

## 部署步骤

### 1. 安装 Prometheus Operator

```bash
kubectl create -f https://raw.githubusercontent.com/prometheus-operator/prometheus-operator/main/bundle.yaml
```

### 2. 创建监控命名空间

```bash
kubectl apply -f monitoring/namespace.yaml
```

### 3. 部署 Prometheus 配置

```bash
kubectl apply -f monitoring/prometheus-config.yaml
kubectl apply -f monitoring/prometheus-alert-rules.yaml
```

### 4. 部署 Grafana 仪表盘

```bash
kubectl apply -f monitoring/grafana-dashboards.yaml
```

## 告警规则说明

| 告警名称 | 严重级别 | 触发条件 |
|----------|----------|----------|
| HighPodRestartCount | warning | Pod 1 小时内重启次数过高 |
| HighCPUUsage | warning | CPU 使用率 > 80% |
| HighMemoryUsage | critical | 内存使用率 > 85% |
| PodNotReady | critical | Pod 超过 2 分钟未就绪 |
| HighErrorRate | critical | 5xx 错误率过高 |
| RedisDown | critical | Redis 不可用 |

## Grafana 面板说明

- **Pod 状态**：显示就绪 Pod 数量，少于 3 个变红
- **CPU 使用率**：各 Pod 的 CPU 使用趋势
- **内存使用率**：各 Pod 的内存使用趋势
- **HTTP 请求速率**：请求速率变化
- **Redis 连接状态**：Redis 连接状态指示
- **Pod 重启次数**：累计重启次数