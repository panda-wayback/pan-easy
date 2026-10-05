# 文件传输

## 目标

调用方不与容器共享目录，仅凭 HTTP 请求完成文件上传与下载。

## 需求

- 上传：调用方把文件内容放在请求里发送
- 下载：服务把文件内容直接返回给调用方
- 上传、下载为同步请求，暂不做断点续传；下载只支持单个文件
- 服务端经临时目录中转，请求完成后清理临时文件

## 方案

- 复用：`app/api` 的 `POST /api/upload` 与 `GET /api/download`，见 [`app/api/interface.yaml`](../../../app/api/interface.yaml)
- 复用：`app/bdpan` 的 upload / download 子命令，见 [`app/bdpan/interface.yaml`](../../../app/bdpan/interface.yaml)

## 不做

- 与容器共享目录的传输方式
- 同步上传、下载的断点续传
