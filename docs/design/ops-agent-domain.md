# K8s Ops Agent 领域与演进设计

## 1. 目标与边界

本次改造将项目从“Todo 靶场 + 独立 Shell 脚本”演进为事件驱动的 Kubernetes
运维工作台。主应用管理运维事件、行动项、诊断、修复计划、执行和审计；原 Todo
应用迁移到 `examples/demo-app/`，仅作为端到端故障演练靶场。

第一阶段的 AgentRun 只采集证据并执行诊断，不允许修改集群。集群变更必须经过
Plan、dry-run、策略判断、人工审批、Execution 和结果验证。最终无人值守动作仅限
于重建由 Deployment 或 StatefulSet 管理、且命中白名单策略的异常 Pod。

## 2. 运行架构

- Flask API：提供 Ops Console、领域 API、健康检查和 Prometheus 指标。
- PostgreSQL：Incident、Task、Plan、Execution、AgentRun 和 Audit 的事实来源。
- Redis：后台任务队列、短期缓存、幂等锁；不保存长期业务事实。
- Worker：消费 AgentRun 和 Execution 作业。
- Kubernetes Adapter：只通过结构化 API 读取或执行白名单动作。
- Prometheus Adapter：查询指标并接收/规范化告警。
- LLM Adapter：接收脱敏后的结构化证据，只返回经过 Schema 校验的诊断结果。

## 3. 领域模型

### Incident

一次需要被跟踪和关闭的运维事件。相同 `fingerprint` 的活动告警合并到同一事件。

关键字段：`id`、`title`、`severity`、`status`、`fingerprint`、`cluster`、
`namespace`、`resource_kind`、`resource_name`、`source`、`summary`、
`first_seen_at`、`last_seen_at`、`resolved_at`、`version`。

`occurrence_count` 记录重复告警次数；`version` 只用于乐观锁。数据库对相同
fingerprint 的活动 Incident 建部分唯一索引。

状态机：

```text
OPEN -> DIAGNOSING -> DIAGNOSED -> PLAN_READY -> AWAITING_APPROVAL
     -> REMEDIATING -> VERIFYING -> RESOLVED -> CLOSED

DIAGNOSING|REMEDIATING|VERIFYING -> FAILED
FAILED -> DIAGNOSING
OPEN|DIAGNOSING|DIAGNOSED|PLAN_READY|AWAITING_APPROVAL -> CLOSED
PLAN_READY -> DIAGNOSED（计划被策略拒绝或取消）
```

### Task

与 Incident 关联的运维行动项，替代原 Todo。任务不能脱离 Incident 存在。

关键字段：`id`、`incident_id`、`title`、`description`、`task_type`、`priority`、
`status`、`assignee`、`created_by`、`approval_required`、`due_at`。

状态机：

```text
TODO -> IN_PROGRESS -> DONE
TODO|IN_PROGRESS -> BLOCKED
BLOCKED -> IN_PROGRESS
TODO|IN_PROGRESS|BLOCKED -> CANCELLED
```

### Plan

不可直接执行的修复意图。包含动作类型、目标、风险、dry-run 结果、验证规范和回滚
说明。允许的动作来自固定 Action Catalog，不接受 LLM 生成的任意命令。

状态机：

```text
DRAFT -> DRY_RUN_READY -> AWAITING_APPROVAL -> APPROVED -> EXECUTABLE -> EXECUTED
AWAITING_APPROVAL -> REJECTED
DRAFT|DRY_RUN_READY|AWAITING_APPROVAL|APPROVED -> CANCELLED
```

### Execution

一次实际动作尝试。通过 `idempotency_key` 防止重复执行，执行记录只追加结果，
不能被复用为第二次操作。

状态机：

```text
PREPARED -> RUNNING -> SUCCEEDED
                   -> FAILED -> ROLLED_BACK
SUCCEEDED -> VERIFICATION_FAILED -> ROLLED_BACK
```

### AgentRun

一次诊断编排。第一阶段固定 `mode=DIAGNOSE_ONLY`。

状态机：

```text
QUEUED -> COLLECTING -> DIAGNOSING -> COMPLETED
QUEUED|COLLECTING|DIAGNOSING -> FAILED
```

### Audit

只追加的审计事件，记录实体、事件类型、操作者、结构化上下文和时间。所有状态转换、
审批、执行、验证、回滚和自动策略决定都必须写入 Audit。

### Evidence 与 Outbox

Evidence 保存 AgentRun 采集到的脱敏、限长、只追加事实，诊断结果只能引用已持久化
Evidence ID。Outbox 与 AgentRun/Execution 同事务创建，由独立 Dispatcher 使用稳定
RQ job ID 投递，避免 API 提交成功后队列消息丢失。

## 4. API 约定

- `POST /api/incidents`、`GET /api/incidents`、`GET /api/incidents/{id}`
- `POST /api/incidents/{id}/agent-runs`、`GET /api/agent-runs/{id}`
- `POST /api/tasks`、`GET /api/tasks`、`PATCH /api/tasks/{id}`
- `POST /api/incidents/{id}/plans`、`POST /api/plans/{id}/dry-run`
- `POST /api/plans/{id}/approve`、`POST /api/plans/{id}/reject`
- `POST /api/plans/{id}/execute`、`GET /api/executions/{id}`
- `GET /api/audit-events`
- `GET /live` 只检查进程；`GET /ready` 检查 PostgreSQL 和 Redis。
- `GET /metrics` 暴露应用、AgentRun、审批和执行指标。

所有创建接口支持 `Idempotency-Key`。状态转换接口拒绝非法转换并返回 `409`。

## 5. 安全策略

- 自动动作仅允许重建由 Deployment 或 StatefulSet 管理的异常 Pod。
- 系统命名空间、无控制器 Pod、非白名单命名空间和健康 Pod 永远禁止自动执行。
- 所有动作先 dry-run；高风险或策略不确定的计划必须人工审批。
- 审批时保存计划摘要，执行前再次核对目标和资源 UID，防止目标漂移。
- LLM 输入必须限长、脱敏并标记为不可信证据；输出必须通过固定 Schema 校验。

## 6. 渐进交付与回滚

1. 领域模型、状态机、Task API 和只诊断 AgentRun。
2. 应用、Redis 和 Agent 指标接入 kube-prometheus-stack。
3. Pod 重建 Plan、dry-run、审批、Execution 和审计，默认不执行。
4. 加入执行后验证；验证失败停止自动流程并创建人工 Task。
5. 仅对白名单、低风险、控制器管理的异常 Pod开放自动修复。
6. 将 Todo 靶场迁移到 `examples/demo-app/` 并完成故障演练。

每一阶段可通过功能开关关闭；数据库迁移只前向添加，在阶段验收前不删除兼容数据。
