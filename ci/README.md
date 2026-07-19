# CI/CD 配置说明

## 文件列表

| 文件 | 用途 | 适用平台 |
|------|------|----------|
| [Jenkinsfile](Jenkinsfile) | Jenkins Pipeline 流水线 | Jenkins |
| [gitlab-ci.yml](gitlab-ci.yml) | GitLab CI/CD 配置 | GitLab |

## 流程说明

```
代码提交 → 单元测试 → 构建镜像 → 部署到 K8s
```

## Jenkins 使用

1. 在 Jenkins 中新建 Pipeline 任务
2. 选择 "Pipeline script from SCM"
3. 配置 Git 仓库地址和分支
4. Jenkinsfile 路径设为 `ci/Jenkinsfile`

## GitLab CI 使用

1. 确保仓库已配置 GitLab Runner
2. 设置 CI/CD Variables：
   - `CI_REGISTRY_USER` / `CI_REGISTRY_PASSWORD`：镜像仓库认证
   - `CI_REGISTRY_IMAGE`：镜像仓库地址
   - `KUBE_CONTEXT`：K8s 集群上下文
3. 推送到 main/master 分支自动触发