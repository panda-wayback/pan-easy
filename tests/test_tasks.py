import asyncio
import json

import pytest

from app.bdpan import BdpanError
from app.tasks import NoShareLink, TaskQueue, parse_share

SAMPLE = """https://pan.baidu.com/s/1gN_JChf4vaDURH-mB4AWRQ?pwd=PhPR
通过百度网盘分享的文件：albn-aut....zip
链接：https://pan.baidu.com/s/1gN_JChf4vaDURH-mB4AWRQ?pwd=PhPR
复制这段内容打开「百度网盘APP 即可获取」"""


@pytest.mark.parametrize(
    "text, url, pwd",
    [
        (SAMPLE, "https://pan.baidu.com/s/1gN_JChf4vaDURH-mB4AWRQ?pwd=PhPR", "PhPR"),
        ("链接: https://pan.baidu.com/s/1abc 提取码: x9Yz 复制", "https://pan.baidu.com/s/1abc?pwd=x9Yz", "x9Yz"),
        ("链接：https://pan.baidu.com/s/1abc?pwd=aaaa 提取码：bbbb", "https://pan.baidu.com/s/1abc?pwd=aaaa", "aaaa"),
        ("https://pan.baidu.com/s/1abc", "https://pan.baidu.com/s/1abc", None),
        ("链接：https://pan.baidu.com/s/1abc?pwd=PhPR复制这段内容", "https://pan.baidu.com/s/1abc?pwd=PhPR", "PhPR"),
    ],
)
def test_parse_share(text, url, pwd):
    assert parse_share(text) == (url, pwd)


@pytest.mark.parametrize("text", ["", "没有链接", "https://example.com/s/1abc"])
def test_parse_share_without_link(text):
    with pytest.raises(NoShareLink):
        parse_share(text)


class ScriptedBdpan:
    def __init__(self, steps):
        self.steps = steps
        self.calls = []
        self.active = 0
        self.max_active = 0

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        self.calls.append((command, list(positionals), list(flags)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            outputs, outcome = self.steps.pop(0)
            for text in outputs:
                on_output(text)
                await asyncio.sleep(0.01)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.active -= 1


async def wait_finished(queue, count, timeout=5.0):
    async def poll():
        while sum(t["status"] not in ("queued", "running", "submitted") for t in queue.list()) < count:
            await asyncio.sleep(0.01)

    await asyncio.wait_for(poll(), timeout)


def test_tasks_run_in_order_one_at_a_time(tmp_path):
    bdpan = ScriptedBdpan([
        ([], {"local": str(tmp_path) + "/", "items": [{"name": "a.zip", "type": "file"}]}),
        ([], {"local": str(tmp_path / "b.zip")}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        a = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        b = queue.submit("链接 https://pan.baidu.com/s/1bbb 提取码：2222")
        await wait_finished(queue, 2)
        return a, b

    a, b = asyncio.run(scenario())
    assert bdpan.max_active == 1
    assert bdpan.calls == [
        ("download", ["https://pan.baidu.com/s/1aaa?pwd=1111", str(tmp_path) + "/"], ["-p", "1111"]),
        ("download", ["https://pan.baidu.com/s/1bbb?pwd=2222", str(tmp_path) + "/"], ["-p", "2222"]),
    ]
    assert queue.get(a["id"])["saved_to"] == "a.zip"
    assert queue.get(b["id"])["status"] == "done"
    assert queue.get(b["id"])["saved_to"] == "b.zip"


def test_progress_only_increases_and_finishes_at_100(tmp_path):
    seen = []
    bdpan = ScriptedBdpan([(["下载中  10% |", "下载中  45% |", "旧行 30% |", "下载中 100% |"], {"local": str(tmp_path)})])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        original = bdpan.run

        async def spy(*args, **kwargs):
            inner = kwargs["on_output"]

            def record(text):
                inner(text)
                seen.append(queue.get(task["id"])["progress"])

            kwargs["on_output"] = record
            return await original(*args, **kwargs)

        bdpan.run = spy
        await wait_finished(queue, 1)
        return task

    task = asyncio.run(scenario())
    assert seen == sorted(seen) and seen[-1] == 99
    assert queue.get(task["id"])["progress"] == 100


def test_stats_parsed_from_progress_line_and_finalized(tmp_path):
    seen = []
    line = "\r下载中   1% |          | (98 kB/6.6 MB, 89 kB/s) [0s:1m12s]\r"
    result = {
        "local": str(tmp_path) + "/",
        "saved_path": "我的应用数据/bdpan/2026-10-03",
        "items": [{"name": "a.txt", "size": 6573680, "saved_path": "我的应用数据/bdpan/2026-10-03/a.txt"}],
    }
    bdpan = ScriptedBdpan([(["\r下载中   0% | | ( 0 B/6.6 MB) [0s:0s]\r", line[:30], line[30:]], result)])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        original = bdpan.run

        async def spy(*args, **kwargs):
            inner = kwargs["on_output"]

            def record(text):
                inner(text)
                seen.append({k: queue.get(task["id"])[k] for k in ("speed", "downloaded", "total", "eta")})

            kwargs["on_output"] = record
            return await original(*args, **kwargs)

        bdpan.run = spy
        await wait_finished(queue, 1)
        return task

    task = asyncio.run(scenario())
    assert seen[0] == {"speed": None, "downloaded": 0, "total": 6_600_000, "eta": 0}
    assert seen[-1] == {"speed": 89_000, "downloaded": 98_000, "total": 6_600_000, "eta": 72}
    got = queue.get(task["id"])
    assert got["total"] == got["downloaded"] == 6573680
    assert got["speed"] is None and got["eta"] is None
    assert got["saved_to"] == "a.txt"
    assert got["pan_path"] == "我的应用数据/bdpan/2026-10-03/a.txt"


def test_history_persists_and_unfinished_become_interrupted(tmp_path):
    tasks_file = tmp_path / "data" / "tasks.json"
    bdpan = ScriptedBdpan([([], {"local": str(tmp_path / "a.zip")})])
    queue = TaskQueue(bdpan, str(tmp_path), str(tasks_file))

    async def scenario():
        done = queue.submit("链接 https://pan.baidu.com/s/1aaa 提取码：1111")
        await wait_finished(queue, 1)
        queue._worker.cancel()
        pending = queue.submit("https://pan.baidu.com/s/1bbb")
        return done, pending

    done, pending = asyncio.run(scenario())
    reloaded = TaskQueue(ScriptedBdpan([]), str(tmp_path), str(tasks_file))
    assert reloaded.get(done["id"])["pwd"] == "1111"
    assert [t["id"] for t in reloaded.list()] == [pending["id"], done["id"]]
    assert reloaded.get(done["id"])["status"] == "done"
    assert reloaded.get(done["id"])["saved_to"] == "a.zip"
    got = reloaded.get(pending["id"])
    assert got["status"] == "failed" and got["error"]["code"] == "interrupted"
    assert TaskQueue(ScriptedBdpan([]), str(tmp_path), str(tasks_file)).get(pending["id"])["status"] == "failed"


def test_retry(tmp_path):
    bdpan = ScriptedBdpan([
        ([], BdpanError("bdpan_error", "网络错误", None)),
        ([], {"local": str(tmp_path) + "/", "items": [{"name": "a.txt", "type": "file"}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        first = queue.submit("链接 https://pan.baidu.com/s/1fail 提取码：1111")
        await wait_finished(queue, 1)
        new = queue.retry(first["id"])
        await wait_finished(queue, 2)
        return first, new

    first, new = asyncio.run(scenario())
    assert new is not None and new["id"] != first["id"] and new["url"] == first["url"]
    assert [t["id"] for t in queue.list()] == [new["id"], first["id"]]
    assert bdpan.calls[1][2][:2] == ["-p", "1111"]
    assert queue.get(first["id"])["status"] == "failed"
    assert queue.get(new["id"])["status"] == "done"
    assert queue.get(new["id"])["saved_to"] == "a.txt"
    assert queue.retry("nope") is None
    assert queue.retry(new["id"]) is None


def test_delete(tmp_path):
    class BlockingBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if positionals[0].endswith("1slow"):
                self.calls.append((command, list(positionals), list(flags)))
                await asyncio.sleep(3600)
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = BlockingBdpan([([], {"local": str(tmp_path / "c.txt")})])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        running = queue.submit("https://pan.baidu.com/s/1slow")
        queued = queue.submit("https://pan.baidu.com/s/1skip")
        last = queue.submit("https://pan.baidu.com/s/1last")
        await asyncio.sleep(0.05)
        assert queue.get(running["id"])["status"] == "running"
        assert await queue.delete(queued["id"]) is True
        assert await queue.delete(running["id"]) is True
        await wait_finished(queue, 1)
        assert await queue.delete("nope") is False
        return running, queued, last

    running, queued, last = asyncio.run(scenario())
    assert queue.get(running["id"]) is None and queue.get(queued["id"]) is None
    assert queue.get(last["id"])["status"] == "done"
    assert [c[1][0] for c in bdpan.calls] == ["https://pan.baidu.com/s/1slow", "https://pan.baidu.com/s/1last"]


def test_failure_keeps_error(tmp_path):
    bdpan = ScriptedBdpan([([], BdpanError("bdpan_error", "提取码错误（错误码 -9）", -9))])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    task = asyncio.run(scenario())
    got = queue.get(task["id"])
    assert got["status"] == "failed"
    assert got["error"] == {"code": "bdpan_error", "message": "提取码错误（错误码 -9）", "errno": -9, "hint": None}
    assert got["finished_at"]


def test_failure_keeps_hint(tmp_path):
    message = "转存失败: 分享接口失败: errno=13045, msg=prohibit transfer self share link"
    bdpan = ScriptedBdpan([([], BdpanError("bdpan_error", message, 13045))])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    error = queue.get(asyncio.run(scenario())["id"])["error"]
    assert error["message"] == message and error["errno"] == 13045
    assert "自己" in error["hint"]


SHARE_ITEMS = [{"name": "a.txt", "size": 5, "is_dir": False}]
PAN_FILE = {"path": "/apps/bdpan/2026-10-08/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False}


class ShareBdpan(ScriptedBdpan):
    def __init__(self, steps):
        super().__init__(steps)
        self.queue = None
        self.statuses_at_ls = []

    async def run_subcommand(self, command, subcommand, positionals=(), flags=()):
        return {"items": SHARE_ITEMS}

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        if command == "ls" and self.queue:
            self.statuses_at_ls.append([t["status"] for t in self.queue.list()])
        return await super().run(command, positionals, flags, stdin, on_output)


def share_queue(tmp_path, steps, **kwargs):
    bdpan = ShareBdpan(steps)
    kwargs.setdefault("transfer_poll_interval", 0.01)
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, **kwargs)
    bdpan.queue = queue
    return bdpan, queue


def test_submitted_waits_for_transfer_then_downloads_from_pan(tmp_path):
    target = str(tmp_path / "5") + "/"
    submitted = {"status": "submitted", "task_id": "t1", "target_dir": "我的应用数据/bdpan/2026-10-08"}
    downloaded = {
        "local": target,
        "saved_path": "我的应用数据/bdpan/2026-10-08",
        "items": [{"name": "a.txt", "size": 5, "saved_path": "我的应用数据/bdpan/2026-10-08/a.txt"}],
    }
    bdpan, queue = share_queue(tmp_path, [
        ([], submitted),
        ([], []),
        ([], {"items": []}),
        ([], [PAN_FILE]),
        ([], downloaded),
        ([], {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])

    async def scenario():
        a = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        b = queue.submit("https://pan.baidu.com/s/1bbb")
        await wait_finished(queue, 2)
        return a, b

    a, b = asyncio.run(scenario())
    assert bdpan.calls == [
        ("download", ["https://pan.baidu.com/s/1aaa?pwd=1111", target], ["-p", "1111"]),
        ("ls", ["/apps/bdpan/2026-10-08"], []),
        ("search", ["a.txt"], ["--no-dir"]),
        ("ls", ["/apps/bdpan/2026-10-08"], []),
        ("download", ["/apps/bdpan/2026-10-08/a.txt", target], []),
        ("download", ["https://pan.baidu.com/s/1bbb", target], []),
    ]
    assert bdpan.statuses_at_ls[0] == ["queued", "submitted"]
    got = queue.get(a["id"])
    assert got["status"] == "done" and got["progress"] == 100
    assert got["saved_to"] == "5/a.txt"
    assert got["pan_path"] == "我的应用数据/bdpan/2026-10-08/a.txt"
    assert got["total"] == got["downloaded"] == 5
    assert got["result"] == downloaded
    assert queue.get(b["id"])["status"] == "done"


def test_submitted_without_dir_finds_single_file_by_search(tmp_path):
    target = str(tmp_path / "5") + "/"
    elsewhere = dict(PAN_FILE, path="/其它/a.txt")
    bdpan, queue = share_queue(tmp_path, [
        ([], {"status": "submitted", "task_id": "t1"}),
        ([], {"items": [elsewhere, PAN_FILE]}),
        ([], {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert bdpan.calls[2] == ("download", ["/apps/bdpan/2026-10-08/a.txt", target], [])
    assert got["status"] == "done" and got["saved_to"] == "5/a.txt"


def test_submitted_times_out(tmp_path):
    bdpan, queue = share_queue(tmp_path, [
        ([], {"status": "submitted", "task_id": "t1", "target_dir": "2026-10-08"}),
        ([], []),
        ([], {"items": []}),
    ], transfer_timeout=0)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert bdpan.calls[1] == ("ls", ["/apps/bdpan/2026-10-08"], [])
    assert bdpan.steps == []
    assert got["status"] == "failed" and got["error"]["code"] == "transfer_timeout"
    assert got["speed"] is None and got["eta"] is None and got["finished_at"]


def test_submitted_becomes_interrupted_after_reload(tmp_path):
    tasks_file = tmp_path / "tasks.json"
    tasks_file.write_text(json.dumps([{
        "id": "abc", "url": "https://pan.baidu.com/s/1aaa", "status": "submitted", "progress": 0,
        "result": {"status": "submitted", "task_id": "t1"}, "error": None,
        "created_at": "2026-10-08T00:00:00+08:00", "finished_at": None,
    }]))
    got = TaskQueue(ScriptedBdpan([]), str(tmp_path), str(tasks_file)).get("abc")
    assert got["status"] == "failed" and got["error"]["code"] == "interrupted"
