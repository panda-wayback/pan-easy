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


# ---- 替身 --------------------------------------------------------------

ITEM = {"name": "a.txt", "size": 5, "is_dir": False}
PAN_FILE = {"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False}


class ScriptedBdpan:
    """按调用顺序消费预设步骤；每个步骤为 (调用匹配, 结果)。

    调用匹配为 None 时匹配任意调用；否则匹配以其开头的命令名。
    """

    def __init__(self, steps):
        self.steps = [dict(match=m, outcome=o) for m, o in steps]
        self.calls = []
        self.active = 0
        self.max_active = 0

    def _take(self, command):
        candidates = [s for s in self.steps if command == s["match"] or command.startswith(s["match"])]
        if not candidates:
            raise AssertionError(f"未为调用 {command} 准备步骤")
        # 精确匹配优先，其次最长前缀
        step = min(candidates, key=lambda s: 0 if command == s["match"] else 1 - len(s["match"]) * 1e-6)
        self.steps.remove(step)
        outcome = step["outcome"]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        self.calls.append((command, list(positionals), list(flags)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            outcome = self._take(command)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        finally:
            self.active -= 1

    async def run_subcommand(self, command, subcommand, positionals=(), flags=()):
        name = f"{command} {subcommand}"
        self.calls.append((name, list(positionals), list(flags)))
        outcome = self._take(name)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def wait_finished(queue, count, timeout=5.0):
    async def poll():
        while sum(t["status"] not in ("queued", "running", "submitted") for t in queue.list()) < count:
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def run_scenario(coro):
    return asyncio.run(coro)


# ---- 基础流程 ----------------------------------------------------------

def _single_steps(outcome=None, target_dir="我的应用数据/bdpan/d"):
    target = None  # 由测试用例替换为 tmp_path/5/

    def build(target):
        return [
            ("transfer list", {"items": [ITEM]}),
            ("transfer", {"target_dir": target_dir}),
            ("ls", [PAN_FILE]),
            ("download", {"local": target, "items": [
                {"name": "a.txt", "size": 5, "saved_path": "我的应用数据/bdpan/d/a.txt"}]}),
        ]

    return build


def test_single_file_full_flow(tmp_path):
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan(_single_steps()(target))
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert bdpan.calls == [
        ("transfer list", ["https://pan.baidu.com/s/1aaa?pwd=1111"], ["-p", "1111"]),
        ("transfer", ["https://pan.baidu.com/s/1aaa?pwd=1111"], ["-p", "1111"]),
        ("ls", ["/apps/bdpan/d"], []),
        ("download", ["/apps/bdpan/d/a.txt", target], []),
    ]
    assert got["status"] == "done" and got["progress"] == 100
    assert got["saved_to"] == "5/a.txt"
    assert got["pan_path"] == "我的应用数据/bdpan/d/a.txt"
    assert got["total"] == got["downloaded"] == 5


# ---- 下载并发 ----------------------------------------------------------

class ConcurrencyStub:
    """两个不同文件；download 阶段持 hold 秒，并单独记录下载并发数。"""

    def __init__(self, tmp_path, hold=0.15):
        self.tmp = tmp_path
        self.hold = hold
        self.calls = []
        self.dl_active = 0
        self.max_dl_active = 0

    def _name(self, url):
        return "a.txt" if url.endswith("1aaa") else "b.txt"

    async def run_subcommand(self, command, subcommand, positionals=(), flags=()):
        name = self._name(positionals[0])
        return {"items": [{"name": name, "size": 5, "is_dir": False}]}

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        self.calls.append((command, list(positionals), list(flags)))
        if command == "transfer":
            return {"target_dir": "我的应用数据/bdpan/d"}
        if command == "ls":
            return [
                {"path": f"/apps/bdpan/d/{n}", "server_filename": n, "size": 5, "isdir": False}
                for n in ("a.txt", "b.txt")
            ]
        if command == "download":
            self.dl_active += 1
            self.max_dl_active = max(self.max_dl_active, self.dl_active)
            try:
                await asyncio.sleep(self.hold)
                name = self._name(positionals[0])
                return {"local": str(self.tmp / "5") + "/",
                        "items": [{"name": name, "size": 5}]}
            finally:
                self.dl_active -= 1
        raise AssertionError(command)


def _concurrent(tmp_path, n):
    bdpan = ConcurrencyStub(tmp_path)
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, max_downloads=n)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        queue.submit("https://pan.baidu.com/s/2bbb")
        await wait_finished(queue, 2)

    asyncio.run(scenario())
    return bdpan, queue


def test_downloads_run_two_at_a_time(tmp_path):
    bdpan, queue = _concurrent(tmp_path, 2)
    assert bdpan.max_dl_active == 2
    assert all(t["status"] == "done" for t in queue.list())


def test_downloads_serial_when_max_one(tmp_path):
    bdpan, queue = _concurrent(tmp_path, 1)
    assert bdpan.max_dl_active == 1
    assert all(t["status"] == "done" for t in queue.list())


# ---- 第一步 本地缓存 ---------------------------------------------------

def test_local_cache_short_circuits(tmp_path):
    cached = tmp_path / "5" / "a.txt"
    cached.parent.mkdir(parents=True)
    cached.write_text("12345")
    bdpan = ScriptedBdpan([("transfer list", {"items": [ITEM]})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert got["status"] == "done" and got["saved_to"] == "5/a.txt"
    assert [c[0] for c in bdpan.calls] == ["transfer list"]


def test_local_cache_size_mismatch_misses(tmp_path):
    cached = tmp_path / "5" / "a.txt"
    cached.parent.mkdir(parents=True)
    cached.write_text("12")  # 大小不符
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    assert queue.list()[0]["status"] == "done"


# ---- 第二步 云盘去重 ---------------------------------------------------

def test_netdisk_dedup_skips_transfer(tmp_path):
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("search", {"items": [PAN_FILE]}),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))  # smart 默认开

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    commands = [c[0] for c in bdpan.calls]
    assert "search" in commands and "transfer" not in commands
    assert bdpan.calls[2] == ("download", ["/apps/bdpan/d/a.txt", target], [])
    assert queue.list()[0]["saved_to"] == "5/a.txt"


# ---- 第三步 任务合并 ---------------------------------------------------

class SubmittedStub:
    """源任务先进入 submitted 长等待；相同链接的后任务应在转存锁之前合并。"""

    def __init__(self, tmp_path, hold=0.3):
        self.tmp = tmp_path
        self.hold = hold
        self.calls = []

    async def run_subcommand(self, command, subcommand, positionals=(), flags=()):
        return {"items": [ITEM]}

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        self.calls.append((command, list(positionals), list(flags)))
        if command == "transfer":
            return {"status": "submitted", "target_dir": "我的应用数据/bdpan/d"}
        if command == "ls":
            return [PAN_FILE]
        if command == "download":
            target = str(self.tmp / "5") + "/"
            return {"local": target, "items": [{"name": "a.txt", "size": 5}]}
        raise AssertionError(command)


def test_alias_same_url_during_submitted_wait(tmp_path):
    bdpan = SubmittedStub(tmp_path)
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        await asyncio.sleep(0.05)  # 让源任务进入 submitted
        second = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        await wait_finished(queue, 2)
        return first, second

    first, second = asyncio.run(scenario())
    got_b = queue.get(second["id"])
    assert got_b["alias_of"] == first["id"]
    assert got_b["status"] == "done" and got_b["saved_to"] == "5/a.txt"
    # 只有源任务发起了一次 transfer 与一次 download
    assert [c[0] for c in bdpan.calls].count("transfer") == 1
    assert [c[0] for c in bdpan.calls].count("download") == 1


def test_alias_same_items_different_url(tmp_path):
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
        ("transfer list", {"items": [dict(ITEM)]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("https://pan.baidu.com/s/1aaa")
        second = queue.submit("https://pan.baidu.com/s/2zzz")
        await wait_finished(queue, 2)
        return first, second

    first, second = asyncio.run(scenario())
    got_b = queue.get(second["id"])
    assert got_b["alias_of"] == first["id"]
    assert got_b["saved_to"] == "5/a.txt"


def test_partial_overlap_not_merged(tmp_path):
    target = str(tmp_path / "5") + "/"
    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [{"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
                {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False}]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": target, "items": [{"name": "b.txt", "size": 5}]}),
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/e"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        queue.submit("https://pan.baidu.com/s/2bbb")
        await wait_finished(queue, 2)

    asyncio.run(scenario())
    second = [t for t in queue.list() if t["url"].endswith("2bbb")][0]
    assert second["alias_of"] is None and second["status"] == "done"
    assert [c[0] for c in bdpan.calls].count("transfer") == 2


def test_alias_follows_source_failure(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("download", BdpanError("bdpan_error", "提取码错误（错误码 -9）", -9)),
        ("transfer list", {"items": [ITEM]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        second = queue.submit("https://pan.baidu.com/s/1aaa")
        await asyncio.sleep(0.1)
        return first, second

    first, second = asyncio.run(scenario())
    # 源任务已失败，不作为合并目标，新任务自己执行到失败
    got_b = queue.get(second["id"])
    assert got_b["alias_of"] is None and got_b["status"] == "failed"


# ---- submitted 流程 ----------------------------------------------------

def test_submitted_then_locates_and_downloads(tmp_path):
    target = str(tmp_path / "5") + "/"
    submitted = {"status": "submitted", "target_dir": "我的应用数据/bdpan/d"}
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", submitted),
        ("ls", []),
        ("search", {"items": []}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False,
                      transfer_timeout=10, transfer_poll_interval=0.01)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    assert [c[0] for c in bdpan.calls] == ["transfer list", "transfer", "ls", "search", "ls", "download"]
    assert queue.list()[0]["saved_to"] == "5/a.txt"


def test_submitted_times_out(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"status": "submitted", "target_dir": "我的应用数据/bdpan/d"}),
        ("ls", []),
        ("search", {"items": []}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False,
                      transfer_timeout=0, transfer_poll_interval=0.01)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert got["status"] == "failed" and got["error"]["code"] == "transfer_timeout"
    assert got["speed"] is None and got["finished_at"]


# ---- 持久化 / 删除 / 重试 / 失败 ---------------------------------------

def test_history_persists_and_unfinished_become_interrupted(tmp_path):
    tasks_file = tmp_path / "data" / "tasks.json"
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), str(tasks_file), enable_smart_download=False)

    async def scenario():
        done = queue.submit("链接 https://pan.baidu.com/s/1aaa 提取码：1111")
        await wait_finished(queue, 1)
        pending = queue.submit("https://pan.baidu.com/s/1bbb")
        return done, pending

    done, pending = asyncio.run(scenario())
    reloaded = TaskQueue(ScriptedBdpan([("transfer list", {"items": [ITEM]})]),
                         str(tmp_path), str(tasks_file), enable_smart_download=False)
    assert reloaded.get(done["id"])["status"] == "done"
    assert reloaded.get(done["id"])["saved_to"] == "5/a.txt"
    got = reloaded.get(pending["id"])
    assert got["status"] == "failed" and got["error"]["code"] == "interrupted"
    # 文件中不含内部字段
    raw = json.loads(tasks_file.read_text())
    assert all(not any(k.startswith("_") for k in t) for t in raw)


def test_delete_running_task(tmp_path):
    class SlowStub(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download":
                await asyncio.sleep(3600)
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = SlowStub([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await asyncio.sleep(0.05)
        assert await queue.delete(task["id"]) is True
        return task

    task = asyncio.run(scenario())
    assert queue.get(task["id"]) is None
    assert asyncio.run(queue.delete("nope")) is False


def test_retry_failed_task(tmp_path):
    target = str(tmp_path / "5") + "/"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", "网络错误", None)),
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": target, "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("链接 https://pan.baidu.com/s/1fail 提取码：1111")
        await wait_finished(queue, 1)
        new = queue.retry(first["id"])
        await wait_finished(queue, 2)
        return first, new

    first, new = asyncio.run(scenario())
    assert new is not None and new["id"] != first["id"] and new["url"] == first["url"]
    assert queue.get(first["id"])["status"] == "failed"
    assert queue.get(new["id"])["status"] == "done"
    assert queue.get(new["id"])["saved_to"] == "5/a.txt"
    assert queue.retry("nope") is None
    assert queue.retry(new["id"]) is None


def test_failure_keeps_error_and_hint(tmp_path):
    message = "转存失败: 分享接口失败: errno=13045, msg=prohibit transfer self share link"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", message, 13045)),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "failed"
    assert got["error"] == {"code": "bdpan_error", "message": message, "errno": 13045,
                            "hint": "这是你自己账号分享的链接，百度不允许转存自己的分享；文件本来就在你的网盘里"}
    assert got["finished_at"]


