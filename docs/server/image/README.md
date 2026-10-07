# Docker 镜像

## 目标

把 API 服务与固定版本 bdpan 打包进同一镜像，支持双架构部署。

## 需求

- 基于精简 Linux 基础镜像，构建时安装固定版本 bdpan 并校验 SHA256，校验失败则构建失败
- API 服务与 bdpan 在同一个镜像里运行
- 对外下载服务 shop 也打包在同一镜像：容器内运行两个进程，API 服务监听 8080（管理页与 `/api/*`），shop 监听 8081（对外下载网站），shop 经 `127.0.0.1:8080` 调用 API 服务；两者版本始终一致，部署只需一个服务
- 任一进程退出时容器退出，由 Docker 的重启策略整体重启
- 未配置卡密服务地址 `SPARK_AUTH_URL` 时拒绝启动并输出原因
- 支持 linux/amd64 与 linux/arm64
- 镜像内不包含任何 bdpan 凭据
- 容器以非 root 用户运行
- `/data` 为持久卷：bdpan 配置写入 `/data/bdpan`，任务历史写入 `/data/tasks.json`；`/downloads` 为下载目录卷
- 未配置访问密钥时拒绝启动并输出原因
- 不提供 bdpan 更新能力；升级 bdpan 只通过重建镜像

## 方案

- 复用：顶层 [`interface.yaml`](../../../interface.yaml) 的镜像相关 input 与 contract
- 外部依赖：`ext:docker`
- 部署：compose 中 8080 只绑定宿主机 `127.0.0.1`（管理页只在服务器本机或 SSH 隧道访问），8081 对外暴露
- 取舍：两个进程放一个容器而非两个容器：两者本就配套，合并后版本不会错配、配置只写一次；代价是 shop 与 API 服务不再隔离，可接受

## 不做

- 运行时更新 bdpan
