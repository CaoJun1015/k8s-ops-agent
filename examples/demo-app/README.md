# Demo Todo Workload

该应用是 K8s Ops Agent 的独立故障演练靶场，不保存 Ops Agent 业务数据。

```bash
docker build -t demo-todo:v1 examples/demo-app
kubectl apply -f examples/demo-app/k8s/demo-app.yaml
kubectl apply -f examples/demo-app/k8s/scenario-crashloop.yaml
```

`scenario-crashloop.yaml` 会制造一个由 Deployment 管理的 CrashLoopBackOff Pod，
用于验证 Prometheus 告警、Incident 聚合、AgentRun 诊断、dry-run、审批、Pod
重建和结果验证。将 `demo-crashloop-mode` ConfigMap 的 `mode` 改为 `healthy` 后，
同一 Pod 会在下一次重启时恢复，便于验证 resolved 告警。测试完毕后删除该场景
Deployment 和 ConfigMap。
