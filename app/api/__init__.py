import asyncio
import hashlib
import hmac
import os
import posixpath
import re
import shutil
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Literal, Optional

from fastapi import Body, FastAPI, File, Form, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.bdpan import Bdpan, BdpanError
from app.tasks import NoShareLink, TaskQueue

_AUTH_CODE_RE = re.compile(r"^[0-9a-fA-F]{32}$")
_COPY_CHUNK = 1024 * 1024

_BDPAN_STATUS = {
    "not_logged_in": 409,
    "token_expired": 409,
    "bdpan_error": 502,
    "timeout": 504,
}


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int, errno: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.errno = errno


def _invalid(message: str) -> ApiError:
    return ApiError("invalid_argument", message, 400)


def _error_response(
    code: str, message: str, status: int, errno: Optional[int] = None, hint: Optional[str] = None
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if errno is not None:
        error["errno"] = errno
    if hint:
        error["hint"] = hint
    return JSONResponse({"ok": False, "error": error}, status_code=status)


def _ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "data": data}, status_code=status)


def _check_path(value: str, field: str) -> str:
    if not value or not value.strip():
        raise _invalid(f"{field} 不能为空")
    if value.startswith(("/", "~")) or ".." in value.split("/") or "\x00" in value:
        raise _invalid(f"{field} 必须是相对 我的应用数据/bdpan 的路径，且不能包含 ..")
    return value


def _check_paths(values: list[str], field: str) -> list[str]:
    if not values:
        raise _invalid(f"{field} 不能为空")
    return [_check_path(v, field) for v in values]


def _sign(api_key: str, task_id: str, exp: int) -> str:
    return hmac.new(api_key.encode(), f"{task_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def _task_file(tasks: TaskQueue, task_id: str) -> Path:
    task = tasks.get(task_id)
    if task is None:
        raise ApiError("task_not_found", "任务不存在", 404)
    if task["status"] != "done":
        raise ApiError("task_not_ready", "任务尚未完成，不能生成下载链接", 409)
    saved_to = task.get("saved_to")
    if not saved_to or saved_to == ".":
        raise ApiError("task_not_ready", "任务包含多个文件，暂不支持生成下载链接", 409)
    root = os.path.realpath(tasks.download_dir)
    path = os.path.realpath(os.path.join(root, saved_to))
    if not path.startswith(root + os.sep):
        raise ApiError("file_missing", "任务文件不在下载目录内", 404)
    if not os.path.isfile(path):
        raise ApiError("file_missing", "任务文件已不在下载目录中", 404)
    return Path(path)


class _ApiKeyMiddleware:
    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            supplied = b""
            for name, value in scope.get("headers", []):
                if name == b"authorization" and value.startswith(b"Bearer "):
                    supplied = value[len(b"Bearer "):]
                    break
            if not hmac.compare_digest(supplied, self.api_key):
                response = _error_response("unauthorized", "缺少或错误的访问密钥", 401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


class _CleanupFileResponse(FileResponse):
    def __init__(self, *args, cleanup_dir: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.cleanup_dir = cleanup_dir

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            shutil.rmtree(self.cleanup_dir, ignore_errors=True)


async def _until_disconnect(request: Request, work: Awaitable[Any]) -> Any:
    task = asyncio.ensure_future(work)
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1)
            if done:
                return task.result()
            if await request.is_disconnected():
                raise ApiError("internal", "客户端已断开连接", 500)
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


class LoginUrlBody(BaseModel):
    accept_disclaimer: bool = False


class LoginCodeBody(BaseModel):
    code: str


class TransferBody(BaseModel):
    url: str
    pwd: Optional[str] = None
    dir: Optional[str] = None


class ShareBody(BaseModel):
    paths: list[str]
    period: Literal[0, 1, 7, 30] = 7


class PathBody(BaseModel):
    path: str


class MoveBody(BaseModel):
    src: str
    dst: str


class RenameBody(BaseModel):
    path: str
    name: str


class PathsBody(BaseModel):
    paths: list[str]


class TaskBody(BaseModel):
    text: str


class LinkBody(BaseModel):
    expires_in: int = Field(86400, ge=60, le=604800)


def create_api(bdpan: Bdpan, api_key: str, tasks: TaskQueue, tmp_dir: Optional[str] = None) -> FastAPI:
    api = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    api.add_middleware(_ApiKeyMiddleware, api_key=api_key)
    login_lock = asyncio.Lock()

    @api.exception_handler(ApiError)
    async def _api_error(_: Request, err: ApiError):
        return _error_response(err.code, err.message, err.status, err.errno)

    @api.exception_handler(BdpanError)
    async def _bdpan_error(_: Request, err: BdpanError):
        status = _BDPAN_STATUS.get(err.kind)
        if status is None:
            return _error_response("internal", err.message, 500, err.errno, err.hint)
        return _error_response(err.kind, err.message, status, err.errno, err.hint)

    @api.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, err: RequestValidationError):
        details = "; ".join(
            f"{'.'.join(str(p) for p in e.get('loc', ()) if p != 'body')}: {e.get('msg')}" for e in err.errors()
        )
        return _error_response("invalid_argument", details or "参数错误", 400)

    @api.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, err: StarletteHTTPException):
        return _error_response("invalid_argument", str(err.detail), err.status_code)

    @api.exception_handler(Exception)
    async def _unexpected(_: Request, err: Exception):
        return _error_response("internal", f"服务内部错误：{type(err).__name__}", 500)

    @api.get("/status")
    async def status():
        return _ok(await bdpan.run("whoami"))

    @api.post("/login/url")
    async def login_url(body: LoginUrlBody):
        if not body.accept_disclaimer:
            raise _invalid("必须先确认 bdpan 安全须知（accept_disclaimer=true）")
        async with login_lock:
            return _ok(await bdpan.run("login", flags=["--get-auth-url", "--accept-disclaimer"]))

    @api.post("/login/code")
    async def login_code(body: LoginCodeBody):
        code = body.code.strip()
        if not _AUTH_CODE_RE.match(code):
            raise ApiError("auth_code_invalid", "授权码格式不正确，应为 32 位十六进制字符", 400)
        async with login_lock:
            try:
                data = await bdpan.run("login", flags=["--set-code-stdin", "--accept-disclaimer"], stdin=code + "\n")
            except BdpanError as err:
                if "授权码" in err.message:
                    raise ApiError("auth_code_invalid", err.message, 400, err.errno) from None
                raise
        return _ok(data)

    @api.get("/ls")
    async def ls(
        path: Optional[str] = None,
        order: Optional[Literal["name", "time", "size"]] = None,
        desc: bool = False,
        folder: bool = False,
    ):
        flags: list[str] = []
        if order:
            flags += ["--order", order]
        if desc:
            flags.append("--desc")
        if folder:
            flags.append("--folder")
        positionals = [_check_path(path, "path")] if path else []
        return _ok(await bdpan.run("ls", positionals, flags))

    @api.get("/search")
    async def search(
        q: str,
        category: int = Query(0, ge=0, le=7),
        scope: Literal["all", "no_dir", "dir_only"] = "all",
        page: int = Query(1, ge=1),
        page_size: int = Query(5, ge=1, le=50),
    ):
        if not q.strip():
            raise _invalid("q 不能为空")
        flags = ["--category", str(category), "--page", str(page), "--page-size", str(page_size)]
        if scope == "no_dir":
            flags.append("--no-dir")
        elif scope == "dir_only":
            flags.append("--dir-only")
        return _ok(await bdpan.run("search", [q], flags))

    @api.post("/upload")
    async def upload(request: Request, file: UploadFile = File(...), remote_path: str = Form(...)):
        _check_path(remote_path, "remote_path")
        if remote_path.endswith("/"):
            raise _invalid("remote_path 必须是文件路径，不能以 / 结尾")
        work_dir = Path(tempfile.mkdtemp(prefix="baidu-easy-", dir=tmp_dir))
        try:
            local = work_dir / "upload"
            with local.open("wb") as out:
                while chunk := await file.read(_COPY_CHUNK):
                    out.write(chunk)
            data = await _until_disconnect(request, bdpan.run("upload", [str(local), remote_path]))
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
        return _ok(data)

    @api.get("/download")
    async def download(request: Request, path: str):
        _check_path(path, "path")
        target = path.rstrip("/")
        parent, name = posixpath.split(target)
        listing = await bdpan.run("ls", [parent] if parent else [])
        for item in listing if isinstance(listing, list) else []:
            if isinstance(item, dict) and item.get("server_filename") == name and item.get("isdir"):
                raise _invalid("下载只支持单个文件，目标是目录")

        work_dir = Path(tempfile.mkdtemp(prefix="baidu-easy-", dir=tmp_dir))
        try:
            local = work_dir / name
            await _until_disconnect(request, bdpan.run("download", [target, str(local)]))
            if not local.is_file():
                raise _invalid("下载只支持单个文件，目标是目录")
        except BaseException:
            shutil.rmtree(work_dir, ignore_errors=True)
            raise
        return _CleanupFileResponse(local, filename=name, media_type="application/octet-stream", cleanup_dir=work_dir)

    @api.post("/transfer")
    async def transfer(body: TransferBody):
        if not body.url.startswith(("https://", "http://")):
            raise _invalid("url 必须是百度网盘分享链接")
        flags: list[str] = []
        if body.pwd:
            flags += ["-p", body.pwd]
        if body.dir:
            flags += ["-d", _check_path(body.dir, "dir")]
        data = await bdpan.run("transfer", [body.url], flags)
        if isinstance(data, dict) and data.get("status") == "submitted":
            return _ok(data, 202)
        return _ok(data)

    @api.post("/share")
    async def share(body: ShareBody):
        paths = _check_paths(body.paths, "paths")
        return _ok(await bdpan.run("share", paths, ["--period", str(body.period)]))

    @api.post("/mkdir")
    async def mkdir(body: PathBody):
        return _ok(await bdpan.run("mkdir", [_check_path(body.path, "path")]))

    @api.post("/mv")
    async def mv(body: MoveBody):
        return _ok(await bdpan.run("mv", [_check_path(body.src, "src"), _check_path(body.dst, "dst")]))

    @api.post("/cp")
    async def cp(body: MoveBody):
        return _ok(await bdpan.run("cp", [_check_path(body.src, "src"), _check_path(body.dst, "dst")]))

    @api.post("/rename")
    async def rename(body: RenameBody):
        name = body.name
        if not name or "/" in name or name in (".", ".."):
            raise _invalid("name 必须是新文件名，不能包含 /")
        return _ok(await bdpan.run("rename", [_check_path(body.path, "path"), name]))

    @api.post("/rm")
    async def rm(body: PathsBody):
        paths = _check_paths(body.paths, "paths")
        return _ok(await bdpan.run("rm", paths, ["--force"]))

    @api.post("/tasks")
    async def submit_task(body: TaskBody):
        try:
            return _ok(tasks.submit(body.text), 202)
        except NoShareLink as err:
            raise _invalid(str(err)) from None

    @api.get("/tasks")
    async def list_tasks():
        return _ok(tasks.list())

    @api.get("/tasks/{task_id}")
    async def get_task(task_id: str):
        task = tasks.get(task_id)
        if task is None:
            raise ApiError("task_not_found", "任务不存在", 404)
        return _ok(task)

    @api.post("/tasks/{task_id}/link")
    async def task_link(request: Request, task_id: str, body: Optional[LinkBody] = Body(None)):
        _task_file(tasks, task_id)
        exp = int(time.time()) + (body or LinkBody()).expires_in
        url = f"{request.url.scheme}://{request.url.netloc}/dl/{task_id}?exp={exp}&sig={_sign(api_key, task_id, exp)}"
        expires_at = datetime.fromtimestamp(exp, timezone.utc).astimezone().isoformat(timespec="seconds")
        return _ok({"url": url, "expires_at": expires_at})

    return api


def create_dl(api_key: str, tasks: TaskQueue) -> FastAPI:
    dl = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @dl.exception_handler(ApiError)
    async def _api_error(_: Request, err: ApiError):
        return _error_response(err.code, err.message, err.status, err.errno)

    @dl.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, err: RequestValidationError):
        return _error_response("link_invalid", "下载链接不完整", 403)

    @dl.api_route("/{task_id}", methods=["GET", "HEAD"])
    async def download_link(task_id: str, exp: int, sig: str):
        if not hmac.compare_digest(sig.encode(), _sign(api_key, task_id, exp).encode()):
            raise ApiError("link_invalid", "下载链接无效", 403)
        if exp < time.time():
            raise ApiError("link_invalid", "下载链接已过期，请重新生成", 403)
        path = _task_file(tasks, task_id)
        return FileResponse(path, filename=path.name, media_type="application/octet-stream")

    return dl
