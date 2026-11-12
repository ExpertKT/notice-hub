"""Cloudflare 快速通道（trycloudflare.com）：不注册账号、不买域名，一条命令就有一个公网地址。

用户 2026-11-07 拍板「两条通道都留，默认 Cloudflare」：手机用流量要能打开网页，
而 Tailscale 要登录账号、偶尔还得去管理后台点批准，对「只想让手机看一眼待办」太重。
快速通道的代价是地址每次重启都会变（状态接口里的 ``url_changes_on_restart``），
要固定地址时切回 Funnel。

每一步都按「成功必须是验证出来的」写：下载完要真跑一次 ``--version``，
拿到地址要真从公网取一次 ``/api/ping``；任何一个环节不通过都进 error 并给中文原因，
绝不留一个坏 exe、也绝不把「命令发出去了」当成「开成了」。
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Callable

from . import tunnel
from .win_text import decode_console_output

LOGGER = logging.getLogger(__name__)

BINARY_VERSION = "2026.10.7"
# 固定版本、固定大小、固定哈希：cloudflared 是能从公网把整站反代出去的程序，
# 「随便下个最新版就跑」等于把用户机器的网络能力交给一个没验过的二进制。
# 这个哈希和 cloudflared 自己启动时自报的 Checksum 一致（2026.10.7 / Windows amd64）。
BINARY_BYTES = 55_390_512
BINARY_SHA256 = "2493dbc168923bb3e59e5e7716f71deced92992eea9acd6e9d74f84ac7751343"

# Gitee 拒收裸 exe（403 malicious file detected and rejected），镜像只能发 zip。
# 这个 zip 里只装那一个 exe，解出来必须还是上面的 BINARY_BYTES / BINARY_SHA256。
ZIP_BYTES = 18_836_264
ZIP_SHA256 = "1ac9146a3a7012455fdf182695429397c0ecfbfa4bcba1ad732c57ae9b3267c6"
ZIP_MEMBER = "cloudflared-windows-amd64.exe"

# 两个源按顺序试：Gitee 镜像快（zip 实测 18.8 MB / 8.9 秒），但可能还没上传 → 404 就跳过；
# GitHub 固定 tag 是权威来源，国内很慢（实测约 40 KB/s），只当兜底。
BINARY_URLS = (
    "https://gitee.com/ExpertKT/notice-hub-mirror/releases/download/v1.1.0-beta.18/cloudflared-windows-amd64.zip",
    "https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-windows-amd64.exe",
)
# 权威源单独留个名字：日志/探针里说「从 GitHub 拿」时用它，不指向会变的 latest。
BINARY_URL = BINARY_URLS[1]
PUBLIC_URL_PATTERN = r"https://[a-z0-9-]+\.trycloudflare\.com"

# 55 MB 走慢速源要很久（实测国内约 40 KB/s，20 分钟才 22 MB），所以总时限给足，但每一块读都卡 30 秒：
# 网络卡死时必须能自己放弃，不能把「正在下载」永远挂在那里。
DOWNLOAD_TIMEOUT_SECONDS = 900.0
READ_TIMEOUT_SECONDS = 30.0
VERSION_TIMEOUT_SECONDS = 8.0
READY_TIMEOUT_SECONDS = 90.0
STOP_TIMEOUT_SECONDS = 5.0
# 冷装实测：地址打出来之后 2–10 秒里公网一侧还连不上（HTTP 530 / 连接被关），
# 只探一次就把「开通成功」误报成「拿到了但连不上」。所以失败要重试，别急着判死。
# 判死必须按时间、不能按次数：用户 2026-11-09 实测「地址出现到公网真通」要 5–15 秒，
# 而本机 DNS 还没生效时（getaddrinfo failed）Windows 会把失败缓存 60 秒
# （trycloudflare.com 的 SOA 默认 TTL 就是 60），一次解析失败要等下一轮才有机会通。
# 所以窗口给 75 秒 = 60 秒负缓存 + 余量；6 次 × 2 秒（约 10 秒）会卡在边界上误杀。
PROBE_TIMEOUT_SECONDS = 75.0
PROBE_INTERVAL_SECONDS = 2.0
CHUNK_BYTES = 1 << 20
# 错误文案里只留最后一行 stderr 的最后 200 字：cloudflared 的日志里有连接串之类的东西，
# 整段贴给页面既没用又难看。
LINE_TAIL = 200
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_URL_RE = re.compile(PUBLIC_URL_PATTERN)


def binary_path(data_dir: Any) -> Path:
    """cloudflared 落在数据目录里（不放 Program Files，升级/卸载都不碰系统目录）。"""
    return Path(data_dir or ".") / "bin" / "cloudflared.exe"


def is_installed(data_dir: Any) -> bool:
    """本机有没有一份大小对得上的 cloudflared.exe；只查文件，不跑命令、不读哈希。

    这里刻意不算哈希：状态接口每次刷新都会问「装没装」，对 55 MB 反复做 sha256
    会把页面拖慢。哈希只在下载那一步校验（``ensure_binary``），装完之后按版本固定不替换。
    """
    path = binary_path(data_dir)
    try:
        return path.is_file() and path.stat().st_size == BINARY_BYTES
    except OSError:
        return False


def ensure_binary(data_dir: Any, *, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> Path:
    """确保本机有一份验过的 cloudflared.exe，返回它的路径。

    已存在且大小对得上就直接用（断网也能开）。否则按 ``BINARY_URLS`` 顺序试：
    流式下载 → 是 zip 就先校压缩包再只解出 ``ZIP_MEMBER`` 那一个成员 → 校验字节数
    → 校验 sha256 → 真跑一次 ``--version`` 并确认版本号 → 才 ``os.replace`` 原子改名。
    任何一步不过就删掉临时文件换下一个源；全部失败给一句能看懂的话。
    半截或冒牌文件绝不落到正式路径上，否则下次启动会「已安装但跑不起来」，
    用户只看到一个英文报错。
    """
    target = binary_path(data_dir)
    if is_installed(data_dir):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".exe.part")
    staged = target.with_suffix(".exe.staged")
    _discard(part)
    _discard(staged)
    deadline = time.monotonic() + max(1.0, float(timeout))
    failures: list[str] = []
    for url in BINARY_URLS:
        candidate = part
        try:
            saved = _download(url, part, deadline, timeout)
            if url.lower().endswith(".zip"):
                candidate = _unpack_zip(part, staged, saved)
                _discard(part)  # 18.8 MB 的压缩包已经用不上了
            size = candidate.stat().st_size
        except OSError as error:
            _discard(part)
            _discard(staged)
            failures.append(str(error))
            continue
        if size != BINARY_BYTES:
            _discard(part)
            _discard(staged)
            failures.append("文件是 %d 字节，应该是 %d 字节" % (size, BINARY_BYTES))
            continue
        digest = _sha256_file(candidate)
        if digest != BINARY_SHA256:
            _discard(part)
            _discard(staged)
            failures.append("文件校验值不对（%s…）" % digest[:12])
            continue
        try:
            completed = subprocess.run(
                [str(candidate), "--version"],
                capture_output=True,
                timeout=VERSION_TIMEOUT_SECONDS,
                check=False,
                creationflags=_CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as error:
            _discard(part)
            _discard(staged)
            failures.append("跑不起来：%s" % error)
            continue
        if completed.returncode != 0:
            _discard(part)
            _discard(staged)
            failures.append("跑不起来（退出码 %s）" % completed.returncode)
            continue
        version_text = _console_text(completed.stdout) + " " + _console_text(completed.stderr)
        if BINARY_VERSION not in version_text:
            _discard(part)
            _discard(staged)
            failures.append("版本不是 %s" % BINARY_VERSION)
            continue
        os.replace(candidate, target)
        return target
    _discard(part)
    _discard(staged)
    raise OSError(
        "通道程序下载失败（已试 %d 个源）：%s。检查网络后点「重试」。"
        % (len(BINARY_URLS), "；".join(failures) or "没有可用的下载源")
    )


def _unpack_zip(archive: Path, target: Path, saved: int) -> Path:
    """校验 Gitee 的 zip 包，只把 ``ZIP_MEMBER`` 那一个成员解到 ``target``。

    成员名必须精确相等：按 basename 模糊匹配会让包里任意子目录下的同名文件过关，
    而写其它路径又等于让镜像决定往用户机器上落什么。
    """
    if saved != ZIP_BYTES:
        raise OSError("压缩包是 %d 字节，应该是 %d 字节" % (saved, ZIP_BYTES))
    digest = _sha256_file(archive)
    if digest != ZIP_SHA256:
        raise OSError("压缩包校验值不对（%s…）" % digest[:12])
    try:
        bundle = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as error:
        raise OSError("压缩包读不出来：%s" % error) from error
    with bundle:
        if ZIP_MEMBER not in bundle.namelist():
            raise OSError("压缩包里没有 %s" % ZIP_MEMBER)
        try:
            with bundle.open(ZIP_MEMBER) as source, target.open("wb") as sink:
                while True:
                    chunk = source.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    sink.write(chunk)
        except OSError as error:
            raise OSError("解压失败：%s" % error) from error
    return target


def parse_public_url(text: str) -> str:
    """从 cloudflared 的 stderr 里取第一条 trycloudflare 地址；没有返回空串。

    只认实时打出来的地址：它还会打印 ``https://developers.cloudflare.com/...``
    之类的文档链接，那些不是通道地址，不能让页面照着显示。
    """
    match = _URL_RE.search(str(text or ""))
    return match.group(0) if match else ""


class QuickTunnel:
    """一次 Cloudflare 快速通道；页面轮询 ``snapshot()`` 看进度。

    state: idle / downloading / starting / ready / error
    """

    def __init__(
        self,
        port: int,
        data_dir: Any,
        *,
        probe: Callable[[str], tuple[bool, str]] | None = None,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
        probe_timeout: float = PROBE_TIMEOUT_SECONDS,
    ) -> None:
        self.port = int(port)
        self._data_dir = data_dir
        self._probe = probe or tunnel.probe_public
        self._ready_timeout = float(ready_timeout)
        self._stop_timeout = float(stop_timeout)
        self._probe_interval = max(0.0, float(probe_interval))
        self._probe_timeout = max(0.0, float(probe_timeout))
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._stop_event = threading.Event()
        self._snapshot: dict[str, Any] = {"state": "idle", "message": "", "url": ""}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            payload = dict(self._snapshot)
        payload["installed"] = is_installed(self._data_dir)
        return payload

    def start(self) -> dict[str, Any]:
        """开始（或继续）开通；正在跑或已开通时原样返回当前状态，连点两下也只起一个进程。"""
        with self._lock:
            state = str(self._snapshot.get("state") or "idle")
            if state in {"downloading", "starting"}:
                return self._snapshot_locked()
            if state == "ready" and self._snapshot.get("url"):
                return self._snapshot_locked()
            self._stop_event.clear()
            self._snapshot = {
                "state": "downloading",
                "message": "正在下载通道程序（约 19 MB，国内一般半分钟内）…",
                "url": "",
            }
            self._thread = threading.Thread(target=self.run, name="cloudflare-quick-tunnel", daemon=True)
            self._thread.start()
            return self._snapshot_locked()

    def stop(self) -> dict[str, Any]:
        """关掉通道：先 terminate，5 秒不退再 kill；状态回 idle，地址清空。"""
        self._stop_event.set()
        self._terminate()
        with self._lock:
            self._process = None
            self._snapshot = {"state": "idle", "message": "", "url": ""}
        return self.snapshot()

    # ---- 后台流程 ----

    def run(self) -> None:
        """同步跑完整个流程；start() 在后台线程里调它，测试可直接调。"""
        try:
            self._run()
        except Exception as error:  # noqa: BLE001 - 后台线程要把任何异常变成页面能看懂的提示
            LOGGER.warning("Cloudflare 快速通道启动失败", exc_info=True)
            self._fail("Cloudflare 快速通道启动失败：%s。可以点「重试」。" % error)

    def _run(self) -> None:
        try:
            executable = ensure_binary(self._data_dir)
        except OSError as error:
            self._fail(str(error))
            return
        if self._stop_event.is_set():
            return
        self._set(state="starting", message="正在启动 Cloudflare 快速通道，并确认公网能打开…", url="")
        command = [
            str(executable),
            "tunnel",
            "--url",
            "http://127.0.0.1:%d" % self.port,
            "--no-autoupdate",
            # 固定走 TCP。实测（2026-11-09，用户这台机器 + cloudflared 2026.10.7 + 8766）：
            # 默认让 cloudflared 自己挑，它 precheck 报 healthy 却选 QUIC，可这台机器的
            # UDP/QUIC 到 Cloudflare 边缘不通——地址打得出来，但 `Registered tunnel connection`
            # 一次都没有，只反复 `datagram manager error: timeout` / `Register tunnel error`，
            # 约 40 秒后自己退出，公网侧一直 530。加 `--protocol http2` 后注册成功、
            # 零条 timeout，地址出现约 19 秒公网就是 HTTP 200。
            # 不做「先 QUIC 失败再回退」的两条路：固定 http2 一条路就够，少一个状态机。
            "--protocol",
            "http2",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=_CREATE_NO_WINDOW,
            )
        except OSError as error:
            # 文件已经在（大小对得上）却启动失败：多半是损坏或被安全软件拦了。
            # 留着它，用户下次点开通还是同一份坏文件、永不重下，只能自己去 data\bin 手动删。
            _discard(Path(executable))
            self._fail("启动 cloudflared 失败：%s。请确认程序完整后点「重试」。" % error)
            return
        with self._lock:
            self._process = process
        inbox: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(
            target=_read_lines,
            args=(process.stderr, inbox),
            name="cloudflare-stderr",
            daemon=True,
        )
        reader.start()

        url, last = self._wait_for_url(process, inbox)
        if not url:
            return  # _wait_for_url 已经把原因写进状态了
        outcome, detail = self._probe_with_retries(url, process, last)
        if self._stop_event.is_set():
            return
        if outcome == "exited":
            return  # _probe_with_retries 已经把「进程退了」写进状态；进程都没了，不用再 terminate
        if outcome != "ready":
            message = "公网地址拿到了，但连不上：%s。可以点「重试」。" % detail
            if _dns_failure(detail):
                message += "这台电脑暂时解析不到这个公网地址；需要固定地址可改用 Tailscale 通道。"
            self._fail(message)
            self._terminate()
            return
        self._set(state="ready", message="公网入口已开通，手机用移动网络也能连。", url=url)
        self._watch(process, inbox, last)

    def _wait_for_url(self, process: subprocess.Popen[bytes], inbox: "queue.Queue[str | None]") -> tuple[str, str]:
        """等 cloudflared 把公网地址打出来；超时/退出都写成中文原因并返回空地址。"""
        deadline = time.monotonic() + self._ready_timeout
        last = ""
        while True:
            if self._stop_event.is_set():
                return "", last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._fail(self._timeout_message(last))
                self._terminate()
                return "", last
            line = _next_line(inbox, min(remaining, 0.5))
            if line is None:
                self._fail(self._exited_message(last))
                return "", last
            if line:
                last = line
                found = parse_public_url(line)
                if found:
                    return found, last
            elif process.poll() is not None:
                self._fail(self._exited_message(last))
                return "", last

    def _probe_with_retries(
        self,
        url: str,
        process: subprocess.Popen[bytes],
        last: str,
    ) -> tuple[str, str]:
        """从公网反复探 ``/api/ping``，直到时限为止；返回 ``(outcome, detail)``。

        outcome：``ready`` 通了 / ``failed`` 没通 / ``exited`` cloudflared 在这期间退了。

        实测（2026-11-07 冷装）：cloudflared 打完地址到公网真能连上之间有 2–10 秒空窗，
        这期间访问是 HTTP 530 或连接被直接关掉。用户 2026-11-09 又踩到一次：地址出来了，
        但本机还没解析到公网域名（getaddrinfo failed），而 Windows 会把这次失败缓存 60 秒
        （trycloudflare.com 的 SOA 默认 TTL）。所以判死按总时限（``PROBE_TIMEOUT_SECONDS``），
        不按次数：6 次 × 2 秒约 10 秒，正好卡在「有时能过」的边界上。

        每轮探测前先看进程：cloudflared 已经退了就别再空等剩下的几十秒。
        等待用 ``stop_event.wait()``：用户点「关闭公网」要能立刻打断，不许多等一轮。
        """
        started = time.monotonic()
        deadline = started + self._probe_timeout
        detail = ""
        while True:
            if self._stop_event.is_set():
                return "failed", detail
            if process.poll() is not None and not self._stop_event.is_set():
                self._fail(self._exited_after_url_message(url, last))
                return "exited", detail
            self._set(
                state="starting",
                message="公网地址已拿到：%s。正在等公网生效（已等 %d 秒，最长 %g 秒）…"
                % (url, int(time.monotonic() - started), self._probe_timeout),
            )
            ok, detail = self._probe(url)
            if ok:
                return "ready", detail
            if self._stop_event.is_set():
                return "failed", detail
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "failed", detail
            if self._stop_event.wait(min(self._probe_interval, remaining)):
                return "failed", detail

    def _watch(self, process: subprocess.Popen[bytes], inbox: "queue.Queue[str | None]", last: str) -> None:
        """就绪之后继续盯着：进程一退或日志断流，就把状态改成「断了」并清掉地址。"""
        while not self._stop_event.is_set():
            line = _next_line(inbox, 0.5)
            if line is None:
                break
            if line:
                last = line
                continue
            if process.poll() is not None:
                break
        if self._stop_event.is_set():
            return
        self._fail(self._disconnected_message(last))
        with self._lock:
            self._process = None

    def _timeout_message(self, last: str) -> str:
        message = "等了 %g 秒没拿到公网地址。" % self._ready_timeout
        if last:
            message += "cloudflared 最后一行：%s" % _tail(last)
        else:
            message += "cloudflared 一行日志都没输出，可能是它被系统拦住了。"
        return message + "可以点「重试」。"

    def _exited_message(self, last: str) -> str:
        message = "Cloudflared 刚启动就退出了，没拿到公网地址。"
        if last:
            message += "最后一行：%s。" % _tail(last)
        return message + "可以点「重试」。"

    def _exited_after_url_message(self, url: str, last: str) -> str:
        """地址已经打出来了、进程才中途退出：不许再说「没拿到公网地址」。"""
        message = "公网地址已拿到：%s，但通道程序中途自己退出了" % url
        if last:
            message += "；最后一行：%s" % _tail(last)
        return message + "。点「重试」会重新开一个地址。"
    def _disconnected_message(self, last: str) -> str:
        message = "公网通道断了"
        if last:
            message += "：%s" % _tail(last)
        return message + "。点「重试」会重新开一个地址。"

    def _set(self, **fields: Any) -> None:
        """关掉之后不再接受任何写入：``stop()`` 写回的 idle 就是最后一句话。

        用户点「关闭公网」可能正好落在后台线程「检查 stop」与「写 starting」之间：
        不加这道闸，线程随后会把 starting 盖回去，页面永远停在「正在等公网生效」，
        而 ``start()`` 见到 starting 又直接返回旧快照——连「重试」都点不动。
        同一道闸也挡住「probe 恰好成功」时把 ready 写到 idle 之上的幽灵「已开通」。
        ``stop()`` 在锁内直接写快照、``start()`` 是先 clear 再直接写 downloading，
        所以合法的写入都不会发生在 stop 置位之后。
        """
        if self._stop_event.is_set():
            return
        with self._lock:
            self._snapshot.update(fields)

    def _fail(self, message: str) -> None:
        # 真错因常常只在 cloudflared 的 stderr 里（2026-11-09 那次 QUIC 连不上就是这样，
        # noticehub.log 一条都没有，只能手工跑 cloudflared 才查得到）：失败必须留痕。
        LOGGER.warning("Cloudflare 快速通道失败：%s", message)
        self._set(state="error", message=str(message), url="")

    def _snapshot_locked(self) -> dict[str, Any]:
        payload = dict(self._snapshot)
        payload["installed"] = is_installed(self._data_dir)
        return payload

    def _terminate(self) -> None:
        with self._lock:
            process = self._process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self._stop_timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
        except OSError:
            pass


def _read_lines(stream: Any, inbox: "queue.Queue[str | None]") -> None:
    """把 stderr 逐行塞进队列；结束时塞一个 None 当哨兵，读的那头就知道「日志断了」。"""
    try:
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = decode_console_output(raw) if isinstance(raw, bytes) else str(raw)
            line = (line or "").strip()
            if line:
                inbox.put(line)
    except (OSError, ValueError):
        pass
    finally:
        inbox.put(None)


def _next_line(inbox: "queue.Queue[str | None]", seconds: float) -> str | None:
    """取一行；超时返回空串（继续等），读到哨兵返回 None（日志断了）。"""
    try:
        return inbox.get(timeout=max(0.01, float(seconds)))
    except queue.Empty:
        return ""


def _tail(text: str) -> str:
    value = str(text or "").strip()
    return value[-LINE_TAIL:] if len(value) > LINE_TAIL else value


def _dns_failure(detail: str) -> bool:
    """这次失败是不是「本机解析不到这个公网地址」（``socket.gaierror`` / getaddrinfo）。

    这种失败不是 Cloudflare 那边还没准备好，而是本机 DNS 还没生效：
    用户重试也未必立刻好，得给一句「怎么办」（换固定地址的 Tailscale 通道）。
    """
    text = str(detail or "").lower()
    return "getaddrinfo" in text or "gaierror" in text


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except (OSError, ValueError):
        pass


def _sha256_file(path: Path) -> str:
    """分块算 sha256：55 MB 一次读进内存没必要，也容易把低内存机器拖死。"""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _console_text(raw: Any) -> str:
    """子进程的输出可能是 bytes、str 或 None；统一成能搜字符串的文本。"""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return decode_console_output(raw)
    return str(raw)


def _download(url: str, part: Path, deadline: float, total_timeout: float) -> int:
    """把安装包流式写到 ``part``，返回写进去的字节数；失败抛中文 OSError。

    ``deadline`` 是绝对时刻（``time.monotonic()``），多个源共用同一份总预算，
    免得第一个源卡死之后第二个源又从头等一遍 15 分钟。
    """
    request = urllib.request.Request(url, headers={"User-Agent": "notice-hub"})
    saved = 0
    expired = False
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT_SECONDS) as response, part.open("wb") as handle:
            while True:
                if time.monotonic() > deadline:
                    expired = True
                    break
                chunk = response.read(CHUNK_BYTES)
                if not chunk:
                    break
                saved += len(chunk)
                handle.write(chunk)
    except urllib.error.HTTPError as error:
        raise OSError("镜像返回 HTTP %s" % error.code) from error
    except urllib.error.URLError as error:
        reason = error.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise OSError("下载超时：%g 秒没收到新数据" % READ_TIMEOUT_SECONDS) from error
        raise OSError("连不上下载源（%s）" % reason) from error
    except (socket.timeout, TimeoutError) as error:
        raise OSError("下载超时：%g 秒没收到新数据" % READ_TIMEOUT_SECONDS) from error
    except OSError as error:
        raise OSError("下载出错：%s" % error) from error
    if expired:
        raise OSError("下载超过 %g 秒还没下完" % max(1.0, float(total_timeout)))
    return saved


__all__ = [
    "BINARY_URL",
    "PUBLIC_URL_PATTERN",
    "QuickTunnel",
    "binary_path",
    "ensure_binary",
    "is_installed",
    "parse_public_url",
]
