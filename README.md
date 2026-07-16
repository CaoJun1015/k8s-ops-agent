# K8s Ops Agent

基于 Kubernetes 的智能运维 Agent，从传统脚本巡检进化到 AI 辅助运维。

## 项目简介

一个完整的 K8s 运维自动化工具集，覆盖日常巡检、日志分析、故障自愈、告警通知等场景。支持接入 LLM（OpenAI/Claude）实现 AI 日志分析和根因定位。

## 架构

```
传统运维：手动执行命令 → 人眼看结果 → 人处理
本项目：  Agent自动采集 → AI分析根因 → 自动修复 → 通知人
```

```
┌─────────────────────────────────────────────┐
│              K8s Ops Agent                   │
├─────────────────────────────────────────────┤
│  ops/                                       │
│  ├── daily-inspection.sh  ← 日常巡检        │
│  ├── log-analyzer.sh      ← 日志分析        │
│  ├── auto-fix.sh          ← 故障自愈        │
│  └── alert.sh             ← 告警通知        │
│                                             │
│  agent/                                     │
│  ├── analyze.sh           ← AI日志分析      │
│  └── report.sh            ← 自然语言报告    │
│                                             │
│  config/                                    │
│  └── threshold.conf       ← 阈值配置        │
│                                             │
│  docs/                                      │
│  ├── architecture.md      ← 架构说明        │
│  └── troubleshooting.md   ← 排查手册        │
└─────────────────────────────────────────────┘
```

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 应用 | Python Flask + Redis | 待办事项Web应用 |
| 容器 | Docker | 应用打包 |
| 编排 | Kubernetes | 生产级部署 |
| 代理 | Nginx | 反向代理 |
| 运维 | Shell脚本 | 巡检/分析/自愈/告警 |
| AI | OpenAI / Claude | 日志分析、根因定位 |
| 方法论 | TDD + 工程化实践 | 参见 docs/ |

## 快速开始

### 1. 部署应用

```bash
# 使用kubectl部署
kubectl apply -f k8s/
```

### 2. 运行巡检

```bash
# 日常巡检
bash ops/daily-inspection.sh

# 日志分析
bash ops/log-analyzer.sh todo-app default

# 故障自愈
bash ops/auto-fix.sh

# AI分析（需要配置API密钥）
export OPENAI_API_KEY=your-key
bash agent/analyze.sh todo-app default
```

### 3. 配置告警

编辑 `config/threshold.conf`：

```ini
[notification]
webhook_url="https://open.feishu.cn/your-webhook"
alert_level="warn"
```

### 4. 定时执行

```bash
# 每天早上8点巡检
crontab -e
0 8 * * * /path/to/ops/daily-inspection.sh

# 每5分钟自愈检查
*/5 * * * * /path/to/ops/auto-fix.sh
```

## 巡检覆盖项

| 检查项 | 说明 |
|--------|------|
| 节点状态 | NotReady检测 |
| 异常Pod | CrashLoopBackOff / ImagePullBackOff |
| 高重启Pod | 重启次数超过阈值 |
| 资源使用 | CPU/内存Top排行 |
| 应用健康 | Service/Deployment状态 |
| Warning事件 | 最近集群告警事件 |
| 日志分析 | ERROR/WARN统计、关键词提取 |
| AI根因分析 | LLM分析日志给出修复建议 |
| 故障自愈 | 自动重启异常Pod、清理垃圾 |
| 告警通知 | 飞书/钉钉/企业微信 |

## 项目文档

- [架构说明](docs/architecture.md)
- [故障排查手册](docs/troubleshooting.md)
- [工程化方法论](docs/engineering-practice.md)

## License

MIT
