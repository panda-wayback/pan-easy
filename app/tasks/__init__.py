import asyncio
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

from app.bdpan import BdpanError

_SHARE_RE = re.compile(r"https?://pan\.baidu\.com/s/[A-Za-z0-9_\-]+(?:\?[A-Za-z0-9=&_\-]*)?")
_PWD_TEXT_RE = re.compile(r"提取码\s*[:：]?\s*([A-Za-z0-9]{4})")
_PERCENT_RE = re.compile(r"(\d{1,3})(?:\.\d+)?%")
_SIZE = r"([\d.]+)\s*([kMGTP]?B)"
_STATS_RE = re.compile(rf"\(\s*{_SIZE}\s*/\s*{_SIZE}(?:,\s*{_SIZE}/s)?\s*\)\s*\[[^:\]]*:([^\]]*)\]")
_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(h|ms|m|s)")
_UNITS = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4, "PB": 1000**5}
_DURATION_UNITS = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}


class NoShareLink(ValueError):
    pass


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
    def __init__(self, bdpan, download_dir: str, tasks_file: Optional[str] = None, enable_smart_download: bool = None):
        self.bdpan = bdpan
        self.download_dir = download_dir
        self.tasks_file = tasks_file
        # 默认启用智能下载，但可以通过参数或环境变量禁用
        if enable_smart_download is None:
            enable_smart_download = os.getenv("BAIDU_EASY_SMART_DOWNLOAD", "1") == "1"
        self.enable_smart_download = enable_smart_download
        self._tasks: dict[str, dict[str, Any]] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
        self._current: Optional[tuple[str, asyncio.Task]] = None
        self._load()

    def _load(self) -> None:
        if not self.tasks_file or not os.path.exists(self.tasks_file):
            return
        with open(self.tasks_file, encoding="utf-8") as f:
            tasks = json.load(f)
        interrupted = False
        for task in tasks:
            if task.get("status") in ("queued", "running"):
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
                json.dump(list(self._tasks.values()), f, ensure_ascii=False)
            os.replace(tmp, self.tasks_file)
        except BaseException:
            os.unlink(tmp)
            raise

    def submit(self, text: str) -> dict[str, Any]:
        url, pwd = parse_share(text)
        return self._add(url, pwd)

    def list(self) -> list[dict[str, Any]]:
        return [dict(t) for t in reversed(self._tasks.values())]

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        task = self._tasks.get(task_id)
        return dict(task) if task else None

    async def delete(self, task_id: str) -> bool:
        if task_id not in self._tasks:
            return False
        current = self._current
        if current and current[0] == task_id:
            current[1].cancel()
            await asyncio.wait({current[1]})
        self._tasks.pop(task_id, None)
        self._save()
        return True

    def retry(self, task_id: str) -> Optional[dict[str, Any]]:
        task = self._tasks.get(task_id)
        if task is None or task["status"] not in ("failed", "interrupted"):
            return None
        return self._add(task["url"], task.get("pwd"))

    def _add(self, url: str, pwd: Optional[str]) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
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
            "created_at": _now(),
            "finished_at": None,
        }
        self._save()
        self._ensure_worker()
        self._queue.put_nowait(task_id)
        return dict(self._tasks[task_id])

    def _ensure_worker(self) -> None:
        if self._worker is None or self._worker.done():
            pending = [tid for tid, t in self._tasks.items() if t["status"] == "queued"]
            self._queue = asyncio.Queue()
            for tid in pending[:-1] if pending else []:
                self._queue.put_nowait(tid)
            self._worker = asyncio.get_running_loop().create_task(self._work())

    async def _work(self) -> None:
        while True:
            task_id = await self._queue.get()
            task = self._tasks.get(task_id)
            if task is None:
                continue
            run = asyncio.ensure_future(self._run(task))
            self._current = (task_id, run)
            try:
                await asyncio.wait({run})
            finally:
                self._current = None
                if not run.done():
                    run.cancel()

    async def _run(self, task: dict[str, Any]) -> None:
        task["status"] = "running"
        self._save()

        # 1. 智能下载：先尝试查找网盘中是否已存在该文件
        if self.enable_smart_download:
            existing_path = await self._find_existing_in_netdisk(task["url"], task.get("pwd"))
            
            if existing_path:
                # 2. 直接从网盘下载（跳过转存）
                success = await self._download_from_netdisk(task, existing_path)
                if success:
                    return

        # 3. 正常的转存 + 下载流程
        await self._download_from_share(task)

    async def _find_existing_in_netdisk(self, url: str, pwd: Optional[str]) -> Optional[str]:
        """在网盘的转存目录中查找是否已存在该文件"""
        try:
            # 获取分享链接的文件信息
            flags = []
            if pwd:
                flags += ["-p", pwd]
            flags += ["--json"]
            
            share_list = await self.bdpan.run("transfer", ["list", url] + flags, [])
            items = share_list.get("items", [])
            if not items:
                return None
            
            # 只处理单文件情况（多文件/文件夹仍走正常流程）
            if len(items) != 1 or items[0].get("isdir"):
                return None
            
            file_info = items[0]
            filename = file_info["server_filename"]
            filesize = file_info.get("size")
            
            # 在网盘的 bdpan 应用目录中搜索
            search_result = await self.bdpan.run("search", [filename], ["--no-dir", "--json"])
            
            for item in search_result.get("items", []):
                # 文件名和大小都匹配，且在 /apps/bdpan/ 目录下
                if (item["server_filename"] == filename and 
                    item.get("size") == filesize and
                    item["path"].startswith("/apps/bdpan/")):
                    # 返回相对路径（去掉 /apps/bdpan/ 前缀）
                    return item["path"].replace("/apps/bdpan/", "")
            
            return None
        except Exception:
            # 查找失败，继续正常流程
            return None

    async def _download_from_netdisk(self, task: dict[str, Any], netdisk_path: str) -> bool:
        """直接从网盘下载（跳过转存）"""
        tail = ""

        def on_output(text: str) -> None:
            nonlocal tail
            text = tail + text
            for match in _PERCENT_RE.finditer(text):
                value = min(int(match.group(1)), 99)
                if value > task["progress"]:
                    task["progress"] = value
            stats = None
            for stats in _STATS_RE.finditer(text):
                pass
            if stats:
                task["downloaded"] = _bytes(stats.group(1), stats.group(2))
                task["total"] = _bytes(stats.group(3), stats.group(4))
                task["speed"] = _bytes(stats.group(5), stats.group(6)) if stats.group(5) else None
                task["eta"] = _seconds(stats.group(7))
            tail = re.split(r"[\r\n]", text)[-1][-512:]

        target = os.path.join(self.download_dir, "")
        try:
            data = await self.bdpan.run("download", [netdisk_path, target], [], on_output=on_output)
            task["result"] = data
            task["status"] = "done"
            task["progress"] = 100
            self._fill_result(task, data if isinstance(data, dict) else {})
            
            # 添加提示信息
            if not task.get("error"):
                task["error"] = {
                    "code": "skipped_transfer", 
                    "message": "文件已在网盘中，跳过转存步骤直接下载", 
                    "errno": None, 
                    "hint": f"从网盘路径下载：{netdisk_path}"
                }
            
            task["speed"] = None
            task["eta"] = None
            task["finished_at"] = _now()
            self._save()
            return True
        except Exception:
            # 下载失败，返回 False 继续正常流程
            task["progress"] = 0
            return False

    async def _download_from_share(self, task: dict[str, Any]) -> None:
        """正常的转存 + 下载流程"""
        tail = ""

        def on_output(text: str) -> None:
            nonlocal tail
            text = tail + text
            for match in _PERCENT_RE.finditer(text):
                value = min(int(match.group(1)), 99)
                if value > task["progress"]:
                    task["progress"] = value
            stats = None
            for stats in _STATS_RE.finditer(text):
                pass
            if stats:
                task["downloaded"] = _bytes(stats.group(1), stats.group(2))
                task["total"] = _bytes(stats.group(3), stats.group(4))
                task["speed"] = _bytes(stats.group(5), stats.group(6)) if stats.group(5) else None
                task["eta"] = _seconds(stats.group(7))
            tail = re.split(r"[\r\n]", text)[-1][-512:]

        flags = []
        pwd = task.get("pwd")
        if pwd:
            flags += ["-p", pwd]
        target = os.path.join(self.download_dir, "")
        try:
            data = await self.bdpan.run("download", [task["url"], target], flags, on_output=on_output)
        except BdpanError as err:
            task["status"] = "failed"
            task["error"] = {"code": err.kind, "message": err.message, "errno": err.errno, "hint": err.hint}
        except Exception as err:
            task["status"] = "failed"
            task["error"] = {"code": "internal", "message": f"{type(err).__name__}: {err}", "errno": None, "hint": None}
        else:
            task["result"] = data
            if isinstance(data, dict) and data.get("status") == "submitted":
                task["status"] = "submitted"
            else:
                task["status"] = "done"
                task["progress"] = 100
                self._fill_result(task, data if isinstance(data, dict) else {})
        task["speed"] = None
        task["eta"] = None
        task["finished_at"] = _now()
        self._save()

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
