# 登录

## 目标

通过 API 完成一次性授权登录，重建容器或升级镜像后登录状态保留。

## 需求

- 分两步：获取授权链接、提交授权码
- 调用方必须显式确认 bdpan 安全须知
- 授权码错误或过期时返回原因，需要重新获取授权链接
- 只需登录一次；登录状态在重建容器、升级镜像后保留
- 凭据只存放在持久化存储里，不打进镜像，也不通过 API 返回

## 方案

- 复用：`app/api` 的 `POST /api/login/url` 与 `POST /api/login/code`，见 [`app/api/interface.yaml`](../../../app/api/interface.yaml)
- bdpan 配置写入 `/data/bdpan` 持久卷
