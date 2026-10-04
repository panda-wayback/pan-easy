import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import create_api, create_dl
from app.bdpan import Bdpan
from app.tasks import TaskQueue
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


def main() -> None:
    api_key = os.environ.get("BAIDU_EASY_API_KEY", "")
    if not api_key:
        print("baidu-easy: 未配置访问密钥 BAIDU_EASY_API_KEY，拒绝启动", file=sys.stderr)
        sys.exit(1)
    host, port = _parse_addr(os.environ.get("BAIDU_EASY_ADDR", ":8080"))
    bdpan_bin = os.environ.get("BAIDU_EASY_BDPAN_BIN", "bdpan")
    download_dir = os.environ.get("BAIDU_EASY_DOWNLOAD_DIR", "downloads")
    tasks_file = os.environ.get("BAIDU_EASY_TASKS_FILE", "tasks.json")

    import uvicorn

    app = create_app(api_key, bdpan_bin, download_dir=download_dir, tasks_file=tasks_file)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
