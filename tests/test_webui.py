import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.webui import COOKIE_NAME

KEY = "ui-key"
AUTH = {"Authorization": f"Bearer {KEY}"}


@pytest.fixture
def client(fake_bin, tmp_path, monkeypatch):
    # fake bdpan 的 stateful 模式强制要求该环境变量
    monkeypatch.setenv("FAKE_BDPAN_STORE", str(tmp_path / "drive"))
    app = create_app(
        KEY, fake_bin, download_dir=str(tmp_path / "dl"),
        tasks_file=str(tmp_path / "tasks.json"),
    )
    return TestClient(app)


# ---- 页面 ---------------------------------------------------------------


def test_pages_public(client):
    for path in ("/", "/logs", "/login"):
        res = client.get(path)
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "baidu-easy" in res.text
        assert "htmx.min.js" in res.text


def test_static_assets(client):
    assert client.get("/static/htmx.min.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200


def test_download_page_warns_without_key(client):
    res = client.get("/")
    assert "还没有设置访问密钥" in res.text


def test_nav_active_state(client):
    res = client.get("/logs")
    assert 'aria-current="page"' in res.text


# ---- 认证 ---------------------------------------------------------------


def test_protected_fragment_requires_key(client):
    res = client.get("/session/tasks")
    assert res.status_code == 401


def test_htmx_unauthorized_redirects(client):
    res = client.get(
        "/session/tasks", headers={"HX-Request": "true"}
    )
    # 错误密钥：htmx 响应 204 + HX-Redirect
    assert res.status_code == 204
    assert res.headers["hx-redirect"] == "/login"


def test_wrong_key_rejected_with_message(client):
    res = client.post(
        "/session/key", data={"key": "nope"},
    )
    assert "访问密钥无效" in res.text


def test_set_key_sets_cookie(client):
    res = client.post("/session/key", data={"key": KEY})
    assert res.status_code == 200
    cookie = res.cookies.get(COOKIE_NAME)
    assert cookie == KEY
    # 携带 Cookie 可访问受保护片段
    res2 = client.get("/session/tasks")
    assert res2.status_code == 200


def test_clear_key_deletes_cookie(client):
    client.cookies.set(COOKIE_NAME, KEY)
    assert client.get("/session/tasks").status_code == 200
    res = client.post("/session/key/clear")
    assert res.status_code == 200
    assert COOKIE_NAME in res.headers.get("set-cookie", "")
    # 模拟浏览器对过期 Cookie 的处理：cookie jar 中移除
    client.cookies.delete(COOKIE_NAME)
    assert client.get("/session/tasks").status_code == 401


# ---- 任务提交流程 -------------------------------------------------------


def test_submit_invalid_text(client):
    res = client.post("/session/tasks", data={"text": "没有链接的文字"}, headers=AUTH)
    assert res.status_code == 200
    assert 'id="task-msg"' in res.text
    assert "没有找到百度网盘分享链接" in res.text


def test_submit_valid_task_and_poll_state(client):
    text = "链接：https://pan.baidu.com/s/1abc?pwd=abcd"
    res = client.post("/session/tasks", data={"text": text}, headers=AUTH)
    assert res.status_code == 200
    assert res.headers.get("hx-trigger") == "task-created"
    assert "已提交" in res.text
    # 片段包含轮询定义（存在 queued 任务）
    assert "hx-trigger=\"load delay:2s\"" in res.text

    listing = client.get("/session/tasks", headers=AUTH).text
    assert "task-title" in listing
    # fake bdpan 执行很快，轮询时可能已从 queued 进入 running，甚至 done
    assert "排队中" in listing or "下载中" in listing or "完成" in listing


def test_task_detail_modal(client):
    # 先提交一个任务
    client.post(
        "/session/tasks",
        data={"text": "https://pan.baidu.com/s/1abc?pwd=abcd"}, headers=AUTH,
    )
    listing = client.get("/session/tasks", headers=AUTH).text
    import re

    task_id = re.search(r"task-id\">([0-9a-f]{12})", listing).group(1)
    res = client.get(f"/session/tasks/{task_id}/detail", headers=AUTH)
    assert res.status_code == 200
    assert task_id in res.text
    assert "dialog-body" in res.text


def test_delete_task(client):
    client.post(
        "/session/tasks",
        data={"text": "https://pan.baidu.com/s/1abc?pwd=abcd"}, headers=AUTH,
    )
    import re

    task_id = re.search(
        r"task-id\">([0-9a-f]{12})",
        client.get("/session/tasks", headers=AUTH).text,
    ).group(1)
    res = client.delete(f"/session/tasks/{task_id}", headers=AUTH)
    assert res.status_code == 200
    assert task_id not in res.text
    # 无活动任务：片段不再轮询
    assert "load delay:2s" not in res.text


# ---- 日志 / 状态 / 授权 -------------------------------------------------


def test_logs_fragment(client):
    res = client.get("/session/logs", headers=AUTH)
    assert res.status_code == 200
    assert "logs-root" in res.text


def test_logs_auto_polls(client):
    res = client.get("/session/logs?auto=on", headers=AUTH)
    assert "/session/logs?auto=on" in res.text


def test_logs_clear(client):
    res = client.post("/session/logs/clear", headers=AUTH)
    assert "暂无日志" in res.text


def test_status_panel(client):
    res = client.post("/session/status", headers=AUTH)
    # fake bdpan 未登录
    assert "未登录" in res.text


def test_login_url_requires_accept(client):
    res = client.post("/session/login/url", data={}, headers=AUTH)
    assert "安全须知" in res.text


def test_login_page_accept_checkbox_submits_with_url_form(client):
    import re

    html = client.get("/login").text
    checkbox = re.search(r'<input[^>]*name="accept_disclaimer"[^>]*>', html).group(0)
    owner = re.search(r'form="([^"]+)"', checkbox).group(1)
    assert re.search(rf'<form[^>]*id="{owner}"[^>]*hx-post="/session/login/url"', html)


def test_login_url_success(client):
    res = client.post(
        "/session/login/url", data={"accept_disclaimer": "on"}, headers=AUTH
    )
    assert "授权页面" in res.text


@pytest.mark.parametrize("code", ["abc", "", "z" * 32])
def test_login_code_format(client, code):
    res = client.post("/session/login/code", data={"code": code}, headers=AUTH)
    assert "授权码格式不正确" in res.text or "err" in res.text
