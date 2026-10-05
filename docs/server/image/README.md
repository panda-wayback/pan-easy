# Docker 镜像

## 目标

把 API 服务与固定版本 bdpan 打包进同一镜像，支持双架构部署。

## 需求

- 基于精简 Linux 基础镜像，构建时安装固定版本 bdpan 并校验 SHA256，校验失败则构建失败
- API 服务与 bdpan 在同一个镜像里运行
- 支持 linux/amd64 与 linux/arm64
- 镜像内不包含任何 bdpan 凭据
- 容器以非 root 用户运行
- `/data` 为持久卷：bdpan 配置写入 `/data/bdpan`，任务历史写入 `/data/tasks.json`；`/downloads` 为下载目录卷
- 未配置访问密钥时拒绝启动并输出原因
- 不提供 bdpan 更新能力；升级 bdpan 只通过重建镜像

## 方案

- 复用：顶层 [`interface.yaml`](../../../interface.yaml) 的镜像相关 input 与 contract
- 外部依赖：`ext:docker`

## 不做

- 运行时更新 bdpan
