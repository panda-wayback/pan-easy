import asyncio
import json
import os
import time

import pytest

import app.bdpan as bdpan_mod
from app.bdpan import Bdpan, BdpanError


def run(coro):
    return asyncio.run(coro)


def read_log(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


def wait_dead(pid, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def log(tmp_path, monkeypatch):
    path = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_BDPAN_LOG", str(path))
    return path


def test_argv_includes_json_and_no_update_and_dashdash(fake_bin, log, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", "[]")
    assert run(Bdpan(fake_bin).run("ls", ["-weird"], ["--order", "name"])) == []
    assert read_log(log)[0]["argv"] == ["ls", "--json", "--no-check-update", "--order", "name", "--", "-weird"]


def test_no_positionals_omits_dashdash(fake_bin, log, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", "{}")
    run(Bdpan(fake_bin).run("whoami"))
    assert read_log(log)[0]["argv"] == ["whoami", "--json", "--no-check-update"]


def test_envelope_success_returns_data(fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", json.dumps({"code": 0, "data": {"saved_path": "x"}, "error": ""}))
    assert run(Bdpan(fake_bin).run("upload", ["a", "b"])) == {"saved_path": "x"}


def test_bare_object_passed_through(fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", json.dumps({"status": "ok", "code": 0, "return_url": "u"}))
    assert run(Bdpan(fake_bin).run("mkdir", ["a"])) == {"status": "ok", "code": 0, "return_url": "u"}


def test_progress_text_before_json_is_skipped(fake_bin, monkeypatch):
    out = "下载中   0% | (0/17 B)\r下载中 100% | (17/17 B)\n\n" + json.dumps({"code": 0, "data": {"local": "x"}, "error": ""})
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", out)
    assert run(Bdpan(fake_bin).run("download", ["a", "b"])) == {"local": "x"}


@pytest.mark.parametrize(
    "message, kind, errno, has_hint",
    [
        ("请先执行 bdpan login 命令", "not_logged_in", None, False),
        ("Token 已过期，请重新登录", "token_expired", None, False),
        ("找不到指定的文件或目录（错误码 -9），请检查路径是否正确。", "bdpan_error", -9, False),
        ("转存失败 errno=13003", "bdpan_error", 13003, True),
        ("转存失败: 分享接口失败: errno=13045, msg=prohibit transfer self share link", "bdpan_error", 13045, True),
    ],
)
def test_error_normalization(fake_bin, monkeypatch, message, kind, errno, has_hint):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", json.dumps({"code": 1, "data": None, "error": message}))
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(fake_bin).run("ls"))
    assert (exc.value.kind, exc.value.errno, exc.value.message) == (kind, errno, message)
    assert bool(exc.value.hint) is has_hint


def test_non_json_output_uses_stderr(fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", "")
    monkeypatch.setenv("FAKE_BDPAN_STDERR", "boom")
    monkeypatch.setenv("FAKE_BDPAN_EXIT", "2")
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(fake_bin).run("ls"))
    assert exc.value.kind == "bdpan_error" and exc.value.message == "boom"


def test_timeout_kills_process(fake_bin, log, monkeypatch):
    monkeypatch.setattr(bdpan_mod, "DEFAULT_TIMEOUT", 2.0)
    monkeypatch.setenv("FAKE_BDPAN_SLEEP", "10")
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", "{}")
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(fake_bin).run("ls"))
    assert exc.value.kind == "timeout"
    assert wait_dead(read_log(log)[0]["pid"])


def test_unbounded_commands_have_no_timeout():
    assert bdpan_mod._timeout_for("upload") is None
    assert bdpan_mod._timeout_for("download") is None
    assert bdpan_mod._timeout_for("transfer") is None
    assert bdpan_mod._timeout_for("login") == bdpan_mod.LOGIN_TIMEOUT
    assert bdpan_mod._timeout_for("ls") == bdpan_mod.DEFAULT_TIMEOUT


def test_cancel_kills_process(fake_bin, log, monkeypatch):
    monkeypatch.setenv("FAKE_BDPAN_SLEEP", "10")
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", "{}")

    async def scenario():
        task = asyncio.ensure_future(Bdpan(fake_bin).run("download", ["a", "b"]))
        for _ in range(100):
            await asyncio.sleep(0.05)
            if log.exists():
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert wait_dead(read_log(log)[0]["pid"])


def test_forbidden_command_does_not_start_process(fake_bin, log):
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(fake_bin).run("update"))
    assert exc.value.kind == "forbidden_command"
    assert not log.exists()


def test_missing_binary(tmp_path):
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(str(tmp_path / "nope")).run("whoami"))
    assert exc.value.kind == "not_found"


def test_on_output_receives_output_and_result_still_parsed(fake_bin, monkeypatch):
    out = json.dumps({"code": 0, "data": {"local": "x"}, "error": ""})
    progress = "下载中  50% |\r下载中 100% |\r"
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", out)
    monkeypatch.setenv("FAKE_BDPAN_STDERR", progress)
    chunks = []
    assert run(Bdpan(fake_bin).run("download", ["a", "b"], on_output=chunks.append)) == {"local": "x"}
    joined = "".join(chunks)
    assert out in joined and progress in joined


def test_auth_code_only_via_stdin_and_hidden_in_errors(fake_bin, log, monkeypatch):
    code = "0123456789abcdef0123456789abcdef"
    monkeypatch.setenv("FAKE_BDPAN_STDOUT", json.dumps({"code": 1, "data": None, "error": f"授权码 {code} 无效"}))
    with pytest.raises(BdpanError) as exc:
        run(Bdpan(fake_bin).run("login", flags=["--set-code-stdin"], stdin=code + "\n"))
    call = read_log(log)[0]
    assert code not in " ".join(call["argv"])
    assert call["stdin"].strip() == code
    assert code not in exc.value.message and code not in str(exc.value)
