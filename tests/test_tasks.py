import asyncio
import json
import os
import re
from datetime import datetime

import pytest

from app.bdpan import BdpanError
from app.tasks import NoShareLink, TaskQueue, parse_max_task_bytes, parse_share


def _pan_dir_re() -> re.Pattern:
    month = datetime.now().astimezone().strftime("%Y%m")
    return re.compile(rf"^整理/{month}/[0-9a-f]{{6}}$")


def _transfer_dir(flags: list) -> str:
    assert "-d" in flags
    return flags[flags.index("-d") + 1]

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


def test_preview_rejects_without_link(tmp_path):
    bdpan = ScriptedBdpan([])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)
    with pytest.raises(NoShareLink):
        asyncio.run(queue.preview("没有链接"))
    assert bdpan.calls == []
    assert queue.list() == []


def test_preview_returns_total_bytes_without_creating_task(tmp_path):
    items = [
        {"name": "a.bin", "size": 150_000_000, "is_dir": False},
        {"name": "b.bin", "size": 50_000_000, "is_dir": False},
        {"name": "folder", "size": 0, "is_dir": True},  # 无 path：无法递归，不计
    ]
    bdpan = ScriptedBdpan([("transfer list", {"items": items})])
    queue = TaskQueue(bdpan, str(tmp_path), tasks_file=str(tmp_path / "tasks.json"),
                      enable_smart_download=False)
    got = asyncio.run(queue.preview("https://pan.baidu.com/s/1abc 提取码：abcd"))
    assert got == {"total_bytes": 200_000_000, "names": ["a.bin", "b.bin"]}
    assert bdpan.calls == [
        ("transfer list", ["https://pan.baidu.com/s/1abc?pwd=abcd"], ["-p", "abcd"]),
    ]
    assert queue.list() == []
    assert not (tmp_path / "tasks.json").exists()


def test_preview_recurses_into_share_dirs(tmp_path):
    """预览递归展开目录，目录内大文件计入 total_bytes（计费依据）。"""
    root = [
        {"name": "a.pt", "size": 22_500_000, "is_dir": False},
        {"name": "b.dmg", "size": 67_800_000, "is_dir": False},
        {"name": "c.docx", "size": 7_700_000, "is_dir": False},
        {"name": "book", "size": 0, "is_dir": True, "path": "/book"},
    ]
    nested = [
        {"name": "big.pdf", "size": 299_000_000, "is_dir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": root, "has_more": False}),
        ("transfer list", {"items": nested, "has_more": False}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)
    got = asyncio.run(queue.preview("https://pan.baidu.com/s/1mix?pwd=8vtg"))
    assert got["total_bytes"] == 22_500_000 + 67_800_000 + 7_700_000 + 299_000_000
    assert got["names"] == ["a.pt", "b.dmg", "c.docx", "big.pdf"]
    assert bdpan.calls == [
        ("transfer list", ["https://pan.baidu.com/s/1mix?pwd=8vtg"], ["-p", "8vtg"]),
        ("transfer list", ["https://pan.baidu.com/s/1mix?pwd=8vtg"],
         ["-p", "8vtg", "--source-dir", "/book"]),
    ]


def test_preview_paginates_transfer_list(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [{"name": "a.bin", "size": 1, "is_dir": False}], "has_more": True}),
        ("transfer list", {"items": [{"name": "b.bin", "size": 2, "is_dir": False}], "has_more": False}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)
    got = asyncio.run(queue.preview("https://pan.baidu.com/s/1page"))
    assert got == {"total_bytes": 3, "names": ["a.bin", "b.bin"]}
    assert bdpan.calls[1][2] == ["--page", "2"]


def test_preview_propagates_bdpan_error(tmp_path):
    err = BdpanError("bdpan_error", "提取码错误", -12)
    bdpan = ScriptedBdpan([("transfer list", err)])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)
    with pytest.raises(BdpanError) as caught:
        asyncio.run(queue.preview("https://pan.baidu.com/s/1abc?pwd=xxxx"))
    assert caught.value is err
    assert queue.list() == []


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
        from pathlib import Path

        self.calls.append((command, list(positionals), list(flags)))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            outcome = self._take(command)
            if isinstance(outcome, Exception):
                raise outcome
            # 模拟 bdpan 落盘，供下载后校验与打包
            if command == "download" and len(positionals) >= 2:
                dest = Path(positionals[1])
                if dest.name:  # 文件路径而非目录
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    size = 0
                    if isinstance(outcome, dict):
                        for it in outcome.get("items") or []:
                            if isinstance(it, dict) and it.get("name") == dest.name:
                                if isinstance(it.get("size"), int):
                                    size = it["size"]
                                break
                    if not dest.is_file():
                        dest.write_bytes(b"x" * size)
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
    bdpan = ScriptedBdpan(_single_steps()("ignored"))
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    dest = str(tmp_path / "5" / got["id"] / "a.txt")
    assert bdpan.calls[0] == (
        "transfer list", ["https://pan.baidu.com/s/1aaa?pwd=1111"], ["-p", "1111"])
    assert bdpan.calls[1][0] == "transfer"
    assert bdpan.calls[1][1] == ["https://pan.baidu.com/s/1aaa?pwd=1111"]
    assert bdpan.calls[1][2][:2] == ["-p", "1111"]
    assert _pan_dir_re().match(_transfer_dir(bdpan.calls[1][2]))
    assert bdpan.calls[2:] == [
        ("ls", ["/apps/bdpan/d"], []),
        ("download", ["/apps/bdpan/d/a.txt", dest], []),
    ]
    assert got["status"] == "done" and got["progress"] == 100
    assert got["saved_to"] == f"5/{got['id']}/a.txt"
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
            from pathlib import Path
            self.dl_active += 1
            self.max_dl_active = max(self.max_dl_active, self.dl_active)
            try:
                await asyncio.sleep(self.hold)
                name = os.path.basename(positionals[1]) if len(positionals) > 1 else self._name(positionals[0])
                dest = Path(positionals[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"x" * 5)
                return {"local": positionals[1], "items": [{"name": name, "size": 5}]}
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
    cached = tmp_path / "5" / "prev" / "a.txt"
    cached.parent.mkdir(parents=True)
    cached.write_text("12345")
    bdpan = ScriptedBdpan([("transfer list", {"items": [ITEM]})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert got["status"] == "done" and got["saved_to"] == "5/prev/a.txt"
    assert [c[0] for c in bdpan.calls] == ["transfer list"]


def test_local_cache_size_mismatch_misses(tmp_path):
    cached = tmp_path / "5" / "prev" / "a.txt"
    cached.parent.mkdir(parents=True)
    cached.write_text("12")  # 大小不符
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert got["status"] == "done" and got["saved_to"] == f"5/{got['id']}/a.txt"


# ---- 第二步 云盘去重 ---------------------------------------------------

def test_netdisk_dedup_skips_transfer(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("search", {"items": [PAN_FILE]}),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))  # smart 默认开

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    dest = str(tmp_path / "5" / got["id"] / "a.txt")
    commands = [c[0] for c in bdpan.calls]
    assert "search" in commands and "transfer" not in commands
    assert bdpan.calls[2] == ("download", ["/apps/bdpan/d/a.txt", dest], [])
    assert got["saved_to"] == f"5/{got['id']}/a.txt"


def test_transfer_uses_date_task_dir(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa?pwd=1111")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    transfer_call = next(c for c in bdpan.calls if c[0] == "transfer")
    assert transfer_call[2][:2] == ["-p", "1111"]
    assert _pan_dir_re().match(_transfer_dir(transfer_call[2]))


def test_dir_share_expands_to_single_file(tmp_path):
    """目录分享在转存前只读展开，仅一个文件时按单文件交付，不打包、size_key 用文件大小。"""
    folder = {"name": "book", "size": 0, "is_dir": True, "path": "/book"}
    inner_share = {"name": "a.pdf", "size": 100, "is_dir": False}
    folder_pan = {"path": "/apps/bdpan/d/book", "server_filename": "book", "size": 0, "isdir": True}
    inner_pan = {
        "path": "/apps/bdpan/d/book/a.pdf",
        "server_filename": "a.pdf",
        "size": 100,
        "isdir": False,
    }
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [folder]}),
        ("transfer list", {"items": [inner_share]}),  # 转存前只读递归 --source-dir /book
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [folder_pan]),       # 定位：浅层只看到目录
        ("ls", [folder_pan]),       # 递归列出转存目录
        ("ls", [inner_pan]),        # 展开目录得到文件
        ("download", {"local": "ignored", "items": [{"name": "a.pdf", "size": 100}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1dir")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert got["saved_to"] == f"100/{got['id']}/a.pdf"
    assert got["total"] == 100
    assert not (tmp_path / "0").exists()
    assert (tmp_path / "100" / got["id"] / "a.pdf").is_file()
    # 不应出现「没有可打包的文件」
    assert got.get("error") is None
    download = next(c for c in bdpan.calls if c[0] == "download")
    assert download[1][0] == "/apps/bdpan/d/book/a.pdf"


def test_dir_share_expands_to_zip(tmp_path):
    folder = {"name": "pack", "size": 0, "is_dir": True, "path": "/pack"}
    share_files = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    folder_pan = {"path": "/apps/bdpan/d/pack", "server_filename": "pack", "size": 0, "isdir": True}
    files = [
        {"path": "/apps/bdpan/d/pack/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
        {"path": "/apps/bdpan/d/pack/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [folder]}),
        ("transfer list", {"items": share_files}),  # 转存前只读递归 --source-dir /pack
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [folder_pan]),
        ("ls", [folder_pan]),  # 递归列出转存目录
        ("ls", files),         # 展开目录
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "ignored", "items": [{"name": "b.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1pack")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert got["saved_to"] == f"10/{got['id']}.zip"
    assert (tmp_path / "10" / f"{got['id']}.zip").is_file()


def test_dir_share_empty_fails(tmp_path):
    folder = {"name": "empty", "size": 0, "is_dir": True, "path": "/empty"}
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [folder]}),
        ("transfer list", {"items": []}),  # 转存前只读递归，目录为空
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1empty")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert got["status"] == "failed"
    assert "空" in (got.get("error") or {}).get("message", "")


def test_netdisk_dedup_multi_all_hit_skips_transfer(tmp_path):
    two = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 7, "is_dir": False},
    ]
    found = [
        {"path": "/apps/bdpan/old/t1/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
        {"path": "/apps/bdpan/old/t2/b.txt", "server_filename": "b.txt", "size": 7, "isdir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": two}),
        ("search", {"items": [found[0]]}),
        ("search", {"items": [found[1]]}),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "ignored", "items": [{"name": "b.txt", "size": 7}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1multi")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert "transfer" not in [c[0] for c in bdpan.calls]
    assert got["status"] == "done"
    assert got["saved_to"] == f"12/{got['id']}.zip"


def test_netdisk_dedup_multi_partial_still_transfers(tmp_path):
    two = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 7, "is_dir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": two}),
        ("search", {"items": [PAN_FILE]}),  # 只命中 a.txt
        ("search", {"items": []}),  # b.txt 未命中
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [
            {"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
            {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 7, "isdir": False},
        ]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "ignored", "items": [{"name": "b.txt", "size": 7}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path))

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1partial")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert [c[0] for c in bdpan.calls].count("transfer") == 1
    transfer_call = next(c for c in bdpan.calls if c[0] == "transfer")
    assert _pan_dir_re().match(_transfer_dir(transfer_call[2]))
    assert got["status"] == "done"


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
            from pathlib import Path
            dest = Path(positionals[1])
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"x" * 5)
            return {"local": positionals[1], "items": [{"name": "a.txt", "size": 5}]}
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
    # 进行中合并为 alias；若源已完成落盘则走本地文件复用
    assert got_b["status"] == "done"
    assert got_b["saved_to"] == f"5/{first['id']}/a.txt"
    assert got_b["alias_of"] == first["id"] or got_b.get("error", {}).get("code") == "local_exists"
    assert [c[0] for c in bdpan.calls].count("transfer") == 1
    assert [c[0] for c in bdpan.calls].count("download") == 1


def test_alias_same_items_different_url(tmp_path):
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
        ("transfer list", {"items": [dict(ITEM)]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        second = queue.submit("https://pan.baidu.com/s/2zzz")
        await wait_finished(queue, 2)
        return first, second

    first, second = asyncio.run(scenario())
    got_b = queue.get(second["id"])
    # 源已完成后，同内容优先本地文件复用（不必再 alias）
    assert got_b["status"] == "done" and got_b["saved_to"] == f"5/{first['id']}/a.txt"
    assert got_b["error"]["code"] == "local_exists"
    assert [c[0] for c in bdpan.calls].count("download") == 1


def test_partial_overlap_not_merged(tmp_path):
    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [{"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
                {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False}]),
        ("download", {"local": "a", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "b", "items": [{"name": "b.txt", "size": 5}]}),
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/e"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "c", "items": [{"name": "a.txt", "size": 5}]}),
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


def test_multi_file_archives_to_zip(tmp_path):
    """多文件下载后打成 task_id.zip；散文件清理掉。"""
    import zipfile
    from pathlib import Path

    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    pan = [
        {"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
        {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False},
    ]

    class WritingBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download" and len(positionals) >= 2:
                dest = Path(positionals[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"hello" if dest.name == "a.txt" else b"world")
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = WritingBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", pan),
        ("download", {"local": "a", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "b", "items": [{"name": "b.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert got["saved_to"] == f"10/{got['id']}.zip"
    assert not got["saved_to"].endswith(got["id"])  # 绝不能是工作目录
    assert got["total"] == got["downloaded"] == 10
    assert got["speed"] is None and got["eta"] is None
    zip_path = tmp_path / "10" / f"{got['id']}.zip"
    assert zip_path.is_file() and zip_path.stat().st_size > 5
    assert not (tmp_path / "10" / got["id"]).exists()
    with zipfile.ZipFile(zip_path) as zf:
        assert set(zf.namelist()) == {"a.txt", "b.txt"}
        assert zf.read("a.txt") == b"hello"
        assert zf.read("b.txt") == b"world"
        assert {(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()} == {
            ("a.txt", 5), ("b.txt", 5),
        }


def test_multi_file_progress_aggregates_across_files(tmp_path):
    """多文件下载中 progress/downloaded/total 为整包，不被当前文件覆盖。"""
    from pathlib import Path

    two_items = [
        {"name": "a.bin", "size": 100, "is_dir": False},
        {"name": "b.bin", "size": 100, "is_dir": False},
    ]
    pan = [
        {"path": "/apps/bdpan/d/a.bin", "server_filename": "a.bin", "size": 100, "isdir": False},
        {"path": "/apps/bdpan/d/b.bin", "server_filename": "b.bin", "size": 100, "isdir": False},
    ]
    seen = []

    class ProgressBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download" and on_output is not None:
                dest = Path(positionals[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"x" * 100)
                # 当前文件报 50/100；整包应是 base+50
                on_output("50% (50/100 B, 10 B/s) [0s:5s]\n")
                task = queue.get(task_id)
                seen.append((task["downloaded"], task["total"], task["progress"], task["eta"]))
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = ProgressBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", pan),
        ("download", {"local": "a", "items": [{"name": "a.bin", "size": 100}]}),
        ("download", {"local": "b", "items": [{"name": "b.bin", "size": 100}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)
    task_id = None

    async def scenario():
        nonlocal task_id
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        task_id = task["id"]
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert seen[0] == (50, 200, 25, 15)   # 第一个文件中途：50/200
    assert seen[1] == (150, 200, 75, 5)    # 第二个文件中途：100+50 / 200
    assert got["total"] == got["downloaded"] == 200
    assert got["eta"] is None and got["speed"] is None


def test_empty_workdir_cleaned_before_download(tmp_path):
    """下载前若工作目录为空则删掉重建，不把空目录当成已有内容。"""
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        # 抢在下载前塞一个空目录（模拟上次失败残留）
        empty = tmp_path / "5" / task["id"]
        empty.mkdir(parents=True, exist_ok=True)
        assert list(empty.iterdir()) == []
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert (tmp_path / "5" / got["id"] / "a.txt").is_file()


def test_download_missing_local_file_fails(tmp_path):
    """bdpan 声称成功但未落盘时任务失败，不留下空目录冒充完成。"""
    class NoWriteBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            # 跳过父类落盘逻辑
            self.calls.append((command, list(positionals), list(flags)))
            outcome = self._take(command)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    bdpan = NoWriteBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "failed"
    assert got["error"]["code"] == "bdpan_error"
    assert not (tmp_path / "5" / got["id"]).exists()


def test_multi_file_reuses_existing_zip(tmp_path):
    """多文件按 zip 成员名+大小比对复用；无任务历史、仅有 zip 也能命中。"""
    import zipfile

    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    planted = tmp_path / "10" / "old.zip"
    planted.parent.mkdir(parents=True)
    with zipfile.ZipFile(planted, "w") as zf:
        zf.writestr("a.txt", b"hello")
        zf.writestr("b.txt", b"world")

    bdpan = ScriptedBdpan([("transfer list", {"items": two_items})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done" and got["saved_to"] == "10/old.zip"
    assert got["error"]["code"] == "local_exists"
    assert [c[0] for c in bdpan.calls] == ["transfer list"]
    assert (tmp_path / "10" / "old.zip").is_file()


def test_multi_does_not_treat_empty_dir_as_deliverable(tmp_path):
    """空目录不得被当成多文件完成物；必须重新下载并打出校验通过的 zip。"""
    import zipfile
    from pathlib import Path

    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    pan = [
        {"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
        {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False},
    ]
    (tmp_path / "10" / "ghost").mkdir(parents=True)  # 空目录，像以前残留的 task 工作目录

    class WritingBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download" and len(positionals) >= 2:
                dest = Path(positionals[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"hello" if dest.name == "a.txt" else b"world")
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = WritingBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", pan),
        ("download", {"local": "a", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "b", "items": [{"name": "b.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert got["saved_to"] == f"10/{got['id']}.zip"
    assert got["saved_to"].endswith(".zip")
    zip_path = tmp_path / got["saved_to"]
    assert zip_path.is_file() and not zip_path.is_dir()
    with zipfile.ZipFile(zip_path) as zf:
        assert {(i.filename, i.file_size) for i in zf.infolist() if not i.is_dir()} == {
            ("a.txt", 5), ("b.txt", 5),
        }
    assert [c[0] for c in bdpan.calls].count("download") == 2


def test_multi_file_zip_size_mismatch_does_not_reuse(tmp_path):
    """zip 内文件大小与分享不一致时不复用。"""
    import zipfile
    from pathlib import Path

    two_items = [
        {"name": "a.txt", "size": 5, "is_dir": False},
        {"name": "b.txt", "size": 5, "is_dir": False},
    ]
    pan = [
        {"path": "/apps/bdpan/d/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False},
        {"path": "/apps/bdpan/d/b.txt", "server_filename": "b.txt", "size": 5, "isdir": False},
    ]
    stale = tmp_path / "10" / "stale.zip"
    stale.parent.mkdir(parents=True)
    with zipfile.ZipFile(stale, "w") as zf:
        zf.writestr("a.txt", b"hi")  # 大小 2 ≠ 5
        zf.writestr("b.txt", b"world")

    class WritingBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download" and len(positionals) >= 2:
                dest = Path(positionals[1])
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"hello" if dest.name == "a.txt" else b"world")
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = WritingBdpan([
        ("transfer list", {"items": two_items}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", pan),
        ("download", {"local": "a", "items": [{"name": "a.txt", "size": 5}]}),
        ("download", {"local": "b", "items": [{"name": "b.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done" and got["saved_to"] == f"10/{got['id']}.zip"
    assert [c[0] for c in bdpan.calls].count("download") == 2


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


def test_done_task_not_aliased_without_local_file(tmp_path):
    """已完成任务不参与合并；本地无文件时新任务自行下载，不挂到历史 done。"""
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/e"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        first = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        # 删掉本地文件：模拟「历史 done 但盘上已无文件」
        saved = queue.get(first["id"])["saved_to"]
        os.remove(tmp_path / saved)
        second = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 2)
        return first, second

    first, second = asyncio.run(scenario())
    got_b = queue.get(second["id"])
    assert got_b["alias_of"] is None
    assert got_b["status"] == "done"
    assert got_b["saved_to"] == f"5/{second['id']}/a.txt"
    assert (tmp_path / got_b["saved_to"]).is_file()
    assert [c[0] for c in bdpan.calls].count("download") == 2


# ---- submitted 流程 ----------------------------------------------------

def test_submitted_then_locates_and_downloads(tmp_path):
    submitted = {"status": "submitted", "target_dir": "我的应用数据/bdpan/d"}
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", submitted),
        ("ls", []),
        ("search", {"items": []}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False,
                      transfer_timeout=10, transfer_poll_interval=0.01)

    async def scenario():
        queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)

    asyncio.run(scenario())
    got = queue.list()[0]
    assert [c[0] for c in bdpan.calls] == ["transfer list", "transfer", "ls", "search", "ls", "download"]
    assert got["saved_to"] == f"5/{got['id']}/a.txt"


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

def test_download_progress_does_not_write_tasks_file(tmp_path):
    """运行中进度只更新内存，不写 tasks_file（避免刷盘拖死事件循环）。"""
    from pathlib import Path

    tasks_file = Path(tmp_path) / "tasks.json"
    wrote_on_progress = {"yes": False}

    class ProgressBdpan(ScriptedBdpan):
        async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
            if command == "download" and on_output is not None:
                before = tasks_file.read_text(encoding="utf-8")
                on_output("50% (2.5/5.0 MB, 1.0 MB/s) [0s:1s]\n")
                wrote_on_progress["yes"] = tasks_file.read_text(encoding="utf-8") != before
            return await super().run(command, positionals, flags, stdin, on_output)

    bdpan = ProgressBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), str(tasks_file), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    task_id = asyncio.run(scenario())["id"]
    assert wrote_on_progress["yes"] is False
    assert queue.get(task_id)["status"] == "done" and queue.get(task_id)["progress"] == 100


def test_history_persists_and_unfinished_become_interrupted(tmp_path):
    tasks_file = tmp_path / "data" / "tasks.json"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
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
    assert reloaded.get(done["id"])["saved_to"] == f"5/{done['id']}/a.txt"
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
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", "网络错误", None)),
        ("transfer list", {"items": [ITEM]}),
        ("transfer", {"target_dir": "我的应用数据/bdpan/d"}),
        ("ls", [PAN_FILE]),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
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
    assert queue.get(new["id"])["saved_to"] == f"5/{new['id']}/a.txt"
    assert queue.retry("nope") is None
    assert queue.retry(new["id"]) is None


def test_own_share_downloads_from_pan(tmp_path):
    """errno 13045：全盘找到同名同大小文件后直接下载。"""
    message = "转存失败: 分享接口失败: errno=13045, msg=prohibit transfer self share link"
    elsewhere = {"path": "/我的资源/a.txt", "server_filename": "a.txt", "size": 5, "isdir": False}
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", message, 13045)),
        ("search", {"items": [elsewhere]}),
        ("download", {"local": "ignored", "items": [{"name": "a.txt", "size": 5}]}),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    dest = str(tmp_path / "5" / got["id"] / "a.txt")
    assert got["status"] == "done" and got["saved_to"] == f"5/{got['id']}/a.txt"
    assert [c[0] for c in bdpan.calls] == ["transfer list", "transfer", "search", "download"]
    assert bdpan.calls[-1][1] == ["/我的资源/a.txt", dest]


def test_own_share_not_found_keeps_13045(tmp_path):
    message = "转存失败: 分享接口失败: errno=13045, msg=prohibit transfer self share link"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", message, 13045)),
        ("search", {"items": []}),
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
    assert [c[0] for c in bdpan.calls] == ["transfer list", "transfer", "search"]


def test_failure_keeps_error_and_hint(tmp_path):
    message = "转存失败: 分享接口失败: errno=13004, msg=share not found"
    bdpan = ScriptedBdpan([
        ("transfer list", {"items": [ITEM]}),
        ("transfer", BdpanError("bdpan_error", message, 13004)),
    ])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "failed"
    assert got["error"] == {"code": "bdpan_error", "message": message, "errno": 13004,
                            "hint": "分享链接已失效、已取消或不存在"}
    assert got["finished_at"]


# ---- 下载空间控制 ------------------------------------------------------

def test_parse_max_task_bytes_rejects_non_positive():
    assert parse_max_task_bytes(10) == 10
    with pytest.raises(ValueError):
        parse_max_task_bytes(0)
    with pytest.raises(ValueError):
        parse_max_task_bytes(-1)


def test_task_too_large_fails_without_transfer(tmp_path):
    big = {"name": "a.bin", "size": 1000, "is_dir": False}
    bdpan = ScriptedBdpan([("transfer list", {"items": [big]})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, max_task_bytes=500,
                      cleanup_interval=0)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1big")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "failed"
    assert got["error"]["code"] == "task_too_large"
    assert [c[0] for c in bdpan.calls] == ["transfer list"]


def test_preview_still_returns_oversized_total(tmp_path):
    big = {"name": "a.bin", "size": 1000, "is_dir": False}
    bdpan = ScriptedBdpan([("transfer list", {"items": [big]})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, max_task_bytes=500,
                      cleanup_interval=0)
    got = asyncio.run(queue.preview("https://pan.baidu.com/s/1big"))
    assert got["total_bytes"] == 1000


def test_disk_full_after_cleanup_fails(tmp_path, monkeypatch):
    bdpan = ScriptedBdpan(_single_steps()(str(tmp_path / "5")))
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, max_task_bytes=10_000,
                      cleanup_interval=0, cleanup_min_age=0)
    monkeypatch.setattr(queue, "_free_bytes", lambda: 0)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "failed"
    assert got["error"]["code"] == "disk_full"


def test_ensure_space_deletes_cold_deliverable(tmp_path, monkeypatch):
    # 放在其它 size 桶，避免同名同大小被本地缓存命中而跳过腾空间
    cold = tmp_path / "999" / "old" / "junk.bin"
    cold.parent.mkdir(parents=True)
    cold.write_bytes(b"xxxxx")
    os.utime(cold, (1, 1))

    free = {"n": 0}

    bdpan = ScriptedBdpan(_single_steps()(str(tmp_path / "5")))
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, max_task_bytes=10_000,
                      cleanup_interval=0, cleanup_min_age=0)
    monkeypatch.setattr(queue, "_free_bytes", lambda: free["n"])
    real_remove = queue._remove_path

    def remove_and_free(path):
        real_remove(path)
        free["n"] = 1000

    monkeypatch.setattr(queue, "_remove_path", remove_and_free)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert not cold.exists()


def test_touch_download_and_local_cache_refresh_mtime(tmp_path):
    dest = tmp_path / "5" / "prev" / "a.txt"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"hello")
    os.utime(dest, (10, 10))
    bdpan = ScriptedBdpan([("transfer list", {"items": [ITEM]})])
    queue = TaskQueue(bdpan, str(tmp_path), enable_smart_download=False, cleanup_interval=0)

    async def scenario():
        task = queue.submit("https://pan.baidu.com/s/1aaa")
        await wait_finished(queue, 1)
        return task

    got = queue.get(asyncio.run(scenario())["id"])
    assert got["status"] == "done"
    assert os.path.getmtime(dest) > 10
    before = os.path.getmtime(dest)
    os.utime(dest, (20, 20))
    queue.touch_download(f"5/prev/a.txt")
    assert os.path.getmtime(dest) > 20


def test_cleanup_cold_respects_min_age(tmp_path):
    cold = tmp_path / "5" / "old" / "a.txt"
    cold.parent.mkdir(parents=True)
    cold.write_bytes(b"xxxxx")
    os.utime(cold, (1, 1))
    hot = tmp_path / "5" / "new" / "a.txt"
    hot.parent.mkdir(parents=True)
    hot.write_bytes(b"xxxxx")
    # hot 保持当前 mtime
    queue = TaskQueue(ScriptedBdpan([]), str(tmp_path), cleanup_interval=0, cleanup_min_age=3600)
    queue.cleanup_cold()
    assert not cold.exists()
    assert hot.exists()


def test_sweep_does_not_delete_hot_file_when_dir_mtime_old(tmp_path):
    """父目录 mtime 很旧、但文件刚被 touch 时，不得整目录删掉。"""
    hot = tmp_path / "5" / "keep" / "a.txt"
    hot.parent.mkdir(parents=True)
    hot.write_bytes(b"hello")
    os.utime(hot, None)  # 文件是新的
    os.utime(hot.parent, (1, 1))  # 目录很旧
    queue = TaskQueue(ScriptedBdpan([]), str(tmp_path), cleanup_interval=0, cleanup_min_age=3600)
    queue.cleanup_cold()
    assert hot.exists()


def test_check_space_raises_disk_full(tmp_path, monkeypatch):
    from app.tasks import DiskFull

    queue = TaskQueue(ScriptedBdpan([]), str(tmp_path), cleanup_interval=0, cleanup_min_age=0)
    monkeypatch.setattr(queue, "_free_bytes", lambda: 0)
    with pytest.raises(DiskFull):
        queue.check_space(100)


