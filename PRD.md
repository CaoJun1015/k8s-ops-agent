# PRD：K8s Todo 应用

## 1. 项目背景

**目标**：创建一个可展示的 Todo（待办事项）Web 应用，用 Docker + Kubernetes 部署，作为简历项目和 GitHub 作品集。

**学习目标**：
- 掌握容器化应用的完整流程（开发→打包→部署）
- 理解 K8s 核心概念（Pod、Deployment、Service、ConfigMap、Secret）
- 学会编写部署文档和架构说明

**目标用户**：面试官（查看 GitHub 项目）、自己（复习和参考）

---

## 2. 功能需求

### 2.1 核心功能（MVP）

| 功能 | 描述 | 优先级 |
|------|------|--------|
| 添加待办 | 用户输入任务，点击添加 | P0 |
| 查看列表 | 显示所有待办事项 | P0 |
| 完成待办 | 点击标记为已完成 | P0 |
| 删除待办 | 点击删除某条待办 | P0 |
| 数据持久化 | 使用 Redis 存储，容器重启数据不丢 | P0 |

### 2.2 扩展功能（Nice to Have）

| 功能 | 描述 | 优先级 |
|------|------|--------|
| 编辑待办 | 修改已有待办内容 | P1 |
| 分类/标签 | 给待办添加分类 | P1 |
| API 文档 | Swagger/OpenAPI 文档 | P2 |

---

## 3. 技术架构

### 3.1 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 前端 | HTML + CSS + JavaScript | 简单的单页面，不需要框架 |
| 后端 | Python Flask | RESTful API |
| 数据库 | Redis | 键值存储，存待办数据 |
| 反向代理 | Nginx | 静态文件服务 + API 代理 |
| 容器化 | Docker | 打包应用为镜像 |
| 编排 | Docker Compose | 本地多容器运行 |
| 部署 | Kubernetes | 生产级部署 |

### 3.2 架构图

```
┌─────────────────────────────────────────────────────────┐
│                     用户浏览器                           │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│                   Nginx (端口 80)                        │
│              静态文件 + API 反向代理                      │
└─────────┬───────────────────────────────────┬───────────┘
          │                                   │
          ▼                                   ▼
┌─────────────────────┐           ┌─────────────────────┐
│   Flask API (5000)  │           │   静态文件 (HTML)    │
│   /api/todos        │           │   index.html        │
│   GET/POST/PUT/DEL  │           │   style.css         │
└─────────┬───────────┘           │   app.js            │
          │                       └─────────────────────┘
          ▼
┌─────────────────────┐
│   Redis (6379)      │
│   存储待办数据       │
└─────────────────────┘
```

### 3.3 K8s 部署架构

```
ConfigMap (todo-config) ──┐
                          ├──→ Deployment (todo-app, 3副本)
Secret (todo-secret) ─────┘         │
                                    ▼
                              Service (todo-svc, NodePort 30080)
                                    │
                                    ▼
                              Deployment (redis, 1副本)
                                    │
                                    ▼
                              Service (redis-svc, ClusterIP)
```

---

## 4. API 设计

### 4.1 接口列表

| 方法 | 路径 | 说明 | 请求体 | 响应 |
|------|------|------|--------|------|
| GET | /api/todos | 获取所有待办 | - | [{id, title, done}] |
| POST | /api/todos | 添加待办 | {title} | {id, title, done} |
| PUT | /api/todos/:id | 更新待办 | {title, done} | {id, title, done} |
| DELETE | /api/todos/:id | 删除待办 | - | {success: true} |
| GET | /health | 健康检查 | - | {status: "ok"} |

### 4.2 数据结构

```json
{
  "id": "uuid",
  "title": "学习 K8s",
  "done": false,
  "created_at": "2026-06-29T10:00:00Z"
}
```

---

## 5. 文件结构

```
k8s-todo-app/
├── README.md                    # 项目说明文档
├── PRD.md                       # 本文件
├── app/
│   ├── app.py                   # Flask 后端
│   ├── requirements.txt         # Python 依赖
│   └── templates/
│       └── index.html           # 前端页面
├── nginx/
│   └── nginx.conf               # Nginx 配置
├── docker/
│   ├── Dockerfile               # Flask 应用镜像
│   ├── Dockerfile.nginx         # Nginx 镜像
│   └── docker-compose.yml       # 本地编排
├── k8s/
│   ├── configmap.yaml           # ConfigMap
│   ├── secret.yaml              # Secret
│   ├── app-deployment.yaml      # Flask Deployment
│   ├── app-service.yaml         # Flask Service
│   ├── redis-deployment.yaml    # Redis Deployment
│   └── redis-service.yaml       # Redis Service
└── docs/
    ├── architecture.md          # 架构说明
    └── screenshots/             # 运行截图
```

---

## 6. 部署方式

### 6.1 Docker Compose（本地开发）

```bash
# 克隆项目
git clone https://github.com/CaoJun1015/k8s-todo-app.git
cd k8s-todo-app

# 启动
docker compose up -d

# 访问
http://localhost:80
```

### 6.2 Kubernetes（生产部署）

```bash
# 导入镜像到 minikube
docker save todo-app -o /tmp/todo-app.tar
minikube image load /tmp/todo-app.tar

# 部署
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/redis-service.yaml
kubectl apply -f k8s/app-deployment.yaml
kubectl apply -f k8s/app-service.yaml

# 访问
minikube service todo-svc --url
```

---

## 7. 验收标准

### 7.1 功能验收

- [ ] 用户可以通过浏览器添加待办
- [ ] 用户可以查看所有待办列表
- [ ] 用户可以标记待办为已完成
- [ ] 用户可以删除待办
- [ ] 容器重启后数据不丢（Redis 持久化）

### 7.2 技术验收

- [ ] Dockerfile 构建成功，镜像体积 < 200MB
- [ ] docker-compose up 一键启动，无报错
- [ ] K8s 部署成功，3 个副本运行正常
- [ ] ConfigMap 和 Secret 正确注入环境变量
- [ ] Service 可以正常访问

### 7.3 文档验收

- [ ] README.md 包含项目介绍、架构图、部署步骤
- [ ] 有运行截图
- [ ] 有 K8s 资源说明

---

## 8. 项目价值

### 8.1 简历包装

**项目名称**：基于 Kubernetes 的 Web 应用容器化部署

**技术栈**：Python Flask、Redis、Docker、Kubernetes、Nginx

**项目描述**：
- 使用 Flask 开发 RESTful API，实现待办事项的增删改查
- 使用 Docker 容器化应用，编写 Dockerfile 优化镜像体积
- 使用 Docker Compose 编排 Flask + Redis + Nginx 多容器应用
- 使用 Kubernetes 部署，配置 Deployment（3副本）、Service、ConfigMap、Secret
- 编写完整的部署文档，支持一键部署

### 8.2 面试话术

"我做了一个 Todo 应用，用 Docker 打包，K8s 部署。前端是简单的 HTML，后端是 Flask，数据存 Redis。K8s 里用了 Deployment 跑 3 个副本，Service 做负载均衡，ConfigMap 存配置，Secret 存密码。整个项目从开发到部署都是我独立完成的。"

---

## 9. 里程碑

| 阶段 | 内容 | 预计时间 |
|------|------|---------|
| M1 | Flask 应用开发 + 本地运行 | 1 小时 |
| M2 | Dockerfile + docker-compose | 30 分钟 |
| M3 | K8s YAML 编写 + 部署 | 30 分钟 |
| M4 | README + 截图 + 推送 GitHub | 30 分钟 |

**总计**：约 2.5 小时

---

*PRD 创建时间：2026-06-29*
