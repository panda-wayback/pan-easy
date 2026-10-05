# 网盘 API

## 目标

让其他程序通过 HTTP 完成百度网盘文件操作。

## 需求

- 操作：查看列表、搜索、转存、分享、建目录、移动、复制、重命名、删除、查看登录状态；上传、下载见 [文件传输](../file-transfer/README.md)
- 调用方必须携带访问密钥；没有密钥或密钥错误时拒绝请求
- 统一 JSON 格式：成功时透传 bdpan 的结果（含保存路径、查看链接）；失败时带 bdpan 的错误码和原因
- 网盘范围沿用 bdpan 默认限制，只操作“我的应用数据/bdpan”
- 一个容器对应一个百度网盘账号
- 未登录返回明确的“未登录”错误并提示登录；Token 失效返回“需要重新登录”

## 方案

- 复用：`app/api` 的各 `/api/*` 调用名，见 [`app/api/interface.yaml`](../../../app/api/interface.yaml)
- 复用：`app/bdpan` 的白名单子命令适配，见 [`app/bdpan/interface.yaml`](../../../app/bdpan/interface.yaml)
- 登录流程见 [登录](../login/README.md)；后台分享下载见 [分享下载](../share-download/README.md)

## 不做

- 面向普通用户的完整网盘界面
- 会员、AI PPT 等非文件操作
