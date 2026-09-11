# K8s Ops Agent 架构

## 控制面与演练面

主应用是模块化单体控制面：Flask 提供 API 和控制台，PostgreSQL 保存 Incident、
Action Task、Plan、Execution、AgentRun、AgentStep、ToolInvocation、Evidence、Outbox 和 Audit；Redis/RQ 负责
异步工作。Outbox Dispatcher 将数据库中已提交的作业请求可靠投递到 RQ，并负责
识别超时的诊断与执行。

`examples/demo-app/` 是完全独立的演练面。它不是 Ops Agent 的业务数据库，也不会
把通用 Todo 概念带入运维领域。

## 核心约束

- 告警先聚合为 Incident，相同活动 fingerprint 不重复建事件。
- AgentRun 通过有界循环按需选择注册的只读工具，不能提交任意 Shell 命令。
- Agent Worker 与 Execution Worker 使用不同队列和 ServiceAccount；只有后者具有受限写权限。
- Kubernetes 日志、状态、事件和 Prometheus 指标先脱敏、限长并保存为 Evidence；
  规则或可选 LLM 的结论必须引用 Evidence ID。
- LLM 默认关闭，启用后只返回结构化决策；模型响应错误经计费记录后回退规则，策略拒绝不能绕过。
- Alertmanager `resolved` 通知必须经过资源/指标验证，不能直接关闭 Incident。
- API 只写业务事实和 Outbox，不能在数据库提交后直接向队列发送易丢失消息。
- 修复动作只能来自 Action Catalog，目前仅有控制器管理的异常 Pod 重建。
- 所有动作都经过 dry-run、策略检查、审批记录、幂等 Execution 和结果验证。
- 自动修复默认关闭；功能开关不能绕过命名空间、UID、控制器和健康状态校验。
- Audit 只追加，失败执行也必须提交审计和人工跟进 Task。
- `/live` 只表示进程存活，`/ready` 检查 PostgreSQL 与 Redis。

完整状态机、API 和交付顺序见 `design/ops-agent-domain.md`；v0.3 Agent Core 运行、
安全与回退见 `design/v0.3-agent-core.md`。

## 上下文与记忆

Agent 在 RQ Worker 中调度完整调查循环，LLM 只位于 ContextBuilder 之后的决策环节。
每轮从当前运行的证据和步骤重建工作记忆，保存到已有 AgentStep 快照，无新增数据库表。
上下文按完整请求预算选择有效证据，保留时间、UID、证据引用及裁剪标记；模型调用前预留
预算，返回后结算 usage，未知消耗保留预留占用。模型引用按本轮可见证据验证，规则降级
按其实际读取的有效证据验证。计划、审批、修复执行继续在独立流程中完成。

实现范围与验收记录见 `design/context-memory-task-cards.md`。
