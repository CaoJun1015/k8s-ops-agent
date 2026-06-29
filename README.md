# K8s Todo App

基于 Kubernetes 的 Web 应用容器化部署项目

## 项目简介

一个简单的待办事项（Todo）Web 应用，使用 Docker 容器化，Kubernetes 部署。展示了从开发到部署的完整流程。

## 技术栈

| 层 | 技术 | 说明 |
|----|------|------|
| 前端 | HTML + CSS + JavaScript | 单页面应用 |
| 后端 | Python Flask | RESTful API |
| 数据库 | Redis | 键值存储 |
| 反向代理 | Nginx | 静态文件 + API 代理 |
| 容器化 | Docker | 应用打包 |
| 编排 | Docker Compose | 本地多容器运行 |
| 部署 | Kubernetes | 生产级部署 |

## 架构图

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

## 功能特性

- ✅ 添加待办事项
- ✅ 查看待办列表
- ✅ 标记待办为已完成
- ✅ 删除待办事项
- ✅ 数据持久化（Redis）
- ✅ 健康检查接口
- ✅ 响应式设计

## API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | /api/todos | 获取所有待办 |
| POST | /api/todos | 添加待办 |
| PUT | /api/todos/:id | 更新待办 |
| DELETE | /api/todos/:id | 删除待办 |
| GET | /health | 健康检查 |

## 快速开始

### 方式一：Docker Compose（推荐）

```bash
# 克隆项目
git clone https://github.com/CaoJun1015/k8s-todo-app.git
cd k8s-todo-app/docker

# 启动服务
docker compose up -d

# 访问应用
open http://localhost
```

### 方式二：Kubernetes

```bash
# 克隆项目
git clone https://github.com/CaoJun1015/k8s-todo-app.git
cd k8s-todo-app

# 构建镜像
docker build -t todo-app:v1 -f docker/Dockerfile app/

# 导入到 minikube
docker save todo-app:v1 -o /tmp/todo-app.tar
minikube image load /tmp/todo-app.tar

# 部署到 K8s
kubectl apply -f k8s/configmap.yaml
kubectl apply -f k8s/secret.yaml
kubectl apply -f k8s/redis-deployment.yaml
kubectl apply -f k8s/redis-service.yaml
kubectl apply -f k8s/app-deployment.yaml
kubectl apply -f k8s/app-service.yaml

# 查看状态
kubectl get all

# 访问应用
minikube service todo-svc --url
```

## 项目结构

```
k8s-todo-app/
├── README.md                    # 项目说明
├── PRD.md                       # 产品需求文档
├── app/
│   ├── app.py                   # Flask 后端
│   ├── requirements.txt         # Python 依赖
│   └── templates/
│       └── index.html           # 前端页面
├── nginx/
│   └── nginx.conf               # Nginx 配置
├── docker/
│   ├── Dockerfile               # Flask 镜像
│   ├── Dockerfile.nginx         # Nginx 镜像
│   └── docker-compose.yml       # 本地编排
└── k8s/
    ├── configmap.yaml           # ConfigMap
    ├── secret.yaml              # Secret
    ├── app-deployment.yaml      # Flask Deployment
    ├── app-service.yaml         # Flask Service
    ├── redis-deployment.yaml    # Redis Deployment
    └── redis-service.yaml       # Redis Service
```

## K8s 资源说明

| 资源 | 名称 | 作用 |
|------|------|------|
| ConfigMap | todo-config | 存储 Redis 连接配置 |
| Secret | todo-secret | 存储 Redis 密码 |
| Deployment | todo-app | Flask 应用（3 副本） |
| Deployment | redis | Redis 数据库（1 副本） |
| Service | todo-svc | Flask 应用访问入口（NodePort 30080） |
| Service | redis-svc | Redis 内部访问入口（ClusterIP） |

## 学习收获

通过这个项目，我掌握了：

1. **Docker 容器化**：编写 Dockerfile，优化镜像体积
2. **Docker Compose**：多容器应用编排
3. **Kubernetes 核心概念**：Pod、Deployment、Service、ConfigMap、Secret
4. **微服务架构**：前后端分离，服务间通信
5. **部署流程**：从开发到生产的完整流程

## 面试话术

"我做了一个 Todo 应用，用 Docker 打包，K8s 部署。前端是简单的 HTML，后端是 Flask，数据存 Redis。K8s 里用了 Deployment 跑 3 个副本，Service 做负载均衡，ConfigMap 存配置，Secret 存密码。整个项目从开发到部署都是我独立完成的。"

## 作者

**曹骏** - GitHub: [CaoJun1015](https://github.com/CaoJun1015)

## 许可证

MIT License
