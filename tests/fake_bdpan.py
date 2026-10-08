"""Test double for the bdpan CLI.

Canned mode (FAKE_BDPAN_STDOUT set): print the given stdout/stderr and exit.
Stateful mode: emulate a tiny drive rooted at FAKE_BDPAN_STORE.
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

VALID_CODE = "a" * 32
SHARE_CONTENT = b"shared-content"


def share_name() -> str:
    return os.environ.get("FAKE_SHARE_NAME", "shared.bin")


def share_items() -> list[dict]:
    return [{"name": share_name(), "size": len(SHARE_CONTENT), "is_dir": False}]


def envelope(data=None, error=""):
    return {"code": 1 if error else 0, "data": data, "error": error}


def entry(store: Path, p: Path) -> dict:
    rel = p.relative_to(store).as_posix()
    return {
        "path": "/apps/bdpan/" + rel,
        "server_filename": p.name,
        "size": 0 if p.is_dir() else p.stat().st_size,
        "isdir": p.is_dir(),
    }


def _in_store(store: Path, remote: str) -> Path:
    if remote.startswith("/apps/bdpan/"):
        remote = remote[len("/apps/bdpan/"):]
    return store / remote


def stateful(argv: list[str], stdin: str) -> object:
    store = Path(os.environ["FAKE_BDPAN_STORE"])
    store.mkdir(parents=True, exist_ok=True)
    marker = store.parent / (store.name + ".login")
    command, rest = argv[0], argv[1:]
    if "--" in rest:
        idx = rest.index("--")
        flags, pos = rest[:idx], rest[idx + 1:]
    else:
        flags, pos = rest, []

    if command == "whoami":
        return {"authenticated": marker.exists(), "has_valid_token": marker.exists()}
    if command == "login":
        if "--get-auth-url" in flags:
            return envelope({"auth_url": "https://openapi.baidu.com/oauth/2.0/authorize?fake=1"})
        if stdin.strip() == VALID_CODE:
            marker.touch()
            return envelope({"message": "登录成功"})
        return envelope(error="登录失败：授权码无效或已过期；请重新获取授权链接后再试")
    if not marker.exists():
        return envelope(error="请先执行 bdpan login 命令")

    if command == "ls":
        target = _in_store(store, pos[0]) if pos else store
        if not target.exists():
            return envelope(error="找不到指定的文件或目录（错误码 -9），请检查路径是否正确。")
        if target.is_file():
            return [entry(store, target)]
        return [entry(store, p) for p in sorted(target.iterdir())]
    if command == "transfer":
        # transfer list：查询分享内容；transfer：把分享转存到 -d 指定目录（默认 日期/）
        if "list" in rest:
            return {"items": share_items()}
        name = share_name()
        dest_rel = "2026-10-03"
        if "-d" in flags:
            dest_rel = flags[flags.index("-d") + 1].strip("/")
        dest_dir = store / dest_rel
        dest_dir.mkdir(parents=True, exist_ok=True)
        (dest_dir / name).write_bytes(SHARE_CONTENT)
        return {"target_dir": "我的应用数据/bdpan/" + dest_rel}
    if command == "mkdir":
        (store / pos[0]).mkdir(parents=True, exist_ok=True)
        return {"status": "ok", "path": pos[0]}
    if command == "upload":
        local, remote = pos
        dest = store / remote
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(local, dest)
        return envelope({"saved_path": "我的应用数据/bdpan/" + remote})
    if command == "download" and pos[0].startswith("http"):
        url, local_dir = pos
        name = os.environ.get("FAKE_SHARE_NAME", "shared.bin")
        for pct in (0, 50, 100):
            print(f"下载中 {pct:3d}% |", end="\r", flush=True, file=sys.stderr)
            time.sleep(0.05)
        dest = Path(local_dir) / name
        dest.write_bytes(b"shared-content")
        return envelope({
            "share": url,
            "count": 1,
            "items": [{"name": name, "type": "file", "saved_path": "我的应用数据/bdpan/2026-10-03/" + name}],
            "saved_path": "我的应用数据/bdpan/2026-10-03",
            "local": local_dir,
        })
    if command == "download":
        remote, local = pos
        src = _in_store(store, remote)
        if not src.exists():
            return envelope(error="找不到指定的文件或目录（错误码 -9），请检查路径是否正确。")
        print("下载中 100% |████| (1/1 B)\r", file=sys.stderr)
        if src.is_dir():
            shutil.copytree(src, local)
            return envelope({"remote": remote, "local": local})
        local_path = Path(local)
        if local_path.is_dir() or local.endswith(("/", os.sep)):
            local_path.mkdir(parents=True, exist_ok=True)
            local_path = local_path / src.name
        shutil.copyfile(src, local_path)
        return envelope({
            "remote": "/" + remote,
            "local": str(local_path.parent) + "/",
            "items": [{
                "name": src.name,
                "size": src.stat().st_size,
                "saved_path": ("我的应用数据/" + remote[len("/apps/"):])
                    if remote.startswith("/apps/") else "我的应用数据/bdpan/" + remote,
            }],
        })
    if command == "rm":
        for p in pos:
            target = store / p
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        return {"status": "ok"}
    return envelope(error=f"fake bdpan 不支持 {command}")


def main() -> int:
    argv = sys.argv[1:]
    stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()

    log = os.environ.get("FAKE_BDPAN_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"argv": argv, "stdin": stdin, "pid": os.getpid()}) + "\n")

    delay = float(os.environ.get("FAKE_BDPAN_SLEEP", "0"))
    if delay:
        time.sleep(delay)

    if "FAKE_BDPAN_STDOUT" in os.environ:
        sys.stdout.write(os.environ["FAKE_BDPAN_STDOUT"])
        sys.stderr.write(os.environ.get("FAKE_BDPAN_STDERR", ""))
        return int(os.environ.get("FAKE_BDPAN_EXIT", "0"))

    print(json.dumps(stateful(argv, stdin), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
