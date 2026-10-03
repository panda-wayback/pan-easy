import asyncio
import codecs
import json
import re
from typing import Any, Callable, Optional, Sequence

COMMANDS = frozenset({
    "whoami", "ls", "search", "upload", "download", "transfer",
    "share", "mkdir", "mv", "cp", "rename", "rm", "login",
})

UNBOUNDED = frozenset({"upload", "download", "transfer"})
LOGIN_TIMEOUT = 30.0
DEFAULT_TIMEOUT = 60.0

HINTS = {
    13003: "需要提取码，或提取码不正确，请检查后重试",
    13004: "分享链接已失效、已取消或不存在",
    13045: "这是你自己账号分享的链接，百度不允许转存自己的分享；文件本来就在你的网盘里",
    13070: "转存任务状态暂时查不到，可能仍在执行，请稍后再看，不要重复提交",
    13071: "已有其他转存任务正在进行，请等约 5 分钟后再试",
    13072: "已达到账号单次转存数量上限，请减少转存内容",
    13073: "已达到账号单次转存数量上限，请减少转存内容",
}

_ERRNO_RE = re.compile(r"(?:错误码|errno)\s*[=:：]?\s*(-?\d+)", re.IGNORECASE)
_LINE_START_JSON_RE = re.compile(r"(?m)^[\[{]")


class BdpanError(Exception):
    def __init__(self, kind: str, message: str, errno: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.errno = errno
        self.hint = HINTS.get(errno) if errno is not None else None


class Bdpan:
    def __init__(self, binary: str = "bdpan"):
        self.binary = binary

    async def run(
        self,
        command: str,
        positionals: Sequence[str] = (),
        flags: Sequence[str] = (),
        stdin: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> Any:
        if command not in COMMANDS:
            raise BdpanError("forbidden_command", f"不允许的子命令：{command}")

        argv = [self.binary, command, "--json", "--no-check-update", *flags]
        if positionals:
            argv += ["--", *positionals]

        return await self._exec(argv, command, stdin, on_output)

    async def run_subcommand(
        self,
        command: str,
        subcommand: str,
        positionals: Sequence[str] = (),
        flags: Sequence[str] = (),
        stdin: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> Any:
        """运行带子命令的 bdpan 命令，如 transfer list"""
        if command not in COMMANDS:
            raise BdpanError("forbidden_command", f"不允许的子命令：{command}")

        # 构建命令：bdpan command subcommand --json --no-check-update flags -- positionals
        argv = [self.binary, command, subcommand, "--json", "--no-check-update", *flags]
        if positionals:
            argv += ["--", *positionals]

        return await self._exec(argv, command, stdin, on_output)

    async def _exec(
        self,
        argv: list[str],
        command: str,
        stdin: Optional[str] = None,
        on_output: Optional[Callable[[str], None]] = None,
    ) -> Any:

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise BdpanError("not_found", f"找不到 bdpan 可执行文件：{self.binary}") from None

        timeout = _timeout_for(command)
        payload = stdin.encode() if stdin is not None else None
        try:
            stdout, stderr = await asyncio.wait_for(_collect(proc, payload, on_output), timeout)
        except asyncio.TimeoutError:
            await _kill(proc)
            raise BdpanError("timeout", f"bdpan {command} 超过 {timeout:.0f} 秒未完成") from None
        except asyncio.CancelledError:
            await _kill(proc)
            raise

        try:
            return _parse(stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))
        except BdpanError as err:
            if stdin:
                secret = stdin.strip()
                if secret:
                    err.message = err.message.replace(secret, "[已隐藏]")
                    err.args = (err.message,)
            raise


async def _collect(
    proc: asyncio.subprocess.Process,
    payload: Optional[bytes],
    on_output: Optional[Callable[[str], None]],
) -> tuple[bytes, bytes]:
    if payload is not None:
        try:
            proc.stdin.write(payload)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        proc.stdin.close()

    async def read(stream: asyncio.StreamReader) -> bytes:
        chunks = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while data := await stream.read(4096):
            chunks.append(data)
            if on_output is not None:
                text = decoder.decode(data)
                if text:
                    on_output(text)
        return b"".join(chunks)

    stdout, stderr = await asyncio.gather(read(proc.stdout), read(proc.stderr))
    await proc.wait()
    return stdout, stderr


def _timeout_for(command: str) -> Optional[float]:
    if command in UNBOUNDED:
        return None
    if command == "login":
        return LOGIN_TIMEOUT
    return DEFAULT_TIMEOUT


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
    await proc.wait()


def _parse(stdout: str, stderr: str) -> Any:
    found, data = _extract_json(stdout)
    if not found:
        message = stderr.strip() or stdout.strip()[-500:] or "bdpan 没有返回任何输出"
        raise _classify(message)
    if _is_envelope(data):
        if data["code"] != 0:
            raise _classify(str(data.get("error") or "bdpan 返回失败但没有说明原因"))
        return data.get("data")
    return data


def _extract_json(text: str) -> tuple[bool, Any]:
    stripped = text.strip()
    if not stripped:
        return False, None
    try:
        return True, json.loads(stripped)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for match in _LINE_START_JSON_RE.finditer(text):
        try:
            data, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if not text[end:].strip():
            return True, data
    return False, None


def _is_envelope(data: Any) -> bool:
    return isinstance(data, dict) and "code" in data and "error" in data and set(data) <= {"code", "data", "error"}


def _classify(message: str) -> BdpanError:
    match = _ERRNO_RE.search(message)
    errno = int(match.group(1)) if match else None
    lowered = message.lower()
    if "bdpan login" in lowered or "未登录" in message:
        return BdpanError("not_logged_in", message, errno)
    if "token" in lowered and ("过期" in message or "失效" in message or "expired" in lowered):
        return BdpanError("token_expired", message, errno)
    return BdpanError("bdpan_error", message, errno)
