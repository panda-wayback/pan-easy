import logging
import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import create_api, create_dl
from app.bdpan import Bdpan
from app.tasks import TaskQueue, parse_max_task_bytes
from app.webui import create_webui

WEB_STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


def create_app(
    api_key: str,
    bdpan_bin: str = "bdpan",
    tmp_dir: Optional[str] = None,
    download_dir: str = "downloads",
    tasks_file: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    os.makedirs(download_dir, exist_ok=True)
    bdpan = Bdpan(bdpan_bin)
    # 启动时校验上限配置（不允许关闭）
    parse_max_task_bytes()
    tasks = TaskQueue(bdpan, download_dir, tasks_file)
    app.mount("/api", create_api(bdpan, api_key, tasks, tmp_dir))
    app.mount("/dl", create_dl(api_key, tasks))
    app.mount("/static", StaticFiles(directory=str(WEB_STATIC)), name="static")
    # webui 最后挂载：其页面路由作为未匹配路径的页面层兜底
    app.mount("/", create_webui(api_key, tasks))
    return app


def _parse_addr(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host or "0.0.0.0", int(port)


def _configure_logging() -> None:
    """配置应用与 uvicorn 日志统一输出到 stdout，供容器日志页读取。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def main() -> None:
    api_key = os.environ.get("BAIDU_EASY_API_KEY", "")
    if not api_key:
        print("baidu-easy: 未配置访问密钥 BAIDU_EASY_API_KEY，拒绝启动", file=sys.stderr)
        sys.exit(1)
    try:
        parse_max_task_bytes()
    except ValueError as err:
        print(f"baidu-easy: {err}，拒绝启动", file=sys.stderr)
        sys.exit(1)
    host, port = _parse_addr(os.environ.get("BAIDU_EASY_ADDR", ":8080"))
    bdpan_bin = os.environ.get("BAIDU_EASY_BDPAN_BIN", "bdpan")
    download_dir = os.environ.get("BAIDU_EASY_DOWNLOAD_DIR", "downloads")
    tasks_file = os.environ.get("BAIDU_EASY_TASKS_FILE", "tasks.json")

    import uvicorn

    _configure_logging()
    app = create_app(api_key, bdpan_bin, download_dir=download_dir, tasks_file=tasks_file)
    # log_config=None：沿用 _configure_logging 配置的 stdout root logger，避免被 uvicorn 重置到 stderr
    uvicorn.run(app, host=host, port=port, log_config=None)


if __name__ == "__main__":
    main()
