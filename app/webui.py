"""Server-rendered UI layer (Jinja2 + htmx).

完全独立于 /api JSON 接口：页面与片段在此渲染，认证使用
HttpOnly Cookie ``baidu_easy_key``（同时兼容 Bearer 头，便于测试）。
"""
from __future__ import annotations

import asyncio
import hmac
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates

from app.bdpan import BdpanError
from app.tasks import NoShareLink, TaskQueue

COOKIE_NAME = "baidu_easy_key"
_AUTH_CODE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

_STATUS_LABEL = {
    "queued": "排队中",
    "running": "下载中",
    "done": "完成",
    "failed": "失败",
    "submitted": "转存已提交，尚未完成",
    "interrupted": "已中断",
}
_BYTE_UNITS = ["B", "kB", "MB", "GB", "TB"]


def fmt_bytes(num: Any) -> str:
    try:
        value = float(num)
    except (TypeError, ValueError):
        return ""
    i = 0
    while value >= 1000 and i < len(_BYTE_UNITS) - 1:
        value /= 1000
        i += 1
    return (f"{value:.1f}" if i else f"{int(value)}") + " " + _BYTE_UNITS[i]


def fmt_eta(seconds: Any) -> str:
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return ""
    if s >= 3600:
        return f"{s // 3600}小时{(s % 3600) // 60}分"
    if s >= 60:
        return f"{s // 60}分{s % 60}秒"
    return f"{s}秒"


def _local_dt(value: Any) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return str(value)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S") if dt.tzinfo else value


def _task_title(task: dict[str, Any]) -> str:
    saved = task.get("saved_to")
    if saved and saved != ".":
        return Path(saved).name
    pan = task.get("pan_path")
    if pan:
        return pan.rstrip("/").split("/")[-1]
    return task.get("url", "")


def _can_link(task: dict[str, Any]) -> bool:
    return task.get("status") == "done" and bool(task.get("saved_to")) and task.get("saved_to") != "."


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request", "") == "true"


def _read_logs(lines: int) -> tuple[list[str], Optional[str]]:
    """读取容器标准输出的最近 N 行（容器内 /proc/1/fd/1）。"""
    try:
        result = subprocess.run(
            ["tail", "-n", str(lines), "/proc/1/fd/1"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as err:  # 本地非容器环境等情况
        return [], f"tail 执行失败：{err}"
    if result.returncode != 0:
        return [], result.stderr.strip() or "tail 返回非零状态"
    return result.stdout.splitlines(), None


def _cached_link(api_key: str, task_queue: TaskQueue, request: Request,
                 task_id: str) -> Optional[str]:
    """生成与 /api 一致签名的下载链接；任务不可链接时返回 None。"""
    from app.api import _public_base, _sign, _task_file

    try:
        _task_file(task_queue, task_id)
    except Exception:
        return None
    exp = int(time.time()) + 86400
    return (
        f"{_public_base(request)}/dl/{task_id}?exp={exp}"
        f"&sig={_sign(api_key, task_id, exp)}"
    )


def _supplied_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):]
    return request.cookies.get(COOKIE_NAME, "")


def _side_view(has_valid_key: bool, status: Optional[dict[str, Any]] = None,
               error: Optional[BdpanError] = None) -> dict[str, str]:
    """侧边状态/状态面板共用的展示数据。"""
    if not has_valid_key:
        return {"state_class": "", "state_text": "未设置访问密钥"}
    if error is not None:
        return {"state_class": "err", "state_text": "状态查询失败"}
    if status and status.get("authenticated"):
        return {"state_class": "ok",
                "state_text": "已登录：" + (status.get("username") or "")}
    return {"state_class": "err", "state_text": "未登录"}


def _unauthorized(request: Request) -> Response:
    if _is_htmx(request):
        resp = Response(status_code=204)
        resp.headers["hx-redirect"] = "/login"
        return resp
    return Response(status_code=401)


def _oob(msg_id: str, kind: str, text: str) -> str:
    from markupsafe import escape

    return f'<div class="msg {kind}" id="{msg_id}" hx-swap-oob="true">{escape(text)}</div>'


def create_webui(api_key: str, tasks: TaskQueue) -> FastAPI:
    webui = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))
    templates.env.filters.update(
        fmt_bytes=fmt_bytes,
        fmt_eta=fmt_eta,
        localtime=_local_dt,
        task_title=_task_title,
    )
    templates.env.globals.update(
        status_label=lambda s: _STATUS_LABEL.get(s, s),
        can_link=_can_link,
    )
    login_lock = asyncio.Lock()

    def render(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, context)

    @webui.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        # 页面、状态展示端点、密钥设置/清除入口公开；其余片段必须持正确密钥
        public = ("/", "/logs", "/login", "/session/side-status",
                  "/session/key", "/session/key/clear", "/favicon.ico")
        if path in public:
            request.state.valid_key = False
            key = _supplied_key(request)
            request.state.valid_key = bool(key) and hmac.compare_digest(key, api_key)
            return await call_next(request)
        key = _supplied_key(request)
        if not key or not hmac.compare_digest(key, api_key):
            return _unauthorized(request)
        request.state.valid_key = True
        return await call_next(request)

    # ---- 页面 ---------------------------------------------------------

    async def _current_status() -> tuple[Optional[dict[str, Any]], Optional[BdpanError]]:
        try:
            return await tasks.bdpan.run("whoami"), None
        except BdpanError as err:
            return None, err

    @webui.get("/", response_class=HTMLResponse)
    async def page_download(request: Request):
        task_list = tasks.list()
        has_active = any(t["status"] in ("queued", "running") for t in task_list)
        status, err = await _current_status() if request.state.valid_key else (None, None)
        view = _side_view(request.state.valid_key, status, err)
        return render(
            request, "download.html", active="download",
            has_key=request.state.valid_key,
            tasks=task_list, has_active=has_active,
            link_for=None, link_url=None,
            state_class=view["state_class"], state_text=view["state_text"],
        )

    @webui.get("/logs", response_class=HTMLResponse)
    async def page_logs(request: Request):
        logs, error_log = _read_logs(500) if request.state.valid_key else ([], None)
        status, err = await _current_status() if request.state.valid_key else (None, None)
        view = _side_view(request.state.valid_key, status, err)
        return render(
            request, "logs.html", active="logs",
            auto=False, logs=logs, error_log=error_log,
            state_class=view["state_class"], state_text=view["state_text"],
        )

    @webui.get("/login", response_class=HTMLResponse)
    async def page_login(request: Request):
        if request.state.valid_key:
            status, err = await _current_status()
        else:
            status, err = None, None
        view = _side_view(request.state.valid_key, status, err)
        ctx: dict[str, Any] = {
            "active": "login",
            "state_class": view["state_class"], "state_text": view["state_text"],
            "username": None, "expires_local": None,
            "auth_url": None,
        }
        if request.state.valid_key and err is None and status is not None:
            ctx["state_text"] = "已登录" if status.get("authenticated") else "未登录"
            ctx["username"] = status.get("username")
            ctx["expires_local"] = _local_dt(status.get("expires_at"))
        return render(request, "login.html", **ctx)

    @webui.get("/favicon.ico")
    async def favicon():
        return Response(status_code=204)

    @webui.get("/session/side-status")
    async def session_side_status(request: Request):
        if not request.state.valid_key:
            view = _side_view(False)
            return render(request, "partials/_side_status.html", **view)
        status, err = await _current_status()
        view = _side_view(True, status, err)
        return render(request, "partials/_side_status.html", **view)

    # ---- 任务片段 -----------------------------------------------------

    def _task_list_html(request: Request, link_for: Optional[str] = None) -> str:
        task_list = tasks.list()
        link_url = None
        if link_for:
            link_url = _cached_link(api_key, tasks, request, link_for)
        has_active = any(t["status"] in ("queued", "running") for t in task_list)
        return render(
            request, "partials/_task_list.html",
            tasks=task_list, has_active=has_active,
            link_for=link_for if link_url else None, link_url=link_url,
        ).body.decode()

    @webui.get("/session/tasks")
    async def session_tasks(request: Request, link_for: Optional[str] = Query(None)):
        return HTMLResponse(_task_list_html(request, link_for))

    @webui.post("/session/tasks")
    async def session_submit_task(request: Request, text: str = Form(...)):
        try:
            task = tasks.submit(text)
        except NoShareLink as err:
            html = _task_list_html(request)
            return HTMLResponse(html + _oob("task-msg", "err", str(err)))
        html = _task_list_html(request)
        resp = HTMLResponse(
            html + _oob("task-msg", "ok", "已提交，任务 ID：" + task["id"])
        )
        resp.headers["hx-trigger"] = "task-created"
        return resp

    @webui.delete("/session/tasks/{task_id}")
    async def session_delete_task(request: Request, task_id: str):
        deleted = await tasks.delete(task_id)
        if not deleted:
            html = _task_list_html(request)
            return HTMLResponse(html + _oob("task-msg", "err", "任务不存在"))
        return HTMLResponse(_task_list_html(request))

    @webui.post("/session/tasks/{task_id}/retry")
    async def session_retry_task(request: Request, task_id: str):
        new_task = tasks.retry(task_id)
        if new_task is None:
            html = _task_list_html(request)
            return HTMLResponse(html + _oob("task-msg", "err", "任务不存在或当前状态不可重试"))
        return HTMLResponse(_task_list_html(request))

    @webui.post("/session/tasks/{task_id}/link")
    async def session_task_link(request: Request, task_id: str):
        return HTMLResponse(_task_list_html(request, link_for=task_id))

    @webui.get("/session/tasks/{task_id}/detail")
    async def session_task_detail(request: Request, task_id: str):
        task = tasks.get(task_id)
        if task is None:
            return HTMLResponse('<div class="dialog-body">任务不存在</div>', status_code=404)
        return render(request, "partials/_task_detail.html", t=task)

    # ---- 日志片段 -----------------------------------------------------

    @webui.get("/session/logs")
    async def session_logs(request: Request, auto: Optional[str] = Query(None)):
        logs, error_log = _read_logs(500)
        return render(
            request, "partials/_logs.html",
            auto=bool(auto), logs=logs, error_log=error_log,
        )

    @webui.post("/session/logs/clear")
    async def session_logs_clear(request: Request):
        return render(request, "partials/_logs.html", auto=False, logs=[], error_log=None)

    # ---- 密钥 / 状态面板 / 授权 ---------------------------------------

    def _set_key_cookie(resp: Response) -> None:
        resp.set_cookie(
            COOKIE_NAME, api_key, httponly=True, samesite="lax",
            max_age=60 * 60 * 24 * 365, path="/",
        )

    @webui.post("/session/key")
    async def session_set_key(request: Request, key: str = Form(...)):
        if not hmac.compare_digest(key.strip(), api_key):
            html = render(request, "partials/_side_status.html",
                          state_class="err", state_text="访问密钥无效").body.decode()
            return HTMLResponse(html + _oob("key-msg", "err", "访问密钥无效"))
        view = _side_view(True)
        resp = HTMLResponse(
            render(request, "partials/_side_status.html",
                   state_class=view["state_class"], state_text=view["state_text"]).body.decode()
            + _oob("key-msg", "ok", "已保存，密钥仅保存在本浏览器的加密 Cookie 中")
        )
        _set_key_cookie(resp)
        return resp

    @webui.post("/session/key/clear")
    async def session_clear_key(request: Request):
        resp = HTMLResponse(
            render(request, "partials/_side_status.html",
                   state_class="", state_text="未设置访问密钥").body.decode()
            + _oob("key-msg", "ok", "已清除")
        )
        resp.delete_cookie(COOKIE_NAME, path="/")
        resp.set_cookie(
            COOKIE_NAME, "", path="/", max_age=0,
            expires="Thu, 01 Jan 1970 00:00:00 GMT",
        )
        return resp

    @webui.post("/session/status")
    async def session_status_panel(request: Request):
        try:
            status = await tasks.bdpan.run("whoami")
        except BdpanError:
            return render(
                request, "partials/_status_panel.html",
                state_text="查询失败", username=None, expires_local=None,
            )
        if status.get("authenticated"):
            return render(
                request, "partials/_status_panel.html",
                state_text="已登录", username=status.get("username"),
                expires_local=_local_dt(status.get("expires_at")),
            )
        return render(
            request, "partials/_status_panel.html",
            state_text="未登录", username=None, expires_local=None,
        )

    @webui.post("/session/login/url")
    async def session_login_url(request: Request, accept_disclaimer: Optional[str] = Form(None)):
        if not accept_disclaimer:
            return HTMLResponse(
                render(request, "partials/_login_url.html", auth_url=None).body.decode()
                + _oob("login-msg", "err", "必须先确认 bdpan 安全须知")
            )
        try:
            async with login_lock:
                data = await tasks.bdpan.run(
                    "login", flags=["--get-auth-url", "--accept-disclaimer"]
                )
        except BdpanError as err:
            return HTMLResponse(
                render(request, "partials/_login_url.html", auth_url=None).body.decode()
                + _oob("login-msg", "err", err.message)
            )
        auth_url = data.get("auth_url") if isinstance(data, dict) else None
        return HTMLResponse(
            render(request, "partials/_login_url.html", auth_url=auth_url).body.decode()
            + _oob("login-msg", "ok", "请在打开的页面完成授权，再把授权码粘贴到下面")
        )

    @webui.post("/session/login/code")
    async def session_login_code(request: Request, code: str = Form("")):
        code = code.strip()
        if not _AUTH_CODE_RE.match(code):
            return HTMLResponse(_oob("login-msg", "err", "授权码格式不正确，应为 32 位十六进制字符"))
        async with login_lock:
            try:
                await tasks.bdpan.run(
                    "login", flags=["--set-code-stdin", "--accept-disclaimer"],
                    stdin=code + "\n",
                )
            except BdpanError as err:
                return HTMLResponse(_oob("login-msg", "err", err.message))
        return HTMLResponse(_oob("login-msg", "ok", "登录成功"))

    return webui




