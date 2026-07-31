# K8s Ops Agent

一个以 Incident 为中心的 Kubernetes 运维工作台，支持告警聚合、行动项协作、
只诊断 AgentRun、受控 Pod 修复、人工审批、结果验证和追加式审计。

## 业务闭环

```text
Prometheus Alert
  → Incident
  → AgentRun（采集与诊断）
  → Plan（固定 Action Catalog）
  → dry-run / 策略检查
  → 人工审批或低风险自动策略
  → Execution
  → 恢复验证
  → RESOLVED / 人工 Action Task
```

原 Todo 应用已迁移到 `examples/demo-app/`，只作为 CrashLoopBackOff、5xx 和
依赖故障等端到端演练靶场。

## 组件

| 组件 | 职责 |
|---|---|
| Flask API / Ops Console | Incident、Task、Plan、Execution、Audit API |
| PostgreSQL | 长期业务事实与审计记录 |
| Redis + RQ | 后台诊断、执行队列与短期协调 |
| Agent Worker | 只诊断 AgentRun 和已批准的修复执行 |
| Kubernetes Adapter | 结构化读取与白名单 Pod 重建 |
| kube-prometheus-stack | Prometheus、Alertmanager、Grafana、集群指标 |
| Redis exporter | 提供真实 `redis_up` 等指标 |

自动修复默认关闭。即使开启，也只允许重建白名单命名空间中、由 Deployment 或
StatefulSet 管理、且当前确认异常的 Pod。

## 本地运行

```bash
docker compose -f docker/docker-compose.yml up --build
```

Compose 会启动 PostgreSQL、Redis、数据库迁移、Ops Agent API、RQ Worker 和
Nginx。访问 `http://localhost`。

## Kubernetes 部署

先修改 `k8s/secret.yaml` 中的数据库密码、Alert Webhook Token 和 Operator
Token，并同步修改监控 Helm values 中的 Alert Webhook Token，再构建并推送
`ops-agent` 镜像。审批、拒绝和执行接口在生产配置下要求
`Authorization: Bearer <OPERATOR_API_TOKEN>`。

```bash
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/postgres.yaml
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/redis-service.yaml
kubectl apply -f k8s/rbac.yaml

kubectl apply -f k8s/migration-job.yaml
kubectl wait --for=condition=complete job/ops-agent-db-migrate --timeout=180s

kubectl apply -f k8s/app-deployment.yaml
kubectl apply -f k8s/worker-deployment.yaml
kubectl apply -f k8s/app-service.yaml
```

安装真实监控链路：

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  -f monitoring/kube-prometheus-stack-values.yaml

kubectl apply -f monitoring/service-monitors.yaml
kubectl apply -f monitoring/prometheus-alert-rules.yaml
kubectl apply -f monitoring/grafana-dashboards.yaml
```

## 故障演练

```bash
docker build -t demo-todo:v1 examples/demo-app
kubectl apply -f examples/demo-app/k8s/demo-app.yaml
kubectl apply -f examples/demo-app/k8s/scenario-crashloop.yaml
```

保持 `AUTO_REMEDIATE_ENABLED=false` 可演练人工审批；只有完成 dry-run、审批和验证
测试后，才应在限定环境把它改为 `true`。

## 测试

```bash
python -m pytest app/tests -q
python -m pytest examples/demo-app/tests -q
```

领域与演进规范见 `docs/design/ops-agent-domain.md`，监控说明见
`monitoring/README.md`。
