import os
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse

from app.api import create_api
from app.bdpan import Bdpan
from app.tasks import TaskQueue

WEB_INDEX = Path(__file__).resolve().parent.parent / "web" / "index.html"


def create_app(
    api_key: str,
    bdpan_bin: str = "bdpan",
    tmp_dir: Optional[str] = None,
    download_dir: str = "downloads",
    tasks_file: Optional[str] = None,
) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(WEB_INDEX, media_type="text/html; charset=utf-8")

    os.makedirs(download_dir, exist_ok=True)
    bdpan = Bdpan(bdpan_bin)
    app.mount("/api", create_api(bdpan, api_key, TaskQueue(bdpan, download_dir, tasks_file), tmp_dir))
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
