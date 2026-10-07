import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from shop.app import create_app

ROOT = Path(__file__).resolve().parents[2]
ACCESS = "shop-key"
MASTER = "master-key"
AUTH = {"Authorization": f"Bearer {ACCESS}"}
SUBMIT = {"text": "https://pan.baidu.com/s/1abc"}


class Upstream:
    """替身 baidu-easy：记录收到的请求，按 (方法, 路径) 返回预设响应。"""

    def __init__(self):
        self.calls = []
        self.responses = {
            ("POST", "/api/tasks"): lambda: JSONResponse(
                {"ok": True, "data": {"id": "abc123", "url": SUBMIT["text"], "status": "queued"}}, status_code=202),
        }
        self.app = FastAPI()

        @self.app.api_route("/{path:path}", methods=["GET", "POST", "HEAD"])
        async def handle(path: str, request: Request):
            self.calls.append({
                "method": request.method,
                "path": "/" + path,
                "query": request.url.query,
                "headers": dict(request.headers),
                "body": await request.body(),
            })
            make = self.responses.get((request.method, "/" + path))
            return make() if make else JSONResponse({"ok": True, "data": {"echo": "/" + path}})


@pytest.fixture
def upstream():
    return Upstream()


@pytest.fixture
def client(upstream):
    app = create_app(ACCESS, MASTER, "http://baidu-easy", transport=httpx.ASGITransport(app=upstream.app))
    with TestClient(app) as c:
        yield c


def submit(client):
    res = client.post("/tasks", json=SUBMIT, headers=AUTH)
    assert res.status_code == 202
    return res.json()["data"]


def test_pages_without_key(client, upstream):
    for path in ("/", "/t/anything"):
        res = client.get(path)
        assert res.status_code == 200 and "text/html" in res.headers["content-type"]
        assert "网盘文件下载" in res.text
    assert upstream.calls == []


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {MASTER}"}])
def test_submit_rejects_missing_or_wrong_key(client, upstream, headers):
    res = client.post("/tasks", json=SUBMIT, headers=headers)
    assert res.status_code == 401 and res.json()["error"]["code"] == "unauthorized"
    assert upstream.calls == []


def test_no_task_listing(client, upstream):
    assert client.get("/tasks", headers=AUTH).status_code == 405
    assert client.get("/tasks/abc123", headers=AUTH).status_code == 404
    assert upstream.calls == []


def test_submit_creates_page(client, upstream):
    data = submit(client)
    call = upstream.calls[-1]
    assert (call["method"], call["path"]) == ("POST", "/api/tasks")
    assert json.loads(call["body"]) == SUBMIT
    assert call["headers"]["authorization"] == f"Bearer {MASTER}"
    assert ACCESS not in str(call["headers"])

    assert data["id"] == "abc123" and data["page"].startswith("/t/abc123.")
    assert ACCESS not in data["page"] and MASTER not in data["page"]
    remaining = datetime.fromisoformat(data["page_expires_at"]).timestamp() - time.time()
    assert 86400 - 10 < remaining <= 86400


def test_submit_error_passes_through(client, upstream):
    error = {"ok": False, "error": {"code": "invalid_argument", "message": "文字中没有找到百度网盘分享链接"}}
    upstream.responses[("POST", "/api/tasks")] = lambda: JSONResponse(error, status_code=400)
    res = client.post("/tasks", json={"text": "x"}, headers=AUTH)
    assert res.status_code == 400 and res.json() == error


def test_page_task_and_link(client, upstream):
    page = submit(client)["page"]
    upstream.responses[("GET", "/api/tasks/abc123")] = lambda: JSONResponse(
        {"ok": True, "data": {"id": "abc123", "status": "done"}})

    res = client.get(f"{page}/task")
    assert res.status_code == 200 and res.json()["data"]["id"] == "abc123"
    call = upstream.calls[-1]
    assert (call["method"], call["path"]) == ("GET", "/api/tasks/abc123")
    assert call["headers"]["authorization"] == f"Bearer {MASTER}"

    client.post(f"{page}/link")
    call = upstream.calls[-1]
    assert (call["method"], call["path"]) == ("POST", "/api/tasks/abc123/link")
    assert 86400 - 10 < json.loads(call["body"])["expires_in"] <= 86400
    assert call["headers"]["x-forwarded-proto"] == "http"
    assert call["headers"]["x-forwarded-host"] == "testserver"

    client.post(f"{page}/link", headers={"X-Forwarded-Proto": "https",
                                         "X-Forwarded-Host": "shop.example.com, inner"})
    assert upstream.calls[-1]["headers"]["x-forwarded-proto"] == "https"
    assert upstream.calls[-1]["headers"]["x-forwarded-host"] == "shop.example.com"


def test_tampered_page_rejected(client, upstream):
    token = submit(client)["page"][len("/t/"):]
    task_id, exp, sig = token.split(".")
    calls = len(upstream.calls)
    for bad in (f"other.{exp}.{sig}", f"{task_id}.{int(exp) + 1}.{sig}", f"{task_id}.{exp}.{'0' * 32}",
                f"{task_id}.{exp}", "a.b.c", "x"):
        for res in (client.get(f"/t/{bad}/task"), client.post(f"/t/{bad}/link")):
            assert res.status_code == 404 and res.json()["error"]["code"] == "page_not_found", bad
    assert len(upstream.calls) == calls


def test_expired_page(client, upstream, monkeypatch):
    page = submit(client)["page"]
    calls = len(upstream.calls)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 86400)
    for res in (client.get(f"{page}/task"), client.post(f"{page}/link")):
        assert res.status_code == 410 and res.json()["error"]["code"] == "page_expired"
    assert len(upstream.calls) == calls


def test_dl_forwards_range(client, upstream):
    headers = {"Content-Range": "bytes 2-5/10", "Accept-Ranges": "bytes",
               "Content-Disposition": "attachment; filename=\"a.txt\""}
    upstream.responses[("GET", "/dl/abc")] = lambda: Response(b"2345", status_code=206, headers=headers,
                                                              media_type="application/octet-stream")
    upstream.responses[("HEAD", "/dl/abc")] = lambda: Response(status_code=200, headers=headers)

    res = client.get("/dl/abc?exp=1&sig=xyz", headers={"Range": "bytes=2-5"})
    assert res.status_code == 206 and res.content == b"2345"
    assert res.headers["content-range"] == "bytes 2-5/10"
    assert "a.txt" in res.headers["content-disposition"]
    call = upstream.calls[-1]
    assert call["query"] == "exp=1&sig=xyz" and call["headers"]["range"] == "bytes=2-5"
    assert "authorization" not in call["headers"]

    head = client.head("/dl/abc?exp=1&sig=xyz")
    assert head.status_code == 200 and head.content == b""

    res = client.get("/dl/a.b")
    assert res.status_code == 404 and res.json()["error"]["code"] == "task_not_found"


def test_upstream_unreachable():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    app = create_app(ACCESS, MASTER, "http://baidu-easy", transport=httpx.MockTransport(refuse))
    with TestClient(app) as client:
        for res in (client.post("/tasks", json=SUBMIT, headers=AUTH), client.get("/dl/abc?exp=1&sig=x")):
            assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_unavailable"


def test_upstream_rejects_master_key(client, upstream):
    error = {"ok": False, "error": {"code": "unauthorized", "message": "缺少或错误的访问密钥"}}
    upstream.responses[("POST", "/api/tasks")] = lambda: JSONResponse(error, status_code=401)
    res = client.post("/tasks", json=SUBMIT, headers=AUTH)
    assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.parametrize("missing", ["SHOP_ACCESS_KEY", "BAIDU_EASY_API_KEY"])
def test_refuses_to_start_without_keys(missing):
    env = {**os.environ, "SHOP_ACCESS_KEY": ACCESS, "BAIDU_EASY_API_KEY": MASTER}
    env.pop(missing)
    proc = subprocess.run([sys.executable, "-m", "shop.app"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and missing in proc.stderr
