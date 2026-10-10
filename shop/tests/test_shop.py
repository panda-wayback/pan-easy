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

from shop.app import cost_uses, create_app, title_from_names

ROOT = Path(__file__).resolve().parents[2]
MASTER = "master-key"
CARD = "AAAAA-BBBBB-CCCCC-DDDDD-EEEEE"
SHARE = "https://pan.baidu.com/s/1abc"
SUBMIT = {"text": SHARE, "code": CARD}
PREVIEW_BYTES = 50_000_000  # → cost 1


class Fake:
    """替身服务：记录收到的请求，按 (方法, 路径) 返回预设响应。"""

    def __init__(self, responses):
        self.calls = []
        self.responses = responses
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
    return Fake({
        ("POST", "/api/tasks/preview"): lambda: JSONResponse(
            {"ok": True, "data": {"total_bytes": PREVIEW_BYTES, "names": ["demo.bin"]}}),
        ("POST", "/api/tasks/space-check"): lambda: JSONResponse(
            {"ok": True, "data": {"free_bytes": 10_000_000_000}}),
        ("POST", "/api/tasks"): lambda: JSONResponse(
            {"ok": True, "data": {"id": "abc123", "url": SHARE, "status": "queued"}}, status_code=202),
    })


@pytest.fixture
def spark():
    return Fake({
        ("POST", "/api/redeem/status"): lambda: JSONResponse(
            {"ok": True, "uses": 10, "used": 0, "remaining": 10}),
        ("POST", "/api/redeem"): lambda: JSONResponse(
            {"ok": True, "remaining": 9, "redeemed_at": "2026-10-07T23:00:00+08:00"}),
    })


@pytest.fixture
def client(upstream, spark):
    app = create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                     transport=httpx.ASGITransport(app=upstream.app),
                     auth_transport=httpx.ASGITransport(app=spark.app))
    with TestClient(app) as c:
        yield c


def submit(client):
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 202
    return res.json()["data"]


def spark_error(code, status=403):
    return lambda: JSONResponse({"ok": False, "error": {"code": code, "message": "x"}}, status_code=status)


def test_pages_without_card(client, upstream, spark):
    for path in ("/", "/t/anything"):
        res = client.get(path)
        assert res.status_code == 200 and "text/html" in res.headers["content-type"]
        assert "网盘文件下载" in res.text
        assert 'id="clear-code"' in res.text and 'id="submit-code"' in res.text
        assert 'id="code-pin"' in res.text and "卡密保存在本浏览器" in res.text
    assert upstream.calls == [] and spark.calls == []


@pytest.mark.parametrize("body", [
    {"text": "没有链接", "code": CARD},
    {"text": "https://pan.example.com/s/1abc", "code": CARD},
    {"text": SHARE, "code": "  "},
    {"text": SHARE},
    ["not", "object"],
])
def test_submit_checks_before_redeem(client, upstream, spark, body):
    res = client.post("/tasks", json=body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert upstream.calls == [] and spark.calls == []


def test_submit_rejects_non_json(client, upstream, spark):
    res = client.post("/tasks", content=b"not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert upstream.calls == [] and spark.calls == []


def test_submit_rejects_task_too_large(upstream, spark):
    upstream.responses[("POST", "/api/tasks/preview")] = lambda: JSONResponse(
        {"ok": True, "data": {"total_bytes": 5_000, "names": ["big.bin"]}})
    app = create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                     transport=httpx.ASGITransport(app=upstream.app),
                     auth_transport=httpx.ASGITransport(app=spark.app),
                     max_task_bytes=1000)
    with TestClient(app) as client:
        res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 409 and res.json()["error"]["code"] == "task_too_large"
    assert [("POST", c["path"]) for c in upstream.calls] == [("POST", "/api/tasks/preview")]
    assert spark.calls == []


def test_submit_rejects_disk_full_without_redeem(client, upstream, spark):
    upstream.responses[("POST", "/api/tasks/space-check")] = lambda: JSONResponse(
        {"ok": False, "error": {"code": "disk_full", "message": "空间不足"}}, status_code=507)
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 507 and res.json()["error"]["code"] == "disk_full"
    assert [c["path"] for c in upstream.calls] == ["/api/tasks/preview", "/api/tasks/space-check"]
    assert spark.calls == []


@pytest.mark.parametrize("total, expected", [
    (0, 1),
    (1, 1),
    (50_000_000, 1),
    (100_000_000, 1),
    (100_000_001, 2),
    (150_000_000, 2),
    (1_000_000_000, 10),
])
def test_cost_uses(total, expected):
    assert cost_uses(total) == expected


@pytest.mark.parametrize("names, title", [
    ([], None),
    (["a.pdf"], "a.pdf"),
    (["a.pdf", "b.docx"], "a.pdf 等2个文件"),
    (["x", "y", "z"], "x 等3个文件"),
])
def test_title_from_names(names, title):
    assert title_from_names(names) == title


@pytest.mark.parametrize("spark_code, status, code", [
    ("CODE_INVALID", 403, "card_invalid"),
    ("REQUEST_INVALID", 400, "card_invalid"),
    ("SOMETHING_NEW", 403, "card_invalid"),
    ("CODE_USED", 403, "card_used"),
    ("CODE_TYPE_MISMATCH", 403, "card_type_mismatch"),
    ("BATCH_DISABLED", 403, "card_disabled"),
    ("PRODUCT_DISABLED", 403, "card_disabled"),
])
def test_redeem_failure_maps_error(client, upstream, spark, spark_code, status, code):
    spark.responses[("POST", "/api/redeem")] = spark_error(spark_code, status)
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 403 and res.json()["error"]["code"] == code
    assert CARD not in res.text
    assert [c["path"] for c in spark.calls] == ["/api/redeem/status", "/api/redeem"]
    assert [c["path"] for c in upstream.calls] == ["/api/tasks/preview", "/api/tasks/space-check"]


def test_redeem_unavailable(upstream):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    def not_json(request):
        return httpx.Response(200, content=b"<html>")

    for transport in (httpx.MockTransport(refuse), httpx.MockTransport(not_json)):
        app = create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                         transport=httpx.ASGITransport(app=upstream.app), auth_transport=transport)
        with TestClient(app) as client:
            res = client.post("/tasks", json=SUBMIT)
            assert res.status_code == 502 and res.json()["error"]["code"] == "auth_unavailable"
            assert [c["path"] for c in upstream.calls] == ["/api/tasks/preview", "/api/tasks/space-check"]
            upstream.calls.clear()


def test_no_task_listing(client, upstream):
    assert client.get("/tasks").status_code == 405
    assert client.get("/tasks/abc123").status_code == 404
    assert upstream.calls == []


def test_card_status_returns_balance(client, upstream, spark):
    res = client.post("/card/status", json={"code": CARD})
    assert res.status_code == 200
    assert res.json()["data"] == {"remaining": 10, "used": 0, "uses": 10}
    assert [c["path"] for c in spark.calls] == ["/api/redeem/status"]
    assert json.loads(spark.calls[0]["body"]) == {"code": CARD}
    assert upstream.calls == []


@pytest.mark.parametrize("body", [
    {"code": "  "},
    {},
    ["not", "object"],
])
def test_card_status_requires_code(client, upstream, spark, body):
    res = client.post("/card/status", json=body)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert spark.calls == [] and upstream.calls == []


def test_card_status_non_json(client, upstream, spark):
    res = client.post("/card/status", content=b"not json",
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"
    assert spark.calls == [] and upstream.calls == []


@pytest.mark.parametrize("spark_code, code", [
    ("CODE_INVALID", "card_invalid"),
    ("CODE_USED", "card_used"),
    ("CODE_TYPE_MISMATCH", "card_type_mismatch"),
    ("BATCH_DISABLED", "card_disabled"),
])
def test_card_status_maps_errors(upstream, spark, spark_code, code):
    spark.responses[("POST", "/api/redeem/status")] = spark_error(spark_code)
    app = create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                     transport=httpx.ASGITransport(app=upstream.app),
                     auth_transport=httpx.ASGITransport(app=spark.app))
    with TestClient(app) as client:
        res = client.post("/card/status", json={"code": CARD})
    assert res.status_code == 403 and res.json()["error"]["code"] == code
    assert CARD not in res.text


def test_card_status_missing_field_from_spark(upstream, spark):
    spark.responses[("POST", "/api/redeem/status")] = lambda: JSONResponse(
        {"ok": True, "remaining": 9, "used": 1})  # 缺 uses
    app = create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                     transport=httpx.ASGITransport(app=upstream.app),
                     auth_transport=httpx.ASGITransport(app=spark.app))
    with TestClient(app) as client:
        res = client.post("/card/status", json={"code": CARD})
    assert res.status_code == 502 and res.json()["error"]["code"] == "auth_unavailable"


def test_preview_failure_skips_auth(client, upstream, spark):
    error = {"ok": False, "error": {"code": "bdpan_error", "message": "提取码错误", "errno": -9}}
    upstream.responses[("POST", "/api/tasks/preview")] = lambda: JSONResponse(error, status_code=502)
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 502 and res.json() == error
    assert spark.calls == []
    assert [c["path"] for c in upstream.calls] == ["/api/tasks/preview"]


def test_insufficient_remaining_skips_redeem(client, upstream, spark):
    spark.responses[("POST", "/api/redeem/status")] = lambda: JSONResponse(
        {"ok": True, "uses": 10, "used": 10, "remaining": 0})
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 403 and res.json()["error"]["code"] == "card_used"
    assert [c["path"] for c in spark.calls] == ["/api/redeem/status"]
    assert [c["path"] for c in upstream.calls] == ["/api/tasks/preview", "/api/tasks/space-check"]


def test_submit_redeems_then_creates_page(client, upstream, spark):
    data = submit(client)
    assert [c["path"] for c in upstream.calls] == [
        "/api/tasks/preview", "/api/tasks/space-check", "/api/tasks"]
    assert json.loads(upstream.calls[0]["body"]) == {"text": SHARE}
    assert json.loads(upstream.calls[1]["body"]) == {"bytes": PREVIEW_BYTES}
    assert json.loads(upstream.calls[2]["body"]) == {"text": SHARE}
    assert upstream.calls[2]["headers"]["authorization"] == f"Bearer {MASTER}"

    assert [c["path"] for c in spark.calls] == ["/api/redeem/status", "/api/redeem"]
    assert json.loads(spark.calls[0]["body"]) == {"code": CARD}
    assert json.loads(spark.calls[1]["body"]) == {"code": CARD, "count": 1}
    assert "authorization" not in spark.calls[0]["headers"]

    assert data["id"] == "abc123" and data["page"].startswith("/t/abc123.")
    assert data["cost"] == 1 and data["remaining"] == 9 and data["uses"] == 10
    assert data["total_bytes"] == PREVIEW_BYTES
    assert data["title"] == "demo.bin"
    assert CARD not in json.dumps(data) and MASTER not in data["page"]
    remaining = datetime.fromisoformat(data["page_expires_at"]).timestamp() - time.time()
    assert 86400 - 10 < remaining <= 86400


def test_submit_title_for_multi_file(client, upstream, spark):
    upstream.responses[("POST", "/api/tasks/preview")] = lambda: JSONResponse(
        {"ok": True, "data": {"total_bytes": PREVIEW_BYTES,
                              "names": ["缠中说禅.pdf", "其它.docx"]}})
    data = submit(client)
    assert data["title"] == "缠中说禅.pdf 等2个文件"


@pytest.mark.parametrize("total_bytes, cost", [
    (50_000_000, 1),
    (100_000_000, 1),
    (150_000_000, 2),
    (1_000_000_000, 10),
])
def test_submit_cost_by_size(client, upstream, spark, total_bytes, cost):
    upstream.responses[("POST", "/api/tasks/preview")] = lambda: JSONResponse(
        {"ok": True, "data": {"total_bytes": total_bytes, "names": ["x.bin"]}})
    spark.responses[("POST", "/api/redeem")] = lambda: JSONResponse(
        {"ok": True, "remaining": 100 - cost, "redeemed_at": "2026-10-07T23:00:00+08:00"})
    data = submit(client)
    assert data["cost"] == cost and data["remaining"] == 100 - cost
    assert json.loads(spark.calls[-1]["body"]) == {"code": CARD, "count": cost}


def test_submit_error_passes_through(client, upstream, spark):
    error = {"ok": False, "error": {"code": "invalid_argument", "message": "文字中没有找到百度网盘分享链接"}}
    upstream.responses[("POST", "/api/tasks")] = lambda: JSONResponse(error, status_code=400)
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 400 and res.json() == error
    assert len(spark.calls) == 2


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
        for res in (client.get(f"/t/{bad}/task"), client.post(f"/t/{bad}/link"), client.post(f"/t/{bad}/retry")):
            assert res.status_code == 404 and res.json()["error"]["code"] == "page_not_found", bad
    assert len(upstream.calls) == calls


def test_expired_page(client, upstream, monkeypatch):
    page = submit(client)["page"]
    calls = len(upstream.calls)
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 86400)
    for res in (client.get(f"{page}/task"), client.post(f"{page}/link"), client.post(f"{page}/retry")):
        assert res.status_code == 410 and res.json()["error"]["code"] == "page_expired"
    assert len(upstream.calls) == calls


def retry_ok(new_id):
    return lambda: JSONResponse({"ok": True, "data": {"id": new_id, "url": SHARE, "status": "queued"}},
                                status_code=202)


def test_retry_switches_page_to_new_task_without_redeem(client, upstream, spark):
    page = submit(client)["page"]
    spark_calls = len(spark.calls)
    upstream.responses[("POST", "/api/tasks/abc123/retry")] = retry_ok("new1")

    res = client.post(f"{page}/retry")
    assert res.status_code == 202
    assert res.json()["data"]["id"] == "new1" and res.json()["data"]["retries_left"] == 2
    assert len(spark.calls) == spark_calls
    call = upstream.calls[-1]
    assert (call["method"], call["path"]) == ("POST", "/api/tasks/abc123/retry")
    assert call["headers"]["authorization"] == f"Bearer {MASTER}"

    res = client.get(f"{page}/task")
    assert upstream.calls[-1]["path"] == "/api/tasks/new1"
    assert res.json()["data"]["retries_left"] == 2
    client.post(f"{page}/link")
    assert upstream.calls[-1]["path"] == "/api/tasks/new1/link"


def test_retry_limit(client, upstream, spark):
    page = submit(client)["page"]
    spark_calls = len(spark.calls)
    assert client.get(f"{page}/task").json()["data"]["retries_left"] == 3
    for old, new in (("abc123", "r1"), ("r1", "r2"), ("r2", "r3")):
        upstream.responses[("POST", f"/api/tasks/{old}/retry")] = retry_ok(new)
        assert client.post(f"{page}/retry").status_code == 202
    calls = len(upstream.calls)
    res = client.post(f"{page}/retry")
    assert res.status_code == 409 and res.json()["error"]["code"] == "retry_exhausted"
    assert len(upstream.calls) == calls and len(spark.calls) == spark_calls
    assert client.get(f"{page}/task").json()["data"]["retries_left"] == 0


def test_retry_rejected_by_upstream_not_counted(client, upstream):
    page = submit(client)["page"]
    error = {"ok": False, "error": {"code": "task_not_ready", "message": "任务状态不可重试"}}
    upstream.responses[("POST", "/api/tasks/abc123/retry")] = lambda: JSONResponse(error, status_code=409)
    res = client.post(f"{page}/retry")
    assert res.status_code == 409 and res.json() == error
    client.get(f"{page}/task")
    assert upstream.calls[-1]["path"] == "/api/tasks/abc123"


def test_retry_persists_across_restart(upstream, spark, tmp_path):
    retries_file = str(tmp_path / "data" / "shop-retries.json")

    def make():
        return create_app(MASTER, "http://baidu-easy", "http://spark-auth",
                          transport=httpx.ASGITransport(app=upstream.app),
                          auth_transport=httpx.ASGITransport(app=spark.app), retries_file=retries_file)

    upstream.responses[("POST", "/api/tasks/abc123/retry")] = retry_ok("new1")
    with TestClient(make()) as client:
        page = submit(client)["page"]
        assert client.post(f"{page}/retry").status_code == 202
    with TestClient(make()) as client:
        res = client.get(f"{page}/task")
        assert upstream.calls[-1]["path"] == "/api/tasks/new1"
        assert res.json()["data"]["retries_left"] == 2


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


def test_upstream_unreachable(spark):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    app = create_app(MASTER, "http://baidu-easy", "http://spark-auth", transport=httpx.MockTransport(refuse),
                     auth_transport=httpx.ASGITransport(app=spark.app))
    with TestClient(app) as client:
        for res in (client.post("/tasks", json=SUBMIT), client.get("/dl/abc?exp=1&sig=x")):
            assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_unavailable"


def test_upstream_rejects_master_key(client, upstream):
    error = {"ok": False, "error": {"code": "unauthorized", "message": "缺少或错误的访问密钥"}}
    upstream.responses[("POST", "/api/tasks")] = lambda: JSONResponse(error, status_code=401)
    res = client.post("/tasks", json=SUBMIT)
    assert res.status_code == 502 and res.json()["error"]["code"] == "upstream_unavailable"


@pytest.mark.parametrize("missing", ["BAIDU_EASY_API_KEY", "SPARK_AUTH_URL"])
def test_refuses_to_start_without_config(missing):
    env = {**os.environ, "BAIDU_EASY_API_KEY": MASTER, "SPARK_AUTH_URL": "http://spark-auth"}
    env.pop(missing)
    proc = subprocess.run([sys.executable, "-m", "shop.app"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and missing in proc.stderr
