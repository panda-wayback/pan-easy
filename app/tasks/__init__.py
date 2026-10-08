from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.bdpan import BdpanError

log = logging.getLogger("baidu_easy.tasks")

DEFAULT_MAX_TASK_BYTES = 10_000_000_000
DEFAULT_CLEANUP_INTERVAL = 3600
DEFAULT_CLEANUP_MIN_AGE = 604800

_SHARE_RE = re.compile(r"https?://pan\.baidu\.com/s/[A-Za-z0-9_\-]+(?:\?[A-Za-z0-9=&_\-]*)?")
_PWD_TEXT_RE = re.compile(r"提取码\s*[:：]?\s*([A-Za-z0-9]{4})")
_PERCENT_RE = re.compile(r"(\d{1,3})(?:\.\d+)?%")
# 匹配 (8.8/12 MB, 82 kB/s) [1m50s:35s] 格式
# 匹配 (98 kB/6.6 MB, 89 kB/s) [0s:1m12s]：已下载/总量各带单位，速度可缺省；兼容共享单位 (8.8/12 MB)
_STATS_RE = re.compile(
    r"\(\s*([\d.]+)\s*([kMGTP]?B)?/([\d.]+)\s*([kMGTP]?B)"
    r"(?:,\s*([\d.]+)\s*([kMGTP]?B)/s)?"
    r"\s*\)\s*\[([^:\]]*):([^\]]*)\]"
)
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s)")
_UNITS = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5}
_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
_AUTH_ERRORS = ("not_logged_in", "token_expired")


class NoShareLink(ValueError):
    pass


class _TransferTimeout(Exception):
    pass


class _TaskFail(Exception):
    """任务业务失败（非 bdpan），带稳定 error.code。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class DiskFull(Exception):
    """下载目录腾空间后仍不足。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def parse_max_task_bytes(value: Optional[Any] = None, env: bool = True) -> int:
    """解析单次任务大小上限；必须为正整数。value 优先，否则读环境变量，再默认。"""
    if value is not None:
        try:
            n = int(value)
        except (TypeError, ValueError) as e:
            raise ValueError("BAIDU_EASY_MAX_TASK_BYTES 必须为正整数") from e
        if n <= 0:
            raise ValueError("BAIDU_EASY_MAX_TASK_BYTES 必须为正整数")
        return n
    if env:
        raw = os.getenv("BAIDU_EASY_MAX_TASK_BYTES")
        if raw is not None and raw != "":
            return parse_max_task_bytes(raw, env=False)
    return DEFAULT_MAX_TASK_BYTES


def _nonneg_int(name: str, default: int, value: Optional[Any] = None) -> int:
    if value is not None:
        n = int(value)
    else:
        raw = os.getenv(name)
        if raw is None or raw == "":
            return default
        n = int(raw)
    if n < 0:
        raise ValueError(f"{name} 不能为负")
    return n


def _pan_dir(path: Any) -> Optional[str]:
    """bdpan 结果中的目录（显示路径、相对路径或绝对路径）转为网盘绝对路径。"""
    if not isinstance(path, str) or not path.strip("/"):
        return None
    if path.startswith("我的应用数据/"):
        return "/apps/" + path[len("我的应用数据/"):]
    return path if path.startswith("/") else "/apps/bdpan/" + path


def _entries(data: Any) -> list[dict[str, Any]]:
    items = data if isinstance(data, list) else (data or {}).get("items") if isinstance(data, dict) else None
    return [it for it in items or [] if isinstance(it, dict) and it.get("path")]


def _entry_name(it: dict[str, Any]) -> Any:
    return it.get("server_filename") or it.get("name")


def _entry_isdir(it: dict[str, Any]) -> bool:
    return bool(it.get("isdir") or it.get("is_dir"))


def _match(items: list[dict[str, Any]], entries: list[dict[str, Any]]) -> Optional[list[str]]:
    """分享中的每一项都按名称、类型与大小在网盘条目中找到时返回它们的网盘路径。"""
    paths = []
    for item in items:
        for entry in entries:
            is_dir = _entry_isdir(entry)
            if (_entry_name(entry) == item.get("name")
                    and is_dir == bool(item.get("is_dir"))
                    and (is_dir or entry.get("size") == item.get("size"))):
                paths.append(entry["path"])
                break
        else:
            return None
    return paths


def parse_share(text: str) -> tuple[str, Optional[str]]:
    match = _SHARE_RE.search(text or "")
    if not match:
        raise NoShareLink("文字中没有找到百度网盘分享链接（https://pan.baidu.com/s/...）")
    url = match.group(0)
    pwd = (parse_qs(urlsplit(url).query).get("pwd") or [None])[0]
    if pwd:
        return url, pwd
    text_pwd = _PWD_TEXT_RE.search(text)
    if text_pwd:
        pwd_value = text_pwd.group(1)
        # 将提取码附加到 URL 中，统一格式
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}pwd={pwd_value}"
        pwd = pwd_value
    return url, pwd


def _bytes(number: str, unit: str) -> int:
    return int(float(number) * _UNITS[unit])


def _seconds(text: str) -> Optional[int]:
    parts = _DURATION_RE.findall(text)
    if not parts:
        return None
    return round(sum(float(n) * _DURATION_UNITS[u] for n, u in parts))


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class TaskQueue:
    def __init__(self, bdpan, download_dir: str, tasks_file: Optional[str] = None, enable_smart_download: bool = None,
                 transfer_timeout: float = 1800, transfer_poll_interval: float = 10, max_downloads: Optional[int] = None,
                 max_task_bytes: Optional[int] = None, cleanup_interval: Optional[int] = None,
                 cleanup_min_age: Optional[int] = None):
        self.bdpan = bdpan
        self.download_dir = download_dir
        self.tasks_file = tasks_file
        self.transfer_timeout = transfer_timeout
        self.transfer_poll_interval = transfer_poll_interval
        # 第二步云盘去重默认启用，可通过参数或环境变量禁用
        if enable_smart_download is None:
            enable_smart_download = os.getenv("BAIDU_EASY_SMART_DOWNLOAD", "1") == "1"
        self.enable_smart_download = enable_smart_download
        if max_downloads is None:
            try:
                max_downloads = int(os.getenv("BAIDU_EASY_MAX_DOWNLOADS", "2"))
            except ValueError:
                max_downloads = 2
        self.max_downloads = max(1, max_downloads)
        self.max_task_bytes = parse_max_task_bytes(max_task_bytes)
        self.cleanup_interval = _nonneg_int(
            "BAIDU_EASY_CLEANUP_INTERVAL", DEFAULT_CLEANUP_INTERVAL, cleanup_interval)
        self.cleanup_min_age = _nonneg_int(
            "BAIDU_EASY_CLEANUP_MIN_AGE", DEFAULT_CLEANUP_MIN_AGE, cleanup_min_age)
        self._tasks: dict[str, dict[str, Any]] = {}
        self._ready: Optional[asyncio.Queue] = None
        self._scheduler: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._download_slots: Optional[asyncio.Semaphore] = None
        self._running: set[asyncio.Task] = set()
        self._seq = 0
        self._load()

    # ---- 持久化 -----------------------------------------------------

    def _load(self) -> None:
        if not self.tasks_file or not os.path.exists(self.tasks_file):
            return
        with open(self.tasks_file, encoding="utf-8") as f:
            tasks = json.load(f)
        interrupted = False
        for task in tasks:
            if task.get("status") in ("queued", "running", "submitted"):
                task.update(
                    status="failed",
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                    error={"code": "interrupted", "message": "服务重启时任务尚未完成，需要时请重新提交", "errno": None, "hint": None},
                )
                interrupted = True
            self._tasks[task["id"]] = task
        if interrupted:
            self._save()

    def _save(self) -> None:
        if not self.tasks_file:
            return
        directory = os.path.dirname(os.path.abspath(self.tasks_file))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tasks-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([self._public(t) for t in self._tasks.values()], f, ensure_ascii=False)
            os.replace(tmp, self.tasks_file)
        except BaseException:
            os.unlink(tmp)
            raise

    def _public(self, task: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in task.items() if not k.startswith("_")}

    # ---- 对外接口 ---------------------------------------------------

    def submit(self, text: str) -> dict[str, Any]:
        url, pwd = parse_share(text)
        return self._add(url, pwd)

    async def preview(self, text: str) -> dict[str, Any]:
        """只读查询分享文件总大小与文件名（递归展开目录），不创建任务、不转存、不下载。"""
        url, pwd = parse_share(text)
        files = await self._list_share_files(url, pwd)
        total = sum(size for it in files if isinstance((size := it.get("size")), int))
        names = [name for it in files if isinstance((name := it.get("name")), str) and name]
        return {"total_bytes": total, "names": names}

    async def _transfer_list_page(
        self, url: str, flags: list[str], source_dir: Optional[str] = None, page: int = 1
    ) -> dict[str, Any]:
        page_flags = list(flags)
        if source_dir:
            page_flags += ["--source-dir", source_dir]
        if page > 1:
            page_flags += ["--page", str(page)]
        data = await self.bdpan.run_subcommand("transfer", "list", [url], page_flags)
        return data if isinstance(data, dict) else {}

    async def _list_share_files(self, url: str, pwd: Optional[str]) -> list[dict[str, Any]]:
        """transfer list 递归展开目录，返回全部文件条目（不含目录）。"""
        flags = ["-p", pwd] if pwd else []
        files: list[dict[str, Any]] = []

        async def walk(source_dir: Optional[str]) -> None:
            page = 1
            while True:
                data = await self._transfer_list_page(url, flags, source_dir, page)
                items = [it for it in data.get("items") or [] if isinstance(it, dict)]
                for it in items:
                    if it.get("is_dir"):
                        path = it.get("path")
                        if isinstance(path, str) and path.strip():
                            await walk(path)
                        continue
                    files.append(it)
                if not data.get("has_more"):
                    break
                page += 1

        await walk(None)
        return files

    def list(self) -> list[dict[str, Any]]:
        return [self._view(t) for t in reversed(self._tasks.values())]

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        task = self._tasks.get(task_id)
        return self._view(task) if task else None

    async def delete(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        run = self._tasks[task_id].get("_run")
        if run is not None:
            run.cancel()
            await asyncio.wait({run})
        self._tasks.pop(task_id, None)
        self._save()
        log.info("任务已删除 task=%s", task_id)
        return True

    def touch_download(self, rel: str) -> None:
        """刷新下载目录内交付物的最近使用时间（mtime）；越界或不存在则忽略。"""
        if not rel or rel == "." or ".." in rel.split(os.sep) or ".." in rel.split("/"):
            return
        root = os.path.realpath(self.download_dir)
        path = os.path.realpath(os.path.join(root, rel))
        if path != root and not path.startswith(root + os.sep):
            return
        if os.path.isfile(path):
            try:
                os.utime(path, None)
            except OSError:
                pass

    def retry(self, task_id: str) -> Optional[dict[str, Any]]:
        """重试失败或中断的任务：复制原分享链接与提取码，新建任务 ID 排队；原记录保留。"""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        failed = task["status"] == "failed"
        if not failed and task.get("alias_of"):
            root = self._tasks.get(task["alias_of"])
            failed = root is not None and root["status"] == "failed"
        if not failed:
            return None
        new_task = self._add(task["url"], task.get("pwd"))
        log.info("任务重试 origin=%s new=%s", task_id, new_task["id"])
        return new_task

    def _add(self, url: str, pwd: Optional[str]) -> dict[str, Any]:
        # 先确保调度器存在（其重建队列时会接管旧的 queued 任务），避免本任务被重复入队
        self._ensure_scheduler()
        task_id = uuid.uuid4().hex[:12]
        self._seq += 1
        self._tasks[task_id] = {
            "id": task_id,
            "url": url,
            "pwd": pwd,
            "status": "queued",
            "progress": 0,
            "speed": None,
            "downloaded": None,
            "total": None,
            "eta": None,
            "saved_to": None,
            "pan_path": None,
            "result": None,
            "error": None,
            "alias_of": None,
            "_seq": self._seq,
            "created_at": _now(),
            "finished_at": None,
        }
        self._save()
        self._ready.put_nowait(task_id)
        log.info("任务已提交 task=%s url=%s", task_id, url)
        return self.get(task_id)

    # ---- 调度 -------------------------------------------------------

    def _ensure_scheduler(self) -> None:
        if self._scheduler is None or self._scheduler.done():
            self._ready = asyncio.Queue()
            self._download_slots = asyncio.Semaphore(self.max_downloads)
            self._transfer_lock = asyncio.Lock()
            for tid, t in self._tasks.items():
                if t["status"] == "queued":
                    self._ready.put_nowait(tid)
            loop = asyncio.get_running_loop()
            self._scheduler = loop.create_task(self._schedule())
            if self.cleanup_interval > 0 and (self._cleanup_task is None or self._cleanup_task.done()):
                self._cleanup_task = loop.create_task(self._cleanup_loop())

    async def _schedule(self) -> None:
        while True:
            task_id = await self._ready.get()
            task = self._tasks.get(task_id)
            if task is None:
                continue
            run = asyncio.create_task(self._process(task))
            task["_run"] = run
            self._running.add(run)
            run.add_done_callback(self._running.discard)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cleanup_interval)
            try:
                self.cleanup_cold()
            except Exception:
                log.exception("定时清理下载目录失败")

    async def _process(self, task: dict[str, Any]) -> None:
        try:
            await self._run(task)
        except asyncio.CancelledError:
            raise
        except BdpanError as err:
            if task.get("status") in ("queued", "running", "submitted"):
                task.update(
                    status="failed",
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                    error={"code": err.kind, "message": err.message, "errno": err.errno, "hint": err.hint},
                )
                self._save()
        except _TransferTimeout as err:
            if task.get("status") in ("queued", "running", "submitted"):
                task.update(
                    status="failed",
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                    error={"code": "transfer_timeout", "message": str(err), "errno": None, "hint": None},
                )
                self._save()
        except _TaskFail as err:
            if task.get("status") in ("queued", "running", "submitted"):
                task.update(
                    status="failed",
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                    error={"code": err.code, "message": err.message, "errno": None, "hint": None},
                )
                self._save()
        except Exception as err:
            if task.get("status") in ("queued", "running", "submitted"):
                task.update(
                    status="failed",
                    speed=None,
                    eta=None,
                    finished_at=_now(),
                    error={"code": "internal", "message": f"{type(err).__name__}: {err}", "errno": None, "hint": None},
                )
                self._save()
        finally:
            task.pop("_run", None)

    # ---- 执行管线 ---------------------------------------------------

    async def _run(self, task: dict[str, Any]) -> None:
        task["status"] = "running"
        self._save()

        # 先只读查询分享条目（transfer list），不占转存/下载名额
        items = await self._share_items(task)
        if not items:
            raise ValueError("分享链接无效或已失效")
        task["_items"] = items

        total_bytes = await self._share_total_bytes(task, items)
        if total_bytes > self.max_task_bytes:
            raise _TaskFail(
                "task_too_large",
                f"分享文件总大小 {total_bytes} 字节超过上限 {self.max_task_bytes} 字节",
            )

        # 第一步：本地缓存（单文件与多文件 zip 均复用已有 downloads）
        cached = self._reuse_local(task, items)
        if cached and self._finish_local(task, items, cached):
            return

        # 第二步：云盘去重（单文件命中即免；多文件须全部命中；含目录则跳过）
        paths = await self._dedup_netdisk(items) if self.enable_smart_download else None

        if paths is not None:
            # 免转存：下载前判定合并
            if await self._alias_if_overlap(task, items):
                return
        else:
            # 转存前先判定，命中则不必排队等锁（源可能正处于 submitted 长等待）
            if await self._alias_if_overlap(task, items):
                return
            # 需要转存：全局串行；锁内若判定到合并会返回 None
            try:
                paths = await self._transfer(task, items)
            except BdpanError as err:
                if err.errno != 13045:
                    raise
                paths = await self._paths_from_own_pan(items)
                if not paths:
                    raise
                log.info("自己分享链接：从网盘直接下载 task=%s paths=%s", task["id"], paths)
            if paths is None:
                return

        # 目录条目展开为文件后再定工作目录，避免 size_key=0、把目录路径当文件打包
        items, paths = await self._expand_dir_items(items, paths)
        task["_items"] = items

        # 下载阶段：占用并发名额；工作目录延后到此处创建，避免合并/失败留下空目录
        async with self._download_slots:
            # 获取名额后可能已有相同任务先开始下载，再判定一次
            if await self._alias_if_overlap(task, items):
                return
            need = sum(s for _, s in self._file_entries(items))
            self._ensure_space(need)
            target_dir = self._target_dir(task, items)
            self._prepare_target_dir(target_dir)
            try:
                await self._download(task, items, paths, target_dir)
            except Exception:
                self._cleanup_empty_dir(target_dir)
                raise

    # ---- 第一步 本地缓存 --------------------------------------------

    @staticmethod
    def _file_entries(items: list[dict[str, Any]]) -> list[tuple[str, int]]:
        """分享中的文件条目（不含目录）：(文件名, 大小)。"""
        out = []
        for it in items:
            name, size = it.get("name"), it.get("size")
            if it.get("is_dir") or not name or not isinstance(size, int):
                continue
            out.append((name, size))
        return out

    @staticmethod
    def _zip_members(path: str) -> Optional[set[tuple[str, int]]]:
        """读 zip 中央目录，返回非目录成员的 (文件名, 大小)；坏包返回 None。"""
        import zipfile

        try:
            with zipfile.ZipFile(path, "r") as zf:
                members: set[tuple[str, int]] = set()
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.rstrip("/").rsplit("/", 1)[-1]
                    members.add((name, info.file_size))
                return members
        except (OSError, zipfile.BadZipFile):
            return None

    def _zip_matches_items(self, path: str, items: list[dict[str, Any]]) -> bool:
        """路径必须是真实 zip 文件，且中央目录成员名+大小与分享文件条目完全一致。"""
        if not path or not os.path.isfile(path) or not path.endswith(".zip"):
            return False
        files = self._file_entries(items)
        expected = set(files)
        if not files or len(expected) != len(files):
            return False
        members = self._zip_members(path)
        return members is not None and members == expected

    def _reuse_local(self, task: dict[str, Any], items: list[dict[str, Any]]) -> Optional[str]:
        """扫盘文件比对复用：单文件同名同大小；多文件只认校验通过的 zip（不认目录）。"""
        files = self._file_entries(items)
        if not files:
            return None
        expected = set(files)
        if len(expected) != len(files):
            return None  # 同名同大小重复条目无法用集合可靠比对

        need_archive = len(items) > 1 or bool(items[0].get("is_dir"))

        # 单文件：只在 downloads/<该文件大小>/<任务ID>/文件名 下找，避免误命中其它包的工作目录散文件
        if not need_archive:
            name, size = files[0]
            size_path = os.path.join(self.download_dir, str(size))
            if not os.path.isdir(size_path):
                return None
            try:
                task_dirs = os.listdir(size_path)
            except OSError:
                return None
            for task_dir in task_dirs:
                task_path = os.path.join(size_path, task_dir)
                if not os.path.isdir(task_path):
                    continue
                candidate = os.path.join(task_path, name)
                if os.path.isfile(candidate) and os.path.getsize(candidate) == size:
                    return os.path.join(str(size), task_dir, name)
            return None

        # 多文件：只扫 *.zip，读中央目录比对；跳过一切目录（含空目录）
        try:
            size_dirs = os.listdir(self.download_dir)
        except OSError:
            return None
        pack_key = self._size_key(items)
        ordered = [pack_key] + [d for d in size_dirs if d != pack_key]
        for size_dir in ordered:
            size_path = os.path.join(self.download_dir, size_dir)
            if not os.path.isdir(size_path):
                continue
            try:
                names = os.listdir(size_path)
            except OSError:
                continue
            for entry in names:
                if not entry.endswith(".zip"):
                    continue
                path = os.path.join(size_path, entry)
                if self._zip_matches_items(path, items):
                    return os.path.join(size_dir, entry)
        return None

    def _finish_local(self, task: dict[str, Any], items: list[dict[str, Any]], rel: str) -> bool:
        """确认交付物有效后标记完成。多文件必须是成员校验通过的 zip；目录一律拒绝。返回是否命中。"""
        need_archive = len(items) > 1 or bool(items and items[0].get("is_dir"))
        abs_path = os.path.join(self.download_dir, rel)
        if need_archive:
            if not self._zip_matches_items(abs_path, items):
                log.warning("本地复用未通过 zip 内部校验，忽略 rel=%s", rel)
                return False
        elif not os.path.isfile(abs_path):
            log.warning("本地复用路径不是文件，忽略 rel=%s", rel)
            return False
        total = sum(i["size"] for i in items if isinstance(i.get("size"), int) and not i.get("is_dir"))
        task.update(status="done", progress=100, saved_to=rel, total=total or None,
                    downloaded=total or None, speed=None, eta=None, finished_at=_now())
        task["error"] = {"code": "local_exists", "message": "文件已在本地下载目录", "errno": None,
                         "hint": f"跳过下载：downloads/{rel}"}
        self.touch_download(rel)
        self._save()
        log.info("任务完成（本地缓存命中） task=%s saved_to=%s", task["id"], rel)
        return True

    # ---- 分享信息 ---------------------------------------------------

    async def _share_items(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        flags = ["-p", task["pwd"]] if task.get("pwd") else []
        data = await self.bdpan.run_subcommand("transfer", "list", [task["url"]], flags)
        return [it for it in (data or {}).get("items") or [] if isinstance(it, dict)]

    async def _share_total_bytes(self, task: dict[str, Any], items: list[dict[str, Any]]) -> int:
        """分享文件总大小（与 preview 一致：含目录内文件，1000 进制字节）。"""
        if any(it.get("is_dir") for it in items):
            files = await self._list_share_files(task["url"], task.get("pwd"))
            return sum(s for it in files if isinstance((s := it.get("size")), int))
        return sum(s for _, s in self._file_entries(items))

    # ---- 空间与清理 -------------------------------------------------

    def _free_bytes(self) -> int:
        st = os.statvfs(self.download_dir)
        return int(st.f_bavail * st.f_frsize)

    def _protected_paths(self) -> set[str]:
        """进行中任务的工作目录与目标 zip，清理时跳过。"""
        out: set[str] = set()
        for t in self._tasks.values():
            if t.get("status") not in ("queued", "running", "submitted"):
                continue
            items = t.get("_items")
            if not items:
                continue
            try:
                out.add(os.path.realpath(self._target_dir(t, items)))
                zip_path = os.path.join(self.download_dir, self._size_key(items), f"{t['id']}.zip")
                out.add(os.path.realpath(zip_path))
            except (OSError, TypeError, ValueError):
                continue
        return out

    def _list_deliverables(self) -> list[tuple[float, str, int]]:
        """交付物列表：(mtime, abs_path, size)。单文件与 zip。"""
        root = os.path.realpath(self.download_dir)
        found: list[tuple[float, str, int]] = []
        try:
            size_dirs = os.listdir(self.download_dir)
        except OSError:
            return found
        for size_dir in size_dirs:
            size_path = os.path.join(self.download_dir, size_dir)
            if not os.path.isdir(size_path):
                continue
            try:
                entries = os.listdir(size_path)
            except OSError:
                continue
            for entry in entries:
                path = os.path.join(size_path, entry)
                if entry.endswith(".zip") and os.path.isfile(path):
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    found.append((st.st_mtime, os.path.realpath(path), st.st_size))
                elif os.path.isdir(path):
                    try:
                        names = os.listdir(path)
                    except OSError:
                        continue
                    for name in names:
                        fp = os.path.join(path, name)
                        if not os.path.isfile(fp):
                            continue
                        try:
                            st = os.stat(fp)
                        except OSError:
                            continue
                        found.append((st.st_mtime, os.path.realpath(fp), st.st_size))
        return [(m, p, s) for m, p, s in found if p.startswith(root + os.sep)]

    def _remove_path(self, path: str) -> None:
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            return
        parent = os.path.dirname(path)
        root = os.path.realpath(self.download_dir)
        while parent.startswith(root + os.sep) or parent == root:
            if parent == root:
                break
            try:
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
                    parent = os.path.dirname(parent)
                else:
                    break
            except OSError:
                break

    def _sweep_orphan_workdirs(self, protected: set[str], min_mtime: float) -> None:
        """删除无运行中任务的残留工作目录：空目录，或目录内全部文件均已闲置超过阈值。

        不以目录自身 mtime 为准（对文件 utime 不一定更新父目录），避免误删仍在用的单文件交付物。
        """
        try:
            size_dirs = os.listdir(self.download_dir)
        except OSError:
            return
        for size_dir in size_dirs:
            size_path = os.path.join(self.download_dir, size_dir)
            if not os.path.isdir(size_path):
                continue
            try:
                entries = os.listdir(size_path)
            except OSError:
                continue
            for entry in entries:
                if entry.endswith(".zip"):
                    continue
                path = os.path.join(size_path, entry)
                real = os.path.realpath(path)
                if not os.path.isdir(path) or real in protected:
                    continue
                try:
                    names = os.listdir(path)
                except OSError:
                    continue
                if not names:
                    self._remove_path(path)
                    continue
                all_cold = True
                for name in names:
                    fp = os.path.join(path, name)
                    if os.path.isdir(fp):
                        all_cold = False
                        break
                    if not os.path.isfile(fp):
                        continue
                    try:
                        if os.path.getmtime(fp) > min_mtime:
                            all_cold = False
                            break
                    except OSError:
                        all_cold = False
                        break
                if all_cold:
                    self._remove_path(path)

    def cleanup_cold(self, need_bytes: Optional[int] = None) -> int:
        """清理闲置超过 cleanup_min_age 的交付物。need_bytes 时删到可用空间足够为止。返回删除字节数。"""
        os.makedirs(self.download_dir, exist_ok=True)
        protected = self._protected_paths()
        cutoff = time.time() - self.cleanup_min_age
        candidates = [
            (mtime, path, size) for mtime, path, size in self._list_deliverables()
            if mtime <= cutoff and path not in protected
            and not any(path.startswith(p + os.sep) for p in protected if os.path.isdir(p))
        ]
        candidates.sort(key=lambda x: x[0])  # 最久未用优先
        removed = 0
        for _mtime, path, size in candidates:
            if need_bytes is not None and self._free_bytes() >= need_bytes:
                break
            self._remove_path(path)
            removed += size
        # 腾空间已够时仍扫空/全冷残留目录，但不依赖目录 mtime
        self._sweep_orphan_workdirs(protected, cutoff)
        try:
            for size_dir in os.listdir(self.download_dir):
                size_path = os.path.join(self.download_dir, size_dir)
                if os.path.isdir(size_path) and not os.listdir(size_path):
                    try:
                        os.rmdir(size_path)
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def check_space(self, need_bytes: int) -> int:
        """按需清理冷文件后检查可用空间；足够则返回当前可用字节，不足则抛 DiskFull。"""
        try:
            self._ensure_space(need_bytes)
        except _TaskFail as err:
            if err.code == "disk_full":
                raise DiskFull(err.message) from None
            raise
        return self._free_bytes()

    def _ensure_space(self, need_bytes: int) -> None:
        if need_bytes <= 0:
            return
        os.makedirs(self.download_dir, exist_ok=True)
        if self._free_bytes() >= need_bytes:
            return
        self.cleanup_cold(need_bytes=need_bytes)
        if self._free_bytes() < need_bytes:
            raise _TaskFail(
                "disk_full",
                f"下载目录可用空间不足（需要 {need_bytes} 字节，清理后仍不够）",
            )

    @staticmethod
    def _size_key(items: list[dict[str, Any]]) -> str:
        total = sum(s for _, s in TaskQueue._file_entries(items))
        return str(total)

    def _target_dir(self, task: dict[str, Any], items: list[dict[str, Any]]) -> str:
        return os.path.join(self.download_dir, self._size_key(items), task["id"])

    def _prepare_target_dir(self, target_dir: str) -> None:
        """下载前准备工作目录：空则删掉重建，有残留则清空，避免空目录/脏目录干扰。"""
        import shutil

        if os.path.isdir(target_dir):
            try:
                contents = os.listdir(target_dir)
            except OSError:
                contents = ["?"]
            if contents:
                log.info("清理残留工作目录 dir=%s files=%s", target_dir, len(contents))
                shutil.rmtree(target_dir, ignore_errors=True)
            else:
                try:
                    os.rmdir(target_dir)
                except OSError:
                    shutil.rmtree(target_dir, ignore_errors=True)
        os.makedirs(target_dir, exist_ok=True)

    def _cleanup_empty_dir(self, target_dir: str) -> None:
        """失败时删掉空工作目录，并尽量去掉空的大小父目录。"""
        try:
            if os.path.isdir(target_dir) and not os.listdir(target_dir):
                os.rmdir(target_dir)
        except OSError:
            return
        parent = os.path.dirname(target_dir)
        try:
            if parent.startswith(self.download_dir) and os.path.isdir(parent) and not os.listdir(parent):
                os.rmdir(parent)
        except OSError:
            pass

    # ---- 第二步 云盘去重 --------------------------------------------

    async def _find_in_netdisk(self, item: dict[str, Any]) -> Optional[str]:
        name, size = item.get("name"), item.get("size")
        if not name:
            return None
        try:
            result = await self.bdpan.run("search", [name], ["--no-dir"])
        except BdpanError:
            return None
        for found in result.get("items", []) if isinstance(result, dict) else []:
            if (_entry_name(found) == name and found.get("size") == size
                    and str(found.get("path", "")).startswith("/apps/bdpan/")):
                return found["path"][len("/apps/bdpan/"):]
        return None

    async def _dedup_netdisk(self, items: list[dict[str, Any]]) -> Optional[list[str]]:
        """在 /apps/bdpan/ 下按同名同大小去重；全部命中才返回绝对路径列表。"""
        if not items or any(it.get("is_dir") for it in items):
            return None
        paths = []
        for item in items:
            rel = await self._find_in_netdisk(item)
            if rel is None:
                return None
            paths.append(f"/apps/bdpan/{rel}")
        return paths

    @staticmethod
    def _pan_transfer_dir(_task: Optional[dict[str, Any]] = None) -> str:
        """转存相对路径：整理/<YYYYMM>/<6 位随机>，外观中性、按月可清理。"""
        month = datetime.now().astimezone().strftime("%Y%m")
        return f"整理/{month}/{uuid.uuid4().hex[:6]}"

    async def _find_anywhere(self, item: dict[str, Any]) -> Optional[str]:
        """在整个网盘按同名同大小查找，返回绝对路径。"""
        name, size = item.get("name"), item.get("size")
        if not name:
            return None
        try:
            result = await self.bdpan.run("search", [name], ["--no-dir"])
        except BdpanError:
            return None
        for found in result.get("items", []) if isinstance(result, dict) else []:
            path = found.get("path")
            if (_entry_name(found) == name and found.get("size") == size
                    and isinstance(path, str) and path.startswith("/")):
                return path
        return None

    async def _paths_from_own_pan(self, items: list[dict[str, Any]]) -> Optional[list[str]]:
        """自己分享无法转存时，按条目在全盘定位；含目录或任一找不到则返回 None。"""
        if not items or any(it.get("is_dir") for it in items):
            return None
        paths = []
        for item in items:
            path = await self._find_anywhere(item)
            if path is None:
                return None
            paths.append(path)
        return paths

    async def _list_files_under(self, pan_path: str) -> list[dict[str, Any]]:
        """递归列出网盘目录下的全部文件：[{name, size, path, is_dir: False}, ...]。"""
        try:
            entries = _entries(await self.bdpan.run("ls", [pan_path], []))
        except BdpanError as err:
            if err.kind in _AUTH_ERRORS:
                raise
            return []
        out: list[dict[str, Any]] = []
        for entry in entries:
            path = entry.get("path")
            if not isinstance(path, str):
                continue
            if _entry_isdir(entry):
                out.extend(await self._list_files_under(path))
                continue
            name = _entry_name(entry)
            size = entry.get("size")
            if not name or not isinstance(size, int):
                continue
            out.append({"name": name, "size": size, "path": path, "is_dir": False})
        return out

    async def _expand_dir_items(
        self, items: list[dict[str, Any]], paths: list[str]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """目录条目递归展开为文件；无目录时原样返回。展开后为空则报错。"""
        if not items or not any(it.get("is_dir") for it in items):
            return items, paths
        if len(paths) < len(items):
            raise BdpanError("bdpan_error", "转存结果路径与分享条目数量不一致", None)
        new_items: list[dict[str, Any]] = []
        new_paths: list[str] = []
        for i, item in enumerate(items):
            path = paths[i]
            if not item.get("is_dir"):
                new_items.append(item)
                new_paths.append(path)
                continue
            files = await self._list_files_under(path)
            if not files:
                raise BdpanError(
                    "bdpan_error",
                    f"转存目录为空或无法列出文件：{item.get('name') or path}",
                    None,
                )
            for f in files:
                new_items.append({"name": f["name"], "size": f["size"], "is_dir": False})
                new_paths.append(f["path"])
        log.info("目录展开为 %s 个文件", len(new_items))
        return new_items, new_paths

    # ---- 转存（全局串行） -------------------------------------------

    async def _transfer(self, task: dict[str, Any], items: list[dict[str, Any]]) -> Optional[list[str]]:
        pan_dir = self._pan_transfer_dir(task)
        flags = ["-p", task["pwd"]] if task.get("pwd") else []
        flags += ["-d", pan_dir]
        async with self._transfer_lock:
            # 等到锁时源任务可能已完成转存，锁内再判定一次以免重复转存
            if await self._alias_if_overlap(task, items):
                return None
            log.info("开始转存 task=%s dir=%s", task["id"], pan_dir)
            result = await self.bdpan.run("transfer", [task["url"]], flags)
            if isinstance(result, dict) and result.get("status") == "submitted":
                task["status"] = "submitted"
                task["result"] = result
                self._save()
                log.info("转存已提交，等待网盘完成 task=%s", task["id"])
                result = await self._wait_transferred(task, items, result)
            task["status"] = "running"
            paths = await self._locate_transferred(result, items)
        if not paths:
            raise BdpanError("bdpan_error", "转存完成但未在网盘中找到转存结果", None)
        return paths

    async def _wait_transferred(self, task: dict[str, Any], items: list[dict[str, Any]],
                                submitted: dict[str, Any]) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.transfer_timeout
        while True:
            paths = await self._locate_transferred(submitted, items)
            if paths:
                return {**submitted, "_paths": paths}
            if loop.time() >= deadline:
                raise _TransferTimeout(
                    f"转存已提交，但 {int(self.transfer_timeout)} 秒内未在网盘中找到转存结果，请稍后重试")
            await asyncio.sleep(self.transfer_poll_interval)

    async def _locate_transferred(self, submitted: dict[str, Any],
                                  items: list[dict[str, Any]]) -> Optional[list[str]]:
        if not items:
            return None
        if submitted.get("_paths"):
            return submitted["_paths"]
        for key in ("target_dir", "saved_path"):
            directory = _pan_dir(submitted.get(key))
            if directory:
                try:
                    paths = _match(items, _entries(await self.bdpan.run("ls", [directory], [])))
                except BdpanError as err:
                    if err.kind in _AUTH_ERRORS:
                        raise
                    paths = None
                if paths:
                    return paths
        if len(items) == 1 and not items[0].get("is_dir") and items[0].get("name"):
            try:
                found = _entries(await self.bdpan.run("search", [items[0]["name"]], ["--no-dir"]))
            except BdpanError as err:
                if err.kind in _AUTH_ERRORS:
                    raise
                return None
            return _match(items, [e for e in found if str(e["path"]).startswith("/apps/bdpan/")])
        return None

    # ---- 第三步 任务合并 --------------------------------------------

    def _signature(self, items: list[dict[str, Any]]) -> set[tuple]:
        return {(i.get("name"), bool(i.get("is_dir")), None if i.get("is_dir") else i.get("size")) for i in items}

    def _norm_url(self, url: str) -> str:
        parts = urlsplit(url)
        pairs = sorted(tuple(kv.split("=", 1)) for kv in parts.query.split("&") if kv)
        query = "&".join(f"{k}={v}" for k, v in pairs)
        base = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
        return f"{base}?{query}" if query else base

    def alias_root(self, task_id: str) -> str:
        seen = set()
        while True:
            t = self._tasks.get(task_id)
            nxt = t and t.get("alias_of") if t else None
            if not nxt or nxt in seen:
                return task_id
            seen.add(task_id)
            task_id = nxt

    async def _alias_if_overlap(self, task: dict[str, Any], items: list[dict[str, Any]]) -> bool:
        """只与进行中的任务合并；已完成的文件复用只走本地缓存，不看历史 done。"""
        sig = self._signature(items)
        url = self._norm_url(task["url"])
        for other_id, other in self._tasks.items():
            # 只合并 queued / running / submitted；done / failed 一律跳过
            if (other_id == task["id"]
                    or other["status"] not in ("queued", "running", "submitted")
                    or other.get("alias_of")
                    or other.get("_seq", 0) >= task.get("_seq", 0)):
                continue
            overlap = self._norm_url(other["url"]) == url
            if not overlap and other.get("_items") is not None:
                overlap = self._signature(other["_items"]) == sig
            if overlap:
                root = self.alias_root(other_id)
                task["alias_of"] = root
                self._save()
                log.info("任务合并 task=%s alias_of=%s", task["id"], root)
                return True
        return False

    # ---- 下载 -------------------------------------------------------

    def _local_dest(self, target_dir: str, item: dict[str, Any]) -> str:
        """单文件为「目录/文件名」；目录条目仍用目录路径（尾加分隔符）。"""
        name = item.get("name")
        if name and not item.get("is_dir"):
            return os.path.join(target_dir, name)
        return os.path.join(target_dir, "")

    async def _download(self, task: dict[str, Any], items: list[dict[str, Any]],
                        paths: list[str], target_dir: str) -> None:
        # 整包 total：分享文件条目 size 之和（多文件进度不被单文件覆盖）
        size_by_index = [
            it["size"] if (not it.get("is_dir") and isinstance(it.get("size"), int)) else 0
            for it in items
        ]
        pack_total = sum(size_by_index)
        if pack_total:
            task["total"] = pack_total
            task["downloaded"] = 0

        completed = 0  # 已完整下完的文件字节合计
        tail = ""
        last_milestone = 0

        def make_on_output(base: int):
            def on_output(text: str) -> None:
                nonlocal tail, last_milestone
                text = tail + text
                stats = None
                for stats in _STATS_RE.finditer(text):
                    pass
                if stats:
                    dl_unit = stats.group(2) or stats.group(4)
                    cur_dl = _bytes(stats.group(1), dl_unit)
                    task["downloaded"] = base + cur_dl
                    if pack_total:
                        task["total"] = pack_total
                    else:
                        task["total"] = base + _bytes(stats.group(3), stats.group(4))
                    speed = _bytes(stats.group(5), stats.group(6)) if stats.group(5) else None
                    task["speed"] = speed
                    remain = (task["total"] or 0) - (task["downloaded"] or 0)
                    if speed and speed > 0 and remain > 0:
                        task["eta"] = round(remain / speed)
                    elif remain <= 0:
                        task["eta"] = 0
                    else:
                        task["eta"] = _seconds(stats.group(8))
                if pack_total and task.get("downloaded") is not None:
                    value = min(99, int(task["downloaded"] * 100 / pack_total))
                    if value > task["progress"]:
                        task["progress"] = value
                else:
                    for match in _PERCENT_RE.finditer(text):
                        value = min(int(match.group(1)), 99)
                        if value > task["progress"]:
                            task["progress"] = value
                milestone = task["progress"] // 10 * 10
                if milestone > last_milestone:
                    last_milestone = milestone
                    log.info("下载进度 task=%s progress=%s%%", task["id"], milestone)
                tail = re.split(r"[\r\n]", text)[-1][-512:]
                # 进度只更新内存，不写 tasks_file（避免刷盘拖死事件循环）
            return on_output

        need_archive = len(items) > 1 or items[0].get("is_dir")
        results = []
        dests = []
        for i, path in enumerate(paths):
            item = items[i] if i < len(items) else {}
            dest = self._local_dest(target_dir, item)
            dests.append(dest)
            results.append(await self.bdpan.run(
                "download", [path, dest], [], on_output=make_on_output(completed)))
            # 每个文件下完后校验本地确有文件，避免空目录冒充完成
            expect = size_by_index[i] if i < len(size_by_index) else 0
            if item.get("name") and not item.get("is_dir"):
                if not os.path.isfile(dest):
                    raise BdpanError("bdpan_error", f"下载结束但本地文件不存在：{item.get('name')}", None)
                actual = os.path.getsize(dest)
                if expect and actual != expect:
                    raise BdpanError(
                        "bdpan_error",
                        f"下载文件大小不符：{item.get('name')} 期望 {expect} 实际 {actual}",
                        None,
                    )
            done_size = expect if expect else (os.path.getsize(dest) if os.path.isfile(dest) else 0)
            completed += done_size
            task["downloaded"] = completed
            if pack_total:
                task["total"] = pack_total
        if len(results) == 1:
            data = results[0] if isinstance(results[0], dict) else {}
            # 显式文件路径时，以我们传入的 dest 为准，避免 bdpan 只返回目录导致二次拼接错误
            if dests and not need_archive:
                data = {**data, "local": dests[0]}
        else:
            data = {
                "local": target_dir,
                "saved_path": next((r.get("target_dir") or r.get("saved_path") for r in results
                                    if isinstance(r, dict) and (r.get("target_dir") or r.get("saved_path"))), None),
                "items": [it for r in results if isinstance(r, dict) for it in r.get("items") or []],
            }
        task["result"] = data
        # 完成态以分享条目 size 为准（多文件 bdpan 结果常缺 size，避免残留单文件进度）
        entries = self._file_entries(items)
        if entries:
            total = sum(s for _, s in entries)
            task["total"] = task["downloaded"] = total

        if need_archive:
            # 多文件：先填 pan_path，saved_to 只能在 zip 成员校验通过后由 _archive 写入（绝不能是目录）
            task["pan_path"] = data.get("saved_path")
            await self._archive(task, items, target_dir, dests)
            zip_abs = os.path.join(self.download_dir, task.get("saved_to") or "")
            if not self._zip_matches_items(zip_abs, items):
                raise BdpanError("bdpan_error", "打包后 zip 内部文件与分享条目不一致", None)
        else:
            self._fill_result(task, data if isinstance(data, dict) else {})
            if entries:
                task["total"] = task["downloaded"] = total

        task["status"] = "done"
        task["progress"] = 100
        task["speed"] = None
        task["eta"] = None
        task["finished_at"] = _now()
        if task.get("saved_to"):
            self.touch_download(task["saved_to"])
        self._save()
        log.info("任务完成 task=%s saved_to=%s", task["id"], task.get("saved_to"))

    async def _archive(
        self,
        task: dict[str, Any],
        items: list[dict[str, Any]],
        target_dir: str,
        file_paths: list[str],
    ) -> None:
        """打成 downloads/<总大小>/<task_id>.zip，读中央目录校验成员后才写入 saved_to。"""
        import zipfile

        files = [p for p in file_paths if isinstance(p, str) and os.path.isfile(p)]
        if not files:
            self._cleanup_empty_dir(target_dir)
            raise BdpanError("bdpan_error", "下载完成但本地没有可打包的文件", None)
        size_key = self._size_key(items)
        zip_name = f"{task['id']}.zip"
        size_dir = os.path.join(self.download_dir, size_key)
        os.makedirs(size_dir, exist_ok=True)
        final_zip = os.path.join(size_dir, zip_name)
        rel = os.path.join(size_key, zip_name)

        def _build() -> None:
            with tempfile.TemporaryDirectory(dir=self.download_dir, prefix=".archive-") as tmp:
                staging_zip = os.path.join(tmp, zip_name)
                used_names: set[str] = set()
                with zipfile.ZipFile(staging_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                    for path in files:
                        base = os.path.basename(path)
                        name = base
                        n = 1
                        while name in used_names:
                            stem, ext = os.path.splitext(base)
                            name = f"{stem}_{n}{ext}"
                            n += 1
                        used_names.add(name)
                        zf.write(path, arcname=name)
                os.replace(staging_zip, final_zip)
            for path in files:
                try:
                    os.remove(path)
                except OSError:
                    pass
            try:
                os.rmdir(target_dir)
            except OSError:
                pass

        try:
            await asyncio.to_thread(_build)
        except Exception as e:
            self._cleanup_empty_dir(target_dir)
            raise BdpanError("bdpan_error", f"打包失败：{e}", None) from e

        if not self._zip_matches_items(final_zip, items):
            try:
                os.remove(final_zip)
            except OSError:
                pass
            self._cleanup_empty_dir(target_dir)
            raise BdpanError("bdpan_error", "打包后 zip 内部文件与分享条目不一致", None)
        task["saved_to"] = rel

    # ---- 视图与结果填充 ---------------------------------------------

    def _view(self, task: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in task.items() if not k.startswith("_")}
        root_id = task.get("alias_of")
        root = self._tasks.get(root_id) if root_id else None
        if root is not None:
            view.update({k: root[k] for k in (
                "status", "progress", "speed", "downloaded", "total", "eta", "saved_to",
                "pan_path", "result", "error", "finished_at")})
            view["alias_of"] = self.alias_root(root_id)
        return view

    def _fill_result(self, task: dict[str, Any], data: dict[str, Any]) -> None:
        items = [it for it in data.get("items") or [] if isinstance(it, dict)]
        single = items[0] if len(items) == 1 else None
        local = data.get("local")
        if local and single and single.get("name"):
            name = single["name"]
            # local 已是完整文件路径时不再拼接；仅当像目录时才补文件名
            if os.path.basename(str(local).rstrip("/\\")) != name:
                local = os.path.join(local, name)
        task["saved_to"] = os.path.relpath(local, self.download_dir) if local else "."
        task["pan_path"] = (single or {}).get("saved_path") or data.get("saved_path")
        sizes = [it["size"] for it in items if isinstance(it.get("size"), int)]
        if sizes:
            task["total"] = task["downloaded"] = sum(sizes)





