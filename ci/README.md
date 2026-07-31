# CI/CD 流水线

两条流水线都执行主应用与 Demo 测试、构建并推送镜像、运行 Alembic Job，然后滚动
更新 API 和 Worker。数据库迁移失败时不会开始 Deployment rollout。

## Jenkins

- 配置凭据 `docker-registry-credentials`。
- 参数 `DOCKER_REGISTRY` 是仓库主机，`DOCKER_IMAGE` 是镜像仓库名。
- Jenkins Agent 需要 Python、Docker、kubectl 和目标集群凭据。

## GitLab CI

需要变量：

- `CI_REGISTRY_USER`、`CI_REGISTRY_PASSWORD`、`CI_REGISTRY_IMAGE`
- `KUBE_CONTEXT`

`deploy` 为手动 Job，只允许 main/master 分支触发。
