# baidu-easy

## 目标

把百度网盘命令行工具 bdpan 封装成 HTTP API，打包进 Docker 镜像。其他程序通过网络调用就能操作百度网盘，不需要在自己的机器上安装 bdpan。

## 功能

- [Docker 镜像](server/image/README.md)：服务与固定版本 bdpan 打包进同一镜像，支持双架构
- [网盘 API](server/api/README.md)：鉴权后通过 HTTP 完成网盘文件操作
- [登录](server/login/README.md)：通过 API 授权登录，凭据持久化
- [文件传输](server/file-transfer/README.md)：请求体上传、文件流下载，无需共享目录
- [测试网页](server/web/README.md)：服务自带的下载与登录测试网页
- [分享下载](server/share-download/README.md)：粘贴分享文字，后台逐个下载
- [下载空间控制](server/download-space/README.md)：单次下载大小上限与冷文件定时清理，防止下载目录占满磁盘
- [对外下载服务](server/public-service/README.md)：面向付费用户的独立下载网站，凭按次数卡密使用
