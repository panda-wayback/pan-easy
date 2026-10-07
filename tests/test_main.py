import json
import os
import subprocess
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.fake_bdpan import VALID_CODE

ROOT = Path(__file__).resolve().parent.parent
KEY = "combo-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


def test_refuses_to_start_without_api_key():
    env = {k: v for k, v in os.environ.items() if k != "BAIDU_EASY_API_KEY"}
    proc = subprocess.run([sys.executable, "-m", "app.main"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0
    assert "BAIDU_EASY_API_KEY" in proc.stderr


def test_serves_web_page_without_key(fake_bin, tmp_path):
    client = TestClient(create_app(KEY, fake_bin, download_dir=str(tmp_path / "dl")))
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "baidu-easy" in res.text


def test_serves_public_page(fake_bin, tmp_path):
    client = TestClient(create_app(KEY, fake_bin, download_dir=str(tmp_path / "dl")))
    res = client.get("/public")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "网盘文件下载" in res.text


def test_combined_flow(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STORE", str(tmp_path / "drive"))
    work = tmp_path / "work"
    work.mkdir()
    downloads = tmp_path / "downloads"
    with TestClient(create_app(KEY, fake_bin, str(work), str(downloads))) as client:
        _combined_flow(client, work, downloads)


def _combined_flow(client, work, downloads):
    assert client.get("/").status_code == 200
    assert client.get("/api/status").status_code == 401

    res = client.get("/api/status", headers=AUTH)
    assert res.json() == {"ok": True, "data": {"authenticated": False, "has_valid_token": False}}

    res = client.get("/api/ls", headers=AUTH)
    assert res.status_code == 409 and res.json()["error"]["code"] == "not_logged_in"

    res = client.post("/api/login/url", json={"accept_disclaimer": True}, headers=AUTH)
    assert res.json()["data"]["auth_url"].startswith("https://")

    res = client.post("/api/login/code", json={"code": "b" * 32}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "auth_code_invalid"

    res = client.post("/api/login/code", json={"code": VALID_CODE}, headers=AUTH)
    assert res.status_code == 200 and res.json()["ok"] is True
    assert client.get("/api/status", headers=AUTH).json()["data"]["authenticated"] is True

    assert client.post("/api/mkdir", json={"path": "t"}, headers=AUTH).json()["ok"] is True

    res = client.post("/api/upload", files={"file": ("p.txt", b"payload")}, data={"remote_path": "t/p.txt"}, headers=AUTH)
    assert res.json() == {"ok": True, "data": {"saved_path": "我的应用数据/bdpan/t/p.txt"}}

    res = client.get("/api/ls", params={"path": "t"}, headers=AUTH)
    assert [i["server_filename"] for i in res.json()["data"]] == ["p.txt"]

    res = client.get("/api/download", params={"path": "t/p.txt"}, headers=AUTH)
    assert res.status_code == 200 and res.content == b"payload"

    res = client.get("/api/download", params={"path": "t"}, headers=AUTH)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_argument"

    res = client.get("/api/download", params={"path": "t/missing.txt"}, headers=AUTH)
    assert res.status_code == 502 and res.json()["error"]["errno"] == -9

    assert client.post("/api/rm", json={"paths": ["t"]}, headers=AUTH).json() == {"ok": True, "data": {"status": "ok"}}
    assert client.get("/api/ls", headers=AUTH).json()["data"] == []

    text = "通过百度网盘分享的文件：shared.bin\n链接：https://pan.baidu.com/s/1abc?pwd=PhPR\n复制这段内容打开「百度网盘APP 即可获取」"
    res = client.post("/api/tasks", json={"text": text}, headers=AUTH)
    assert res.status_code == 202
    task_id = res.json()["data"]["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=AUTH).json()["data"]
        if task["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert task["status"] == "done", task
    assert task["progress"] == 100 and task["saved_to"] == "shared.bin"
    assert task["pan_path"] == "我的应用数据/bdpan/2026-10-03/shared.bin"
    assert (downloads / "shared.bin").read_bytes() == b"shared-content"

    res = client.post(f"/api/tasks/{task_id}/link", headers=AUTH)
    assert res.status_code == 200
    url = res.json()["data"]["url"]
    assert url.startswith(f"http://testserver/dl/{task_id}?") and KEY not in url
    assert client.get(url).content == b"shared-content"

    assert list(work.iterdir()) == []


def _task(task_id, status="done", saved_to=None):
    return {"id": task_id, "url": "https://pan.baidu.com/s/1x", "status": status, "progress": 100, "saved_to": saved_to}


def test_download_link(fake_bin, tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    (downloads / "中文 名.txt").write_bytes(b"0123456789")
    (downloads / "gone.txt").write_bytes(b"x")
    tasks_file = tmp_path / "tasks.json"
    tasks = [
        _task("one", saved_to="中文 名.txt"),
        _task("multi", saved_to="."),
        _task("running", status="failed"),
        _task("gone", saved_to="gone.txt"),
        _task("escape", saved_to="../tasks.json"),
    ]
    tasks_file.write_text(json.dumps(tasks), encoding="utf-8")
    
    with TestClient(create_app(KEY, fake_bin, download_dir=str(downloads), tasks_file=str(tasks_file))) as client:
        def link(task_id, **body):
            return client.post(f"/api/tasks/{task_id}/link", headers=AUTH, json=body or None)

        res = link("one", expires_in=3600)
        data = res.json()["data"]
        assert res.status_code == 200 and data["expires_at"]
        url = data["url"].replace("http://testserver", "")

        got = client.get(url)
        assert got.status_code == 200 and got.content == b"0123456789"
        assert "filename*=utf-8''%E4%B8%AD%E6%96%87%20%E5%90%8D.txt" in got.headers["content-disposition"]
        part = client.get(url, headers={"Range": "bytes=2-5"})
        assert part.status_code == 206 and part.content == b"2345"
        assert client.head(url).status_code == 200

        query = dict(p.split("=") for p in url.split("?")[1].split("&"))
        bad_sig = f"/dl/one?exp={query['exp']}&sig={'0' * 32}"
        bad_exp = f"/dl/one?exp={int(query['exp']) + 1}&sig={query['sig']}"
        for bad in (bad_sig, bad_exp, "/dl/one", f"/dl/multi?exp={query['exp']}&sig={query['sig']}"):
            res = client.get(bad)
            assert res.status_code == 403 and res.json()["error"]["code"] == "link_invalid", bad

        from app.api import _sign

        past = int(time.time()) - 10
        res = client.get(f"/dl/one?exp={past}&sig={_sign(KEY, 'one', past)}")
        assert res.status_code == 403 and "过期" in res.json()["error"]["message"]

        assert link("multi").status_code == 409
        assert link("running").json()["error"]["code"] == "task_not_ready"
        assert link("nope").status_code == 404
        assert link("escape").json()["error"]["code"] == "file_missing"
        proxied = client.post(
            "/api/tasks/one/link",
            headers={**AUTH, "Host": "127.0.0.1:8080", "X-Forwarded-Proto": "https", "X-Forwarded-Host": "pan.example.com, inner"},
        )
        assert proxied.json()["data"]["url"].startswith("https://pan.example.com/dl/one?")
        host_only = client.post("/api/tasks/one/link", headers={**AUTH, "Host": "pan.example.com:8080"})
        assert host_only.json()["data"]["url"].startswith("http://pan.example.com:8080/dl/one?")

        assert link("one", expires_in=10).status_code == 400
        assert link("one", expires_in=604801).status_code == 400
        assert client.post("/api/tasks/one/link").status_code == 401

        gone_url = link("gone").json()["data"]["url"].replace("http://testserver", "")
        (downloads / "gone.txt").unlink()
        res = client.get(gone_url)
        assert res.status_code == 404 and res.json()["error"]["code"] == "file_missing"

        res = client.post("/api/tasks/one/retry", headers=AUTH)
        assert res.status_code == 409

        res = client.post("/api/tasks/running/retry", headers=AUTH)
        assert res.status_code == 202
        data = res.json()["data"]
        assert data["id"] != "running" and data["status"] == "queued"

        res = client.delete("/api/tasks/one", headers=AUTH)
        assert res.status_code == 200
        assert client.get("/api/tasks/one", headers=AUTH).status_code == 404
