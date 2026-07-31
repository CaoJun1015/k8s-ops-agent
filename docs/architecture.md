# K8s Ops Agent 架构

## 控制面与演练面

主应用是模块化单体控制面：Flask 提供 API 和控制台，PostgreSQL 保存 Incident、
Action Task、Plan、Execution、AgentRun 和 Audit；Redis/RQ 负责异步工作。

`examples/demo-app/` 是完全独立的演练面。它不是 Ops Agent 的业务数据库，也不会
把通用 Todo 概念带入运维领域。

## 核心约束

- 告警先聚合为 Incident，相同活动 fingerprint 不重复建事件。
- AgentRun 第一职责是采集和诊断，不能提交任意 Shell 命令。
- 修复动作只能来自 Action Catalog，目前仅有控制器管理的异常 Pod 重建。
- 所有动作都经过 dry-run、策略检查、审批记录、幂等 Execution 和结果验证。
- 自动修复默认关闭；功能开关不能绕过命名空间、UID、控制器和健康状态校验。
- Audit 只追加，失败执行也必须提交审计和人工跟进 Task。
- `/live` 只表示进程存活，`/ready` 检查 PostgreSQL 与 Redis。

完整状态机、API 和交付顺序见 `design/ops-agent-domain.md`。
