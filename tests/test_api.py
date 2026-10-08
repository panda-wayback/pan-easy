import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import _CleanupFileResponse, create_api
from app.bdpan import BdpanError
from app.tasks import TaskQueue

KEY = "test-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def make_api(stub, tmp_dir):
    downloads = tmp_dir.parent / "downloads"
    downloads.mkdir(exist_ok=True)
    return create_api(stub, KEY, TaskQueue(stub, str(downloads)), str(tmp_dir))


class StubBdpan:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.errors = {}
        self.hooks = {}

    async def run(self, command, positionals=(), flags=(), stdin=None, on_output=None):
        self.calls.append((command, list(positionals), list(flags), stdin))
        if command in self.hooks:
            return await self.hooks[command](list(positionals))
        if command in self.errors:
            raise self.errors[command]
        return self.results.get(command, {"status": "ok"})

    async def run_subcommand(self, command, subcommand, positionals=(), flags=()):
        name = f"{command} {subcommand}"
        self.calls.append((name, list(positionals), list(flags), None))
        if name in self.errors:
            raise self.errors[name]
        return self.results.get(name, {"items": []})


@pytest.fixture
def stub():
    return StubBdpan()


@pytest.fixture
def tmp_dir(tmp_path):
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def client(stub, tmp_dir):
    return TestClient(make_api(stub, tmp_dir), raise_server_exceptions=False)


def last_call(stub):
    return stub.calls[-1]


# --- auth ---------------------------------------------------------------


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": KEY}])
def test_rejects_missing_or_wrong_key(client, stub, headers):
    res = client.get("/status", headers=headers)
    assert res.status_code == 401
    assert res.json() == {"ok": False, "error": {"code": "unauthorized", "message": "缺少或错误的访问密钥"}}
    assert stub.calls == []


def test_accepts_correct_key(client, stub):
    stub.results["whoami"] = {"authenticated": True}
    res = client.get("/status", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "data": {"authenticated": True}}


def test_upload_rejected_before_body_is_processed(client, stub, tmp_dir):
    res = client.post("/upload", files={"file": ("a.txt", b"x")}, data={"remote_path": "a.txt"})
    assert res.status_code == 401
    assert stub.calls == []


# --- login --------------------------------------------------------------


def test_login_url_requires_disclaimer(client, stub):
    res = client.post("/login/url", json={"accept_disclaimer": False}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert stub.calls == []


def test_login_url(client, stub):
    stub.results["login"] = {"auth_url": "https://x"}
    res = client.post("/login/url", json={"accept_disclaimer": True}, headers=AUTH)
    assert res.json() == {"ok": True, "data": {"auth_url": "https://x"}}
    assert last_call(stub) == ("login", [], ["--get-auth-url", "--accept-disclaimer"], None)


@pytest.mark.parametrize("code", ["", "abc", "z" * 32, "a" * 33])
def test_login_code_format(client, stub, code):
    res = client.post("/login/code", json={"code": code}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "auth_code_invalid"
    assert stub.calls == []


def test_login_code_passes_via_stdin(client, stub):
    code = "0123456789abcdef0123456789ABCDEF"
    res = client.post("/login/code", json={"code": code}, headers=AUTH)
    assert res.status_code == 200
    assert last_call(stub) == ("login", [], ["--set-code-stdin", "--accept-disclaimer"], code + "\n")


def test_login_code_rejected_by_bdpan(client, stub):
    stub.errors["login"] = BdpanError("not_logged_in", "登录失败：授权码无效或已过期；请执行 bdpan login")
    res = client.post("/login/code", json={"code": "a" * 32}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "auth_code_invalid"


def test_login_operations_are_serialized(stub, tmp_dir):
    active = {"now": 0, "max": 0}

    async def slow_login(_):
        active["now"] += 1
        active["max"] = max(active["max"], active["now"])
        await asyncio.sleep(0.05)
        active["now"] -= 1
        return {"auth_url": "u"}

    stub.hooks["login"] = slow_login
    api = make_api(stub, tmp_dir)

    async def scenario():
        import httpx

        transport = httpx.ASGITransport(app=api)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", headers=AUTH) as c:
            await asyncio.gather(*[c.post("/login/url", json={"accept_disclaimer": True}) for _ in range(4)])

    asyncio.run(scenario())
    assert active["max"] == 1


# --- parameter mapping -------------------------------------------------


def test_ls_mapping(client, stub):
    stub.results["ls"] = []
    client.get("/ls", params={"path": "docs", "order": "time", "desc": "true", "folder": "true"}, headers=AUTH)
    assert last_call(stub) == ("ls", ["docs"], ["--order", "time", "--desc", "--folder"], None)
    client.get("/ls", headers=AUTH)
    assert last_call(stub) == ("ls", [], [], None)


def test_search_mapping(client, stub):
    client.get("/search", params={"q": "-x", "category": 3, "scope": "no_dir", "page": 2, "page_size": 10}, headers=AUTH)
    assert last_call(stub) == ("search", ["-x"], ["--category", "3", "--page", "2", "--page-size", "10", "--no-dir"], None)
    client.get("/search", params={"q": "y", "scope": "dir_only"}, headers=AUTH)
    assert last_call(stub)[2][-1] == "--dir-only"


@pytest.mark.parametrize("params", [{"q": "x", "category": 8}, {"q": "x", "page_size": 51}, {"q": " "}, {}])
def test_search_invalid(client, stub, params):
    res = client.get("/search", params=params, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"


def test_transfer_mapping_and_submitted(client, stub):
    stub.results["transfer"] = {"count": 1, "files": []}
    res = client.post("/transfer", json={"url": "https://pan.baidu.com/s/1x", "pwd": "abcd", "dir": "in"}, headers=AUTH)
    assert res.status_code == 200
    assert last_call(stub) == ("transfer", ["https://pan.baidu.com/s/1x"], ["-p", "abcd", "-d", "in"], None)

    stub.results["transfer"] = {"status": "submitted", "task_id": "t1"}
    res = client.post("/transfer", json={"url": "https://pan.baidu.com/s/1x"}, headers=AUTH)
    assert res.status_code == 202
    assert res.json() == {"ok": True, "data": {"status": "submitted", "task_id": "t1"}}


def test_share_mapping(client, stub):
    client.post("/share", json={"paths": ["a", "b"]}, headers=AUTH)
    assert last_call(stub) == ("share", ["a", "b"], ["--period", "7"], None)
    res = client.post("/share", json={"paths": ["a"], "period": 3}, headers=AUTH)
    assert res.status_code == 400


def test_mkdir_mv_cp_rename_rm_mapping(client, stub):
    client.post("/mkdir", json={"path": "a/b"}, headers=AUTH)
    assert last_call(stub) == ("mkdir", ["a/b"], [], None)
    client.post("/mv", json={"src": "a", "dst": "b"}, headers=AUTH)
    assert last_call(stub) == ("mv", ["a", "b"], [], None)
    client.post("/cp", json={"src": "a", "dst": "b"}, headers=AUTH)
    assert last_call(stub) == ("cp", ["a", "b"], [], None)
    client.post("/rename", json={"path": "a/x.txt", "name": "y.txt"}, headers=AUTH)
    assert last_call(stub) == ("rename", ["a/x.txt", "y.txt"], [], None)
    client.post("/rm", json={"paths": ["a", "b"]}, headers=AUTH)
    assert last_call(stub) == ("rm", ["a", "b"], ["--force"], None)


@pytest.mark.parametrize(
    "method, url, kwargs",
    [
        ("get", "/ls", {"params": {"path": "../x"}}),
        ("get", "/ls", {"params": {"path": "/apps/bdpan/x"}}),
        ("get", "/download", {"params": {"path": "~/x"}}),
        ("post", "/mkdir", {"json": {"path": "a/../../b"}}),
        ("post", "/mv", {"json": {"src": "a", "dst": "/etc"}}),
        ("post", "/rm", {"json": {"paths": []}}),
        ("post", "/rm", {"json": {"paths": ["ok", ".."]}}),
        ("post", "/rename", {"json": {"path": "a", "name": "x/y"}}),
        ("post", "/transfer", {"json": {"url": "ftp://x"}}),
        ("post", "/transfer", {"json": {"url": "https://pan.baidu.com/s/1", "dir": "../x"}}),
    ],
)
def test_invalid_arguments(client, stub, method, url, kwargs):
    res = getattr(client, method)(url, headers=AUTH, **kwargs)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert stub.calls == []


@pytest.mark.parametrize(
    "error, status",
    [
        (BdpanError("not_logged_in", "请先执行 bdpan login 命令"), 409),
        (BdpanError("token_expired", "Token 过期"), 409),
        (BdpanError("bdpan_error", "找不到（错误码 -9）", -9), 502),
        (BdpanError("bdpan_error", "errno=13004, msg=share not found", 13004), 502),
        (BdpanError("timeout", "超时"), 504),
        (BdpanError("not_found", "找不到 bdpan"), 500),
    ],
)
def test_error_mapping(client, stub, error, status):
    stub.errors["mkdir"] = error
    res = client.post("/mkdir", json={"path": "a"}, headers=AUTH)
    assert res.status_code == status
    body = res.json()["error"]
    assert body["code"] == (error.kind if status != 500 else "internal")
    assert body["message"] == error.message
    assert body.get("errno") == error.errno
    assert body.get("hint") == error.hint


# --- tasks --------------------------------------------------------------


def test_tasks_submit_list_get(stub, tmp_dir):
    stub.results["download"] = {"local": str(tmp_dir.parent / "downloads" / "a.zip")}
    with TestClient(make_api(stub, tmp_dir)) as client:
        res = client.post("/tasks", json={"text": "链接：https://pan.baidu.com/s/1abc?pwd=wxyz 复制"}, headers=AUTH)
        assert res.status_code == 202
        first = res.json()["data"]
        assert first["url"] == "https://pan.baidu.com/s/1abc?pwd=wxyz"
        assert first["status"] in ("queued", "running", "done")

        second = client.post("/tasks", json={"text": "https://pan.baidu.com/s/2def"}, headers=AUTH).json()["data"]
        listed = client.get("/tasks", headers=AUTH).json()["data"]
        assert [t["id"] for t in listed] == [second["id"], first["id"]]

        got = client.get(f"/tasks/{first['id']}", headers=AUTH)
        assert got.status_code == 200 and got.json()["data"]["id"] == first["id"]


def test_tasks_without_link_rejected(client, stub):
    res = client.post("/tasks", json={"text": "没有链接的文字"}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert stub.calls == []


def test_tasks_preview(client, stub):
    stub.results["transfer list"] = {
        "items": [
            {"name": "a.bin", "size": 150_000_000, "is_dir": False},
            {"name": "dir", "size": 0, "is_dir": True},
        ]
    }
    res = client.post("/tasks/preview", json={"text": "https://pan.baidu.com/s/1abc?pwd=wxyz"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"ok": True, "data": {"total_bytes": 150_000_000, "names": ["a.bin"]}}
    assert stub.calls == [
        ("transfer list", ["https://pan.baidu.com/s/1abc?pwd=wxyz"], ["-p", "wxyz"], None),
    ]
    assert client.get("/tasks", headers=AUTH).json()["data"] == []


def test_tasks_preview_without_link(client, stub):
    res = client.post("/tasks/preview", json={"text": "没有链接"}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert stub.calls == []


def test_tasks_preview_bdpan_error(client, stub):
    stub.errors["transfer list"] = BdpanError("bdpan_error", "提取码错误", -9)
    res = client.post("/tasks/preview", json={"text": "https://pan.baidu.com/s/1abc?pwd=bad"}, headers=AUTH)
    assert res.status_code == 502
    assert res.json()["error"]["code"] == "bdpan_error"
    assert stub.calls == [
        ("transfer list", ["https://pan.baidu.com/s/1abc?pwd=bad"], ["-p", "bad"], None),
    ]


def test_unknown_task_404(client):
    res = client.get("/tasks/nope", headers=AUTH)
    assert res.status_code == 404 and res.json()["error"]["code"] == "task_not_found"


# --- upload / download temp files ---------------------------------------


def test_upload_success_cleans_temp(client, stub, tmp_dir):
    seen = {}

    async def upload(pos):
        local, remote = pos
        seen["content"] = Path(local).read_bytes()
        seen["remote"] = remote
        return {"saved_path": "我的应用数据/bdpan/" + remote}

    stub.hooks["upload"] = upload
    res = client.post("/upload", files={"file": ("a.txt", b"hello")}, data={"remote_path": "docs/a.txt"}, headers=AUTH)
    assert res.status_code == 200
    assert seen == {"content": b"hello", "remote": "docs/a.txt"}
    assert list(tmp_dir.iterdir()) == []


def test_upload_failure_cleans_temp(client, stub, tmp_dir):
    stub.errors["upload"] = BdpanError("bdpan_error", "空间不足（错误码 -10）", -10)
    res = client.post("/upload", files={"file": ("a.txt", b"hello")}, data={"remote_path": "a.txt"}, headers=AUTH)
    assert res.status_code == 502
    assert list(tmp_dir.iterdir()) == []


def test_upload_remote_path_must_be_file(client, stub):
    res = client.post("/upload", files={"file": ("a.txt", b"x")}, data={"remote_path": "docs/"}, headers=AUTH)
    assert res.status_code == 400
    assert stub.calls == []


def test_download_success_streams_and_cleans(client, stub, tmp_dir):
    stub.results["ls"] = [{"server_filename": "a.txt", "isdir": False}]

    async def download(pos):
        Path(pos[1]).write_bytes(b"content")
        return {"local": pos[1]}

    stub.hooks["download"] = download
    res = client.get("/download", params={"path": "docs/a.txt"}, headers=AUTH)
    assert res.status_code == 200
    assert res.content == b"content"
    assert "a.txt" in res.headers["content-disposition"]
    assert stub.calls[0][:2] == ("ls", ["docs"])
    assert list(tmp_dir.iterdir()) == []


def test_download_rejects_directory(client, stub, tmp_dir):
    stub.results["ls"] = [{"server_filename": "docs", "isdir": True}]
    res = client.get("/download", params={"path": "docs"}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert [c[0] for c in stub.calls] == ["ls"]
    assert list(tmp_dir.iterdir()) == []


def test_download_failure_cleans_temp(client, stub, tmp_dir):
    stub.results["ls"] = []
    stub.errors["download"] = BdpanError("bdpan_error", "找不到（错误码 -9）", -9)
    res = client.get("/download", params={"path": "a.txt"}, headers=AUTH)
    assert res.status_code == 502
    assert list(tmp_dir.iterdir()) == []


# --- client disconnect --------------------------------------------------


async def asgi_call(app, method, path, headers, body=b"", query=b""):
    pending = [{"type": "http.request", "body": body, "more_body": False}]

    async def receive():
        if pending:
            return pending.pop(0)
        return {"type": "http.disconnect"}

    async def send(_):
        pass

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": query,
        "root_path": "", "headers": headers, "client": ("test", 1), "server": ("test", 80),
    }
    await app(scope, receive, send)


def _hanging(cancelled):
    async def hang(_):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    return hang


def test_upload_client_disconnect_cancels_and_cleans(stub, tmp_dir):
    cancelled = []
    stub.hooks["upload"] = _hanging(cancelled)
    api = make_api(stub, tmp_dir)
    boundary = "XBOUNDARY"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"remote_path\"\r\n\r\na.txt\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.txt\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\nhello\r\n--{boundary}--\r\n"
    ).encode()
    headers = [
        (b"authorization", f"Bearer {KEY}".encode()),
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        (b"content-length", str(len(body)).encode()),
    ]
    asyncio.run(asyncio.wait_for(asgi_call(api, "POST", "/upload", headers, body), 10))
    assert cancelled == [True]
    assert list(tmp_dir.iterdir()) == []


def test_download_client_disconnect_during_bdpan_cancels_and_cleans(stub, tmp_dir):
    cancelled = []
    stub.results["ls"] = []
    stub.hooks["download"] = _hanging(cancelled)
    api = make_api(stub, tmp_dir)
    headers = [(b"authorization", f"Bearer {KEY}".encode())]
    asyncio.run(asyncio.wait_for(asgi_call(api, "GET", "/download", headers, query=b"path=a.txt"), 10))
    assert cancelled == [True]
    assert list(tmp_dir.iterdir()) == []


def test_download_client_disconnect_during_stream_cleans(tmp_path):
    work = tmp_path / "w"
    work.mkdir()
    (work / "a.txt").write_bytes(b"x" * 1024)
    response = _CleanupFileResponse(work / "a.txt", filename="a.txt", cleanup_dir=work)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_):
        raise OSError("client gone")

    scope = {"type": "http", "method": "GET", "headers": [], "path": "/download", "query_string": b""}
    with pytest.raises(Exception):
        asyncio.run(response(scope, receive, send))
    assert not work.exists()
