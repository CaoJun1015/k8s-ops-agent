# 上下文记忆与模型预算验收记录

日期：2026-09-10。对应 [7 张实施任务卡](context-memory-task-cards.md)。
代码和文档保存在工作区，未创建 Git 提交。

## 实现结果

- 新增运行内确定性工作记忆与上下文 v2，复用 AgentStep 快照，无数据库迁移。
- 保留关键结构化事实、最新工具结果和有界日志片段；重复内容合并，省略与冲突显式标记。
- 使用完整请求的保守字节估算，统一输入预算、输出预留及安全余量。
- 请求前持久化调用次数和预算占用；收到 usage 后结算，失败解析同样计费，未知消耗保留预留。
- 模型只引用本轮可见证据，规则只引用实际读取的当前有效证据；策略拒绝不能通过降级绕过。
- API、RQ Worker、Compose 和 Kubernetes 配置入口同步；无新增依赖和集群写权限。

## 验证结果

| 环境与命令 | 结果 |
|---|---|
| Windows `.venv/Scripts/python.exe -m pytest app/tests -q` | **140 passed in 9.71s** |
| Windows `.venv/Scripts/python.exe -m pytest app/tests/test_context_memory.py -q` | **31 passed in 3.72s** |
| Windows `.venv/Scripts/python.exe -m pytest examples/demo-app/tests -q` | **13 passed in 0.65s** |
| Ubuntu WSL `docker compose -f docker/docker-compose.yml config --quiet`，使用临时占位环境变量 | 退出码 0；仅提示已有 `version` 属性过时 |
| Ubuntu WSL `bash ci/kind-e2e.sh` | 首轮和最终代码轮均退出码 0 |
| Ubuntu WSL `kind get clusters`，最终验证后 | `No kind clusters found.` |

kind 使用专用 `ops-agent-e2e` 集群。启动前已确认不存在同名或其他 kind 集群；验证结束后
清理该测试集群。脚本构建当前工作区应用镜像，在真实 Kubernetes、PostgreSQL、Redis/RQ
链路上验证只读调查、多轮 Evidence 持久化、CRASH_LOOP 诊断、资源恢复、功能开关回退和
可丢弃数据库的 `0006 → 0005 → 0006`。新增断言直接读取数据库步骤快照，确认 v2 工作记忆
与证据引用，并检查审计中的上下文版本和保留数量；已有 RBAC 断言要求 Agent 删除 Pod 权限为 `no`。

最终轮日志已观察到：

```text
context v2: persisted working memory and evidence references verified
```

随后进程成功退出，集群清理检查通过。日志写入 WSL `/tmp`，进程结束后的再次查询已不存在，
本记录保留工具返回的退出码和运行中观察结果，不提供不存在的原始日志下载链接。

## 关键回归覆盖

31 项新增参数化检查覆盖：大日志关键事实、重复内容、稳定序列化、UTF-8 中文和英文请求计数、
固定上下文不可容纳、无效模型配置、非正预算、输出预留一致性、无效 TTL、过期与未知时间、
同名不同 UID、跨运行隔离、相同时间观测冲突、模型解析与 Schema 错误、未知引用、存在但未展示
的证据引用、失败调用计费、未知 usage、实际超预算、模型多轮看到新增证据、取消期间不执行工具、
策略拒绝不回退重试以及上下文失败后的运行收尾。

固定大日志场景中，旧式单条截断后的整体证据超过 10,000 字节，上下文发生整体截断；新构建器
在同一字节上限内仍保留 OOMKilled 结构化事实，并记录省略数量。该比较验证关键证据保留，
不代表真实模型诊断准确率提升。

## 已知限制

- 模型决策和失败响应通过模拟客户端验证；未调用真实 LLM API，也未验证真实模型效果或账单。
- 使用保守 UTF-8 字节估算，没有增加 tokenizer；预算占用可能包含未知调用的预留，不等同实际费用。
- 日志摘要是关键词与末尾片段启发式；冲突检测只覆盖相同类型、UID、采集时间的指定结构化字段。
- 无历史案例检索、独立记忆表、多 Agent 或自动修复扩展。旧 v1 步骤快照保留原样。
