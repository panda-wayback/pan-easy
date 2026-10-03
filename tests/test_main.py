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

    assert list(work.iterdir()) == []
