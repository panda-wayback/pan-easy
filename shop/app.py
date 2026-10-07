import hashlib
import hmac
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

PAGE = Path(__file__).resolve().parent / "index.html"
PAGE_TTL = 86400
_LINK_MIN = 60
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SHARE_RE = re.compile(r"pan\.baidu\.com/s/", re.IGNORECASE)
_CARD_ERRORS = {
    "CODE_USED": ("card_used", "卡密次数已用完"),
    "CODE_TYPE_MISMATCH": ("card_type_mismatch", "该卡密不是按次数卡密，不能用于本服务"),
    "BATCH_DISABLED": ("card_disabled", "卡密已停用"),
    "PRODUCT_DISABLED": ("card_disabled", "卡密已停用"),
}
_DL_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges",
               "content-disposition", "etag", "last-modified")


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": {"code": code, "message": message}}, status_code=status)


def _unavailable() -> JSONResponse:
    return _error("upstream_unavailable", "下载服务暂时不可用，请稍后再试", 502)


def _not_found() -> JSONResponse:
    return _error("task_not_found", "任务不存在", 404)


def _page_sig(api_key: str, task_id: str, exp: int) -> str:
    # 前缀与 baidu-easy 下载链接签名区分，避免页面凭证被当作下载签名使用
    message = f"shop-page.{task_id}.{exp}".encode()
    return hmac.new(api_key.encode(), message, hashlib.sha256).hexdigest()[:32]


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="seconds")


def _public_origin(request: Request) -> tuple[str, str]:
    def first(name: str) -> str:
        return request.headers.get(name, "").split(",")[0].strip()

    proto = first("x-forwarded-proto").lower()
    if proto not in ("http", "https"):
        proto = request.url.scheme
    host = first("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return proto, host


def create_app(
    api_key: str,
    upstream_url: str,
    auth_url: str,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    auth_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
    client = httpx.AsyncClient(base_url=upstream_url, transport=transport, timeout=60.0)
    auth = httpx.AsyncClient(base_url=auth_url, transport=auth_transport, timeout=15.0)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        await client.aclose()
        await auth.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    async def redeem(code: str) -> tuple[Optional[int], Optional[JSONResponse]]:
        """核销按次数卡密 1 次，返回剩余次数。"""
        try:
            resp = await auth.post("/api/redeem", json={"code": code})
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            data = None
        if not isinstance(data, dict):
            return None, _error("auth_unavailable", "卡密服务暂不可用，请稍后再试", 502)
        if data.get("ok") is True and isinstance(data.get("remaining"), int):
            return data["remaining"], None
        error = data.get("error")
        if data.get("ok") is not False or not isinstance(error, dict):
            return None, _error("auth_unavailable", "卡密服务暂不可用，请稍后再试", 502)
        code_name, message = _CARD_ERRORS.get(error.get("code"), ("card_invalid", "卡密无效"))
        return None, _error(code_name, message, 403)

    async def forward(method: str, path: str, body: bytes = b"",
                      extra_headers: Optional[dict[str, str]] = None) -> JSONResponse:
        headers = {"Authorization": f"Bearer {api_key}", **(extra_headers or {})}
        if body:
            headers["Content-Type"] = "application/json"
        try:
            resp = await client.request(method, path, content=body or None, headers=headers)
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            return _unavailable()
        if resp.status_code == 401:
            return _unavailable()
        return JSONResponse(data, status_code=resp.status_code)

    def open_page(token: str) -> tuple[Optional[str], int, Optional[JSONResponse]]:
        """校验专属页面凭证 {task_id}.{exp}.{sig}，返回任务 ID 与过期时间。"""
        task_id, _, rest = token.partition(".")
        exp_text, _, sig = rest.partition(".")
        if not (_TASK_ID_RE.match(task_id) and exp_text.isdigit()):
            return None, 0, _error("page_not_found", "页面不存在", 404)
        exp = int(exp_text)
        if not hmac.compare_digest(sig.encode(), _page_sig(api_key, task_id, exp).encode()):
            return None, 0, _error("page_not_found", "页面不存在", 404)
        if exp - time.time() < _LINK_MIN:
            return None, 0, _error("page_expired", "页面已过期，请重新提交", 410)
        return task_id, exp, None

    @app.get("/")
    async def home():
        return FileResponse(PAGE, media_type="text/html")

    @app.get("/t/{token}")
    async def task_page(token: str):
        return FileResponse(PAGE, media_type="text/html")

    @app.post("/tasks")
    async def submit_task(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            return _error("invalid_argument", "请求格式错误", 400)
        text, code = payload.get("text"), payload.get("code")
        if not isinstance(text, str) or not _SHARE_RE.search(text):
            return _error("invalid_argument", "文字中没有找到百度网盘分享链接", 400)
        if not isinstance(code, str) or not code.strip():
            return _error("invalid_argument", "请输入卡密", 400)
        remaining, err = await redeem(code)
        if err:
            return err
        resp = await forward("POST", "/api/tasks", json.dumps({"text": text}).encode())
        if resp.status_code != 202:
            return resp
        body = json.loads(resp.body)
        data = body.get("data")
        task_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
            return _unavailable()
        exp = int(time.time()) + PAGE_TTL
        data["page"] = f"/t/{task_id}.{exp}.{_page_sig(api_key, task_id, exp)}"
        data["page_expires_at"] = _iso(exp)
        data["remaining"] = remaining
        return JSONResponse(body, status_code=202)

    @app.get("/t/{token}/task")
    async def page_task(token: str):
        task_id, _, err = open_page(token)
        if err:
            return err
        return await forward("GET", f"/api/tasks/{task_id}")

    @app.post("/t/{token}/link")
    async def page_link(token: str, request: Request):
        task_id, exp, err = open_page(token)
        if err:
            return err
        proto, host = _public_origin(request)
        body = json.dumps({"expires_in": exp - int(time.time())}).encode()
        return await forward("POST", f"/api/tasks/{task_id}/link", body,
                             {"X-Forwarded-Proto": proto, "X-Forwarded-Host": host})

    @app.api_route("/dl/{task_id}", methods=["GET", "HEAD"])
    async def download(task_id: str, request: Request):
        if not _TASK_ID_RE.match(task_id):
            return _not_found()
        path = f"/dl/{task_id}" + (f"?{request.url.query}" if request.url.query else "")
        headers = {k: v for k in ("range", "if-range") if (v := request.headers.get(k))}
        try:
            upstream = await client.send(client.build_request(request.method, path, headers=headers), stream=True)
        except httpx.HTTPError:
            return _unavailable()
        out = {k: upstream.headers[k] for k in _DL_HEADERS if k in upstream.headers}
        if request.method == "HEAD":
            await upstream.aclose()
            return Response(status_code=upstream.status_code, headers=out)
        return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                 headers=out, background=BackgroundTask(upstream.aclose))

    return app


def _parse_addr(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host or "0.0.0.0", int(port)


def main() -> None:
    api_key = os.environ.get("BAIDU_EASY_API_KEY", "")
    auth_url = os.environ.get("SPARK_AUTH_URL", "")
    if not api_key or not auth_url:
        print("shop: 未配置 BAIDU_EASY_API_KEY 或 SPARK_AUTH_URL，拒绝启动", file=sys.stderr)
        sys.exit(1)
    host, port = _parse_addr(os.environ.get("SHOP_ADDR", ":8081"))
    upstream = os.environ.get("BAIDU_EASY_URL", "http://127.0.0.1:8080")

    import uvicorn

    uvicorn.run(create_app(api_key, upstream, auth_url), host=host, port=port)


if __name__ == "__main__":
    main()
