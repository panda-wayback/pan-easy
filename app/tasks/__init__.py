from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.bdpan import BdpanError

log = logging.getLogger("baidu_easy.tasks")

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
                 transfer_timeout: float = 1800, transfer_poll_interval: float = 10, max_downloads: Optional[int] = None):
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
        self._tasks: dict[str, dict[str, Any]] = {}
        self._ready: Optional[asyncio.Queue] = None
        self._scheduler: Optional[asyncio.Task] = None
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
        """只读查询分享文件总大小，不创建任务、不转存、不下载。"""
        url, pwd = parse_share(text)
        flags = ["-p", pwd] if pwd else []
        data = await self.bdpan.run_subcommand("transfer", "list", [url], flags)
        items = [it for it in (data or {}).get("items") or [] if isinstance(it, dict)]
        total = sum(
            size for it in items
            if not it.get("is_dir") and isinstance((size := it.get("size")), int)
        )
        return {"total_bytes": total}

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
            self._scheduler = asyncio.get_running_loop().create_task(self._schedule())

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
        single_file = len(items) == 1 and not items[0].get("is_dir")

        # 第一步：本地缓存
        if single_file:
            cached = self._cached_local(items[0])
            if cached:
                self._finish_local(task, items[0], cached)
                return

        # 第二步：云盘去重（仅单文件）
        netdisk_path = None
        if self.enable_smart_download and single_file:
            netdisk_path = await self._find_in_netdisk(items[0])

        target = os.path.join(self._target_dir(items), "")
        os.makedirs(target, exist_ok=True)

        if netdisk_path is not None:
            paths = [f"/apps/bdpan/{netdisk_path}"]
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

        # 下载阶段：占用并发名额
        async with self._download_slots:
            # 获取名额后可能已有相同任务先开始下载，再判定一次
            if await self._alias_if_overlap(task, items):
                return
            await self._download(task, items, paths, target)

    # ---- 第一步 本地缓存 --------------------------------------------

    def _cached_local(self, item: dict[str, Any]) -> Optional[str]:
        name, size = item.get("name"), item.get("size")
        if not name or size is None:
            return None
        rel = os.path.join(str(size), name)
        path = os.path.join(self.download_dir, rel)
        if os.path.isfile(path) and os.path.getsize(path) == size:
            return rel
        return None

    def _finish_local(self, task: dict[str, Any], item: dict[str, Any], rel: str) -> None:
        task.update(status="done", progress=100, saved_to=rel, total=item["size"],
                    downloaded=item["size"], speed=None, eta=None, finished_at=_now())
        task["error"] = {"code": "local_exists", "message": "文件已在本地下载目录", "errno": None,
                         "hint": f"跳过下载：downloads/{rel}"}
        self._save()
        log.info("任务完成（本地缓存命中） task=%s saved_to=%s", task["id"], rel)

    # ---- 分享信息 ---------------------------------------------------

    async def _share_items(self, task: dict[str, Any]) -> list[dict[str, Any]]:
        flags = ["-p", task["pwd"]] if task.get("pwd") else []
        data = await self.bdpan.run_subcommand("transfer", "list", [task["url"]], flags)
        return [it for it in (data or {}).get("items") or [] if isinstance(it, dict)]

    def _target_dir(self, items: list[dict[str, Any]]) -> str:
        if len(items) == 1 and not items[0].get("is_dir"):
            size = items[0].get("size")
            return os.path.join(self.download_dir, str(size)) if size is not None else self.download_dir
        if len(items) > 1:
            total = sum(i.get("size", 0) for i in items if not i.get("is_dir"))
            return os.path.join(self.download_dir, str(total) if total else "multi")
        return os.path.join(self.download_dir, "folder")

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

    # ---- 转存（全局串行） -------------------------------------------

    async def _transfer(self, task: dict[str, Any], items: list[dict[str, Any]]) -> Optional[list[str]]:
        flags = ["-p", task["pwd"]] if task.get("pwd") else []
        async with self._transfer_lock:
            # 等到锁时源任务可能已完成转存，锁内再判定一次以免重复转存
            if await self._alias_if_overlap(task, items):
                return None
            log.info("开始转存 task=%s", task["id"])
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
        sig = self._signature(items)
        url = self._norm_url(task["url"])
        for other_id, other in self._tasks.items():
            if (other_id == task["id"] or other["status"] == "failed" or other.get("alias_of")
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

    async def _download(self, task: dict[str, Any], items: list[dict[str, Any]],
                        paths: list[str], target: str) -> None:
        tail = ""
        last_milestone = 0

        def on_output(text: str) -> None:
            nonlocal tail, last_milestone
            text = tail + text
            for match in _PERCENT_RE.finditer(text):
                value = min(int(match.group(1)), 99)
                if value > task["progress"]:
                    task["progress"] = value
            stats = None
            for stats in _STATS_RE.finditer(text):
                pass
            if stats:
                dl_unit = stats.group(2) or stats.group(4)
                task["downloaded"] = _bytes(stats.group(1), dl_unit)
                task["total"] = _bytes(stats.group(3), stats.group(4))
                task["speed"] = _bytes(stats.group(5), stats.group(6)) if stats.group(5) else None
                task["eta"] = _seconds(stats.group(8))
            milestone = task["progress"] // 10 * 10
            if milestone > last_milestone:
                last_milestone = milestone
                log.info("下载进度 task=%s progress=%s%%", task["id"], milestone)
            tail = re.split(r"[\r\n]", text)[-1][-512:]
            if stats:
                self._save()

        need_archive = len(items) > 1 or items[0].get("is_dir")
        results = []
        for path in paths:
            results.append(await self.bdpan.run("download", [path, target], [], on_output=on_output))
        data = results[0] if len(results) == 1 else {
            "local": target,
            "saved_path": next((r.get("target_dir") or r.get("saved_path") for r in results
                                if isinstance(r, dict) and (r.get("target_dir") or r.get("saved_path"))), None),
            "items": [it for r in results if isinstance(r, dict) for it in r.get("items") or []],
        }
        task["result"] = data
        task["status"] = "done"
        task["progress"] = 100
        self._fill_result(task, data if isinstance(data, dict) else {})

        if need_archive:
            await self._archive(task, items, target)

        task["speed"] = None
        task["eta"] = None
        task["finished_at"] = _now()
        self._save()
        log.info("任务完成 task=%s saved_to=%s", task["id"], task.get("saved_to"))

    async def _archive(self, task: dict[str, Any], items: list[dict[str, Any]], target_dir: str) -> None:
        import glob
        import shutil

        downloaded = glob.glob(os.path.join(target_dir, "*"))
        if not downloaded:
            return
        if len(items) > 1:
            archive_name = f"multiple_files_{len(items)}"
        else:
            archive_name = items[0].get("name", "folder")
        try:
            shutil.make_archive(os.path.join(target_dir, archive_name), "zip", target_dir)
            for item_path in downloaded:
                if os.path.isfile(item_path):
                    os.remove(item_path)
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
            task["saved_to"] = os.path.join(os.path.basename(target_dir), f"{archive_name}.zip")
        except Exception as e:
            log.warning("打包失败 task=%s err=%s", task["id"], e)

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
            local = os.path.join(local, single["name"])
        task["saved_to"] = os.path.relpath(local, self.download_dir) if local else "."
        task["pan_path"] = (single or {}).get("saved_path") or data.get("saved_path")
        sizes = [it["size"] for it in items if isinstance(it.get("size"), int)]
        if sizes:
            task["total"] = task["downloaded"] = sum(sizes)





