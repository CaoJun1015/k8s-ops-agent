# 故障排查手册

## 排查思维框架

```
服务访问不了？
├─ ping IP → 不通 → 网络/防火墙/服务器问题
├─ ss | grep 端口 → 没监听 → 服务没启动
├─ curl http://IP:端口 → 没响应 → 服务内部错误
└─ curl 返回200 → 服务正常，问题在客户端
```

## 常见故障及处理

### 1. Pod CrashLoopBackOff

```
现象：Pod反复重启，状态显示CrashLoopBackOff

排查：
  kubectl describe pod <pod-name>        → 看Events
  kubectl logs <pod-name> --previous     → 看崩溃前日志
  kubectl logs <pod-name>                → 看当前日志

常见原因：
  - 应用启动失败（配置错误/依赖不可用）
  - 内存溢出（OOMKilled）
  - 健康检查失败

处理：
  - 配置错误：修复配置，重新部署
  - OOM：增加内存限制
  - 健康检查：检查探针路径和端口
```

### 2. Pod ImagePullBackOff

```
现象：Pod无法启动，显示ImagePullBackOff

排查：
  kubectl describe pod <pod-name>        → 看Events中的错误信息

常见原因：
  - 镜像名/标签写错
  - 私有仓库需要认证
  - 网络问题拉不到镜像

处理：
  - 检查镜像名是否正确
  - 配置imagePullSecrets
  - minikube里用 minikube image load 导入
```

### 3. Service访问不通

```
现象：curl Service地址无响应

排查：
  kubectl get svc                         → 确认Service存在
  kubectl get endpoints                   → 确认有endpoint
  kubectl describe svc <svc-name>         → 看selector是否匹配

常见原因：
  - selector和Pod的label不匹配
  - Pod没有运行
  - 端口配置错误

处理：
  - 修正selector或Pod的label
  - 确保Pod正常运行
  - 检查targetPort是否正确
```

### 4. 节点NotReady

```
现象：kubectl get nodes 显示NotReady

排查：
  kubectl describe node <node-name>      → 看Conditions
  ssh到节点上查看kubelet状态

常见原因：
  - kubelet挂了
  - 磁盘/内存压力
  - 网络不通

处理：
  - 重启kubelet
  - 清理磁盘/释放内存
  - 检查网络连接
```

### 5. PVC Pending

```
现象：PVC一直处于Pending状态

排查：
  kubectl describe pvc <pvc-name>        → 看Events

常见原因：
  - 没有匹配的StorageClass
  - PV不足
  - accessModes不匹配

处理：
  - 检查StorageClass配置
  - 创建足够的PV
  - 修正accessModes
```

## 命令速查

```
# Pod排查
kubectl get pods -A                      → 列出所有Pod
kubectl describe pod <name> -n <ns>      → Pod详情
kubectl logs <name> -n <ns> --tail=50    → 最近50行日志
kubectl logs <name> -n <ns> --previous   → 上一次崩溃日志
kubectl exec -it <name> -n <ns> -- sh    → 进入容器

# Service排查
kubectl get svc -A                       → 列出所有Service
kubectl get endpoints <name>             → 查看endpoint
kubectl port-forward svc/<name> 8080:80  → 端口转发测试

# 节点排查
kubectl top nodes                        → 节点资源使用
kubectl describe node <name>             → 节点详情

# 事件排查
kubectl get events -A --sort-by='.lastTimestamp'  → 最近事件
kubectl get events -A --field-selector type=Warning → 只看Warning
```
