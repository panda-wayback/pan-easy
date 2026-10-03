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
        return url, None
    text_pwd = _PWD_TEXT_RE.search(text)
    return url, text_pwd.group(1) if text_pwd else None


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
    def __init__(self, bdpan, download_dir: str, tasks_file: Optional[str] = None):
        self.bdpan = bdpan
        self.download_dir = download_dir
        self.tasks_file = tasks_file
        self._tasks: dict[str, dict[str, Any]] = {}
        self._pwds: dict[str, Optional[str]] = {}
        self._queue: Optional[asyncio.Queue] = None
        self._worker: Optional[asyncio.Task] = None
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
        task_id = uuid.uuid4().hex[:12]
        self._tasks[task_id] = {
            "id": task_id,
            "url": url,
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
        self._pwds[task_id] = pwd
        self._save()
        self._ensure_worker()
        self._queue.put_nowait(task_id)
        return dict(self._tasks[task_id])

    def list(self) -> list[dict[str, Any]]:
        return [dict(t) for t in reversed(self._tasks.values())]

    def get(self, task_id: str) -> Optional[dict[str, Any]]:
        task = self._tasks.get(task_id)
        return dict(task) if task else None

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
            await self._run(self._tasks[task_id])

    async def _run(self, task: dict[str, Any]) -> None:
        task["status"] = "running"
        self._save()

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
        pwd = self._pwds.pop(task["id"], None)
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
