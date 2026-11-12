"""Cloudflare 快速通道的用例：地址解析、下载校验、状态机。

红线：不连网、不起真的 cloudflared、不对本机 8766 开公网。
``urllib`` 和 ``Popen`` 全部打桩，探活用假函数，所以跑这些用例不会碰到任何真实网络。
"""

from __future__ import annotations

import hashlib
import io
import threading
import time
import unittest
import urllib.error
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from noticehub import cloudflare

URL = "https://neutral-colleges-photographic-softball.trycloudflare.com"

# cloudflared 的真实输出：先一大段条款 banner，地址夹在中间，后面还有 precheck 表。
NOISY_STDERR = [
    "Thank you for trying Cloudflare Tunnel. Doing so means that you are choosing",
    "not to use a cloud account, which is great!",
    "INF Thank you for trying Cloudflare Tunnel.",
    "2026-11-07T19:12:33Z INF +--------------------------------------------------------------------------------------------+",
    "2026-11-07T19:12:38Z INF |  Your quick Tunnel has been created! Visit it at:                                            |",
    "2026-11-07T19:12:38Z INF |  " + URL + "  |",
    "2026-11-07T19:12:38Z INF +--------------------------------------------------------------------------------------------+",
]

LONG_LINE = "ERR " + "x" * 500


class _FakeResponse:
    """假的 urlopen 响应：按块吐出预设的字节。"""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, size: int = -1) -> bytes:  # noqa: ARG002 - 与 urlopen 的签名对齐
        if not self._chunks:
            return b""
        return self._chunks.pop(0)

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _LiveStream:
    """按行吐完就卡住，模拟「进程还活着、但没有新日志」。

    测试收尾时 ``release()``，读线程才会读到 EOF 退出。
    """

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)
        self._closed = threading.Event()

    def readline(self) -> bytes:
        if self._lines:
            return (self._lines.pop(0) + "\n").encode("utf-8")
        self._closed.wait(10.0)
        return b""

    def release(self) -> None:
        self._closed.set()


class _DeadStream:
    """立刻 EOF：模拟 cloudflared 起来就退出 / 日志断流。"""

    def readline(self) -> bytes:
        return b""


class _EofAfterLines:
    """先吐完给定行，再 EOF：模拟「已经打出地址、随后进程就没了」。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> bytes:
        if not self._lines:
            return b""
        return (self._lines.pop(0) + "\n").encode("utf-8")


class _FakeProcess:
    def __init__(self, stream: object, *, returncode: int | None = None) -> None:
        self.stderr = stream
        self._returncode = returncode
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self._returncode

    def terminate(self) -> None:
        self.terminated += 1
        self._returncode = 0

    def kill(self) -> None:
        self.killed += 1
        self._returncode = -9

    def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002 - 与 Popen 对齐
        return 0 if self._returncode is None else self._returncode


class _BlockOnFirstPoll(_FakeProcess):
    """第一次 ``poll()`` 时停住：把「检查 stop」到「写 starting」之间那段微秒级窗口放大成可复现的时序。

    真实故障就是用户正好在这个窗口里点「关闭公网」：``stop()`` 已经把 idle 写回去，
    后台线程随后又把 starting 写上去，页面永远停在「正在等公网生效」。
    """

    def __init__(self, stream: object, entered: threading.Event, gate: threading.Event) -> None:
        super().__init__(stream)
        self._entered = entered
        self._gate = gate
        self._blocked = False

    def poll(self) -> int | None:
        if not self._blocked:
            self._blocked = True
            self._entered.set()
            self._gate.wait(5.0)
        return super().poll()


def _wait_state(quick: cloudflare.QuickTunnel, state: str, timeout: float = 5.0) -> dict:
    """等快照走到某个状态；等不到就把当前快照带出来让断言报清楚。"""
    deadline = time.monotonic() + timeout
    snapshot = quick.snapshot()
    while time.monotonic() < deadline:
        snapshot = quick.snapshot()
        if snapshot.get("state") == state:
            return snapshot
        time.sleep(0.01)
    return snapshot


class ParsePublicUrlTests(unittest.TestCase):
    def test_reads_the_url_out_of_noisy_stderr(self) -> None:
        self.assertEqual(cloudflare.parse_public_url("\n".join(NOISY_STDERR)), URL)

    def test_ignores_documentation_links(self) -> None:
        # cloudflared 会顺手打官方文档链接，那不是通道地址，页面照抄过去就是错的。
        text = "See https://developers.cloudflare.com/cloudflare-one/ for details"
        self.assertEqual(cloudflare.parse_public_url(text), "")

    def test_blank_text_has_no_url(self) -> None:
        self.assertEqual(cloudflare.parse_public_url(""), "")
        self.assertEqual(cloudflare.parse_public_url("INF no address here"), "")


class EnsureBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 真包 55 MB，用例里没必要真写这么大：把「应有大小/哈希/版本」压成 32 字节的小样本，
        # 校验逻辑一模一样——大小、sha256、版本串三条都得过。
        self.body = b"x" * 32
        self.addCleanup(mock.patch.object(cloudflare, "BINARY_BYTES", len(self.body)).stop)
        mock.patch.object(cloudflare, "BINARY_BYTES", len(self.body)).start()
        self.addCleanup(mock.patch.object(cloudflare, "BINARY_SHA256", hashlib.sha256(self.body).hexdigest()).stop)
        mock.patch.object(cloudflare, "BINARY_SHA256", hashlib.sha256(self.body).hexdigest()).start()
        self.data_dir = Path(self.tmp.name) / "data"
        self.version_ok = SimpleNamespace(
            returncode=0,
            stdout=b"cloudflared version 2026.10.7 (built 2026-11-01)",
            stderr=b"",
        )

    def _files(self) -> list[str]:
        folder = cloudflare.binary_path(self.data_dir).parent
        if not folder.is_dir():
            return []
        return sorted(item.name for item in folder.iterdir())

    def _not_found(self) -> urllib.error.HTTPError:
        return urllib.error.HTTPError("https://example.invalid/x.exe", 404, "Not Found", None, None)

    def _zip_bytes(self, payload: bytes, member: str | None = None) -> bytes:
        """在内存里造一份真 zip，形状和 Gitee 上那份镜像一致。"""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
            bundle.writestr(member or cloudflare.ZIP_MEMBER, payload)
        return buffer.getvalue()

    def _accept_zip(self, archive: bytes) -> None:
        """把「压缩包应有的大小/哈希」也压成这份样本，校验逻辑照跑。"""
        for name, value in (
            ("ZIP_BYTES", len(archive)),
            ("ZIP_SHA256", hashlib.sha256(archive).hexdigest()),
        ):
            patcher = mock.patch.object(cloudflare, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _serves(payload: bytes):
        """每个源都回同一份内容：同一个响应对象读第二次只会读到空。"""
        return lambda *args, **kwargs: _FakeResponse([payload])

    def test_existing_binary_is_reused_without_downloading(self) -> None:
        target = cloudflare.binary_path(self.data_dir)
        target.parent.mkdir(parents=True)
        target.write_bytes(self.body)
        with mock.patch.object(cloudflare.urllib.request, "urlopen", side_effect=AssertionError("不该下载")):
            self.assertEqual(cloudflare.ensure_binary(self.data_dir), target)

    def test_existing_binary_with_the_wrong_size_is_not_trusted(self) -> None:
        # 大小必须精确对上：一份 30 MB 的截断包曾被当成「已安装」，之后每次点开通都失败且永不重下。
        target = cloudflare.binary_path(self.data_dir)
        target.parent.mkdir(parents=True)
        target.write_bytes(b"x" * 4)
        self.assertFalse(cloudflare.is_installed(self.data_dir))

    def test_falls_back_to_the_next_source_when_the_first_is_missing(self) -> None:
        # 源顺序：Gitee 先试（快），404 就换 GitHub（权威但慢）。
        responses = [self._not_found(), _FakeResponse([self.body])]
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=responses
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            target = cloudflare.ensure_binary(self.data_dir)
        self.assertTrue(target.is_file())
        self.assertEqual(urlopen.call_count, 2)
        first = urlopen.call_args_list[0].args[0]
        second = urlopen.call_args_list[1].args[0]
        self.assertIn("gitee.com", first.full_url)
        self.assertIn("github.com", second.full_url)

    def test_short_download_moves_on_to_the_next_source(self) -> None:
        responses = [_FakeResponse([b"x" * 4]), _FakeResponse([self.body])]
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=responses
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            target = cloudflare.ensure_binary(self.data_dir)
        self.assertTrue(target.is_file())
        self.assertEqual(urlopen.call_count, 2, "第一个源给了半截文件，必须换下一个源")

    def test_all_sources_failing_leaves_no_binary_and_says_how_many_were_tried(self) -> None:
        with mock.patch.object(cloudflare.urllib.request, "urlopen", side_effect=self._not_found()):
            with self.assertRaises(OSError) as caught:
                cloudflare.ensure_binary(self.data_dir)
        message = str(caught.exception)
        self.assertIn("通道程序下载失败", message)
        self.assertIn("已试 2 个源", message)
        self.assertIn("重试", message)
        self.assertEqual(self._files(), [])

    def test_wrong_hash_is_rejected_even_when_the_size_matches(self) -> None:
        with mock.patch.object(cloudflare, "BINARY_SHA256", "0" * 64), mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=self._serves(self.body)
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            with self.assertRaises(OSError) as caught:
                cloudflare.ensure_binary(self.data_dir)
        self.assertIn("校验值不对", str(caught.exception))
        self.assertEqual(urlopen.call_count, 2, "哈希不符的源要丢掉，接着试下一个源")
        self.assertEqual(self._files(), [], "哈希不符的包绝不能落到正式路径上")

    def test_wrong_version_is_rejected(self) -> None:
        old = SimpleNamespace(returncode=0, stdout=b"cloudflared version 2026.4.0 (built 2026-04-01)", stderr=b"")
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=self._serves(self.body)
        ), mock.patch.object(cloudflare.subprocess, "run", return_value=old):
            with self.assertRaises(OSError) as caught:
                cloudflare.ensure_binary(self.data_dir)
        self.assertIn("版本不是 2026.10.7", str(caught.exception))
        self.assertEqual(self._files(), [])

    def test_binary_that_cannot_run_is_rejected(self) -> None:
        completed = SimpleNamespace(returncode=3, stdout=b"", stderr=b"")
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=self._serves(self.body)
        ), mock.patch.object(cloudflare.subprocess, "run", return_value=completed):
            with self.assertRaises(OSError) as caught:
                cloudflare.ensure_binary(self.data_dir)
        self.assertIn("跑不起来（退出码 3）", str(caught.exception))
        self.assertEqual(self._files(), [])

    def test_good_download_lands_on_the_final_path(self) -> None:
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", side_effect=self._serves(self.body)
        ), mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            target = cloudflare.ensure_binary(self.data_dir)
        self.assertTrue(target.is_file())
        self.assertEqual(target.stat().st_size, len(self.body))
        self.assertEqual(self._files(), ["cloudflared.exe"])

    def test_zip_source_is_verified_then_unpacked_to_the_final_path(self) -> None:
        archive = self._zip_bytes(self.body)
        self._accept_zip(archive)
        with mock.patch.object(
            cloudflare.urllib.request, "urlopen", return_value=_FakeResponse([archive])
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok) as run:
            target = cloudflare.ensure_binary(self.data_dir)
        self.assertEqual(urlopen.call_count, 1)
        self.assertTrue(urlopen.call_args.args[0].full_url.endswith(".zip"))
        self.assertEqual(target.read_bytes(), self.body, "落盘的必须是解出来的 exe，不是压缩包")
        self.assertIn(
            "cloudflared.exe",
            str(run.call_args.args[0][0]),
            "--version 跑的是解出来的 exe，不是压缩包",
        )
        self.assertNotIn(".zip", str(run.call_args.args[0][0]))
        self.assertEqual(self._files(), ["cloudflared.exe"])

    def test_zip_with_the_wrong_hash_falls_back_to_the_official_exe(self) -> None:
        archive = self._zip_bytes(self.body)
        with mock.patch.object(cloudflare, "ZIP_BYTES", len(archive)), mock.patch.object(
            cloudflare, "ZIP_SHA256", "0" * 64
        ), mock.patch.object(
            cloudflare.urllib.request,
            "urlopen",
            side_effect=[_FakeResponse([archive]), _FakeResponse([self.body])],
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            target = cloudflare.ensure_binary(self.data_dir)
        self.assertTrue(target.is_file())
        self.assertEqual(urlopen.call_count, 2, "压缩包哈希不对要丢掉，换下一个源")
        self.assertIn("github.com", urlopen.call_args.args[0].full_url)

    def test_zip_without_the_expected_member_is_rejected(self) -> None:
        archive = self._zip_bytes(self.body, member="cloudflared.exe")
        self._accept_zip(archive)
        with mock.patch.object(
            cloudflare.urllib.request,
            "urlopen",
            side_effect=[_FakeResponse([archive]), _FakeResponse([b"junk"])],
        ) as urlopen, mock.patch.object(cloudflare.subprocess, "run", return_value=self.version_ok):
            with self.assertRaises(OSError) as caught:
                cloudflare.ensure_binary(self.data_dir)
        self.assertEqual(urlopen.call_count, 2, "成员名不对要换源，不能按 basename 认")
        self.assertIn("压缩包里没有 %s" % cloudflare.ZIP_MEMBER, str(caught.exception))
        self.assertIn("已试 2 个源", str(caught.exception))
        self.assertEqual(self._files(), [])


class QuickTunnelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name) / "data"
        self.streams: list[_LiveStream] = []
        # download 不该在这里发生：ensure_binary 直接给个假路径。
        self.binary = Path(self.tmp.name) / "cloudflared.exe"
        self.binary.write_bytes(b"x")
        patcher = mock.patch.object(cloudflare, "ensure_binary", return_value=self.binary)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        for stream in self.streams:
            stream.release()

    def _stream(self, lines: list[str]) -> _LiveStream:
        stream = _LiveStream(lines)
        self.streams.append(stream)
        return stream

    def _quick(
        self,
        stream: object,
        *,
        probe=lambda url: (True, ""),
        ready_timeout: float = 5.0,
        probe_interval: float = 0.0,
        probe_timeout: float = 5.0,
    ):
        process = _FakeProcess(stream)
        patcher = mock.patch.object(cloudflare.subprocess, "Popen", return_value=process)
        patcher.start()
        self.addCleanup(patcher.stop)
        quick = cloudflare.QuickTunnel(
            self.port,
            self.data_dir,
            probe=probe,
            ready_timeout=ready_timeout,
            stop_timeout=0.2,
            probe_interval=probe_interval,
            probe_timeout=probe_timeout,
        )
        quick.start()
        self.addCleanup(quick.stop)
        return quick, process

    @property
    def port(self) -> int:
        # 固定一个没人在用的高位端口，绝不碰用户的 8766。
        return 59123

    def test_ready_when_the_url_shows_up_and_probe_passes(self) -> None:
        quick, process = self._quick(self._stream(NOISY_STDERR))
        snapshot = _wait_state(quick, "ready")
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["url"], URL)
        self.assertIn("已开通", snapshot["message"])
        self.assertEqual(process.terminated, 0)

    def test_timeout_reports_the_last_stderr_line(self) -> None:
        quick, process = self._quick(self._stream([LONG_LINE]), ready_timeout=0.4)
        snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        message = snapshot["message"]
        self.assertIn("没拿到公网地址", message)
        self.assertIn("最后一行", message)
        # 只留尾部 200 字，别把整段日志糊到页面上。
        self.assertIn("x" * 200, message)
        self.assertNotIn(LONG_LINE, message)
        self.assertIn("重试", message)
        self.assertEqual(process.terminated, 1)

    def test_process_exiting_right_away_is_reported(self) -> None:
        quick, _process = self._quick(_DeadStream())
        snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("刚启动就退出", snapshot["message"])

    def test_disconnect_after_ready_clears_the_url(self) -> None:
        # 地址已经打出来、probe 也通过，随后进程没了：必须变成「断了」并把地址清掉，
        # 不能让页面继续显示一个已经不通的公网地址。
        process = _FakeProcess(_EofAfterLines(NOISY_STDERR))
        with mock.patch.object(cloudflare.subprocess, "Popen", return_value=process):
            quick = cloudflare.QuickTunnel(self.port, self.data_dir, probe=lambda url: (True, ""))
            self.addCleanup(quick.stop)
            quick.start()
            snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("公网通道断了", snapshot["message"])

    def test_probe_is_retried_until_the_public_url_really_answers(self) -> None:
        # 冷装实测：地址打出来之后公网还要 2–10 秒才通，这期间是 530 / 连接被关。
        # 只探一次会把开通成功误报成「拿到了但连不上」，所以前几次失败必须重试。
        calls: list[str] = []

        def probe(url: str) -> tuple[bool, str]:
            calls.append(url)
            if len(calls) < 3:
                return False, "连不上 %s：Remote end closed connection without response" % url
            return True, "公网访问正常"

        quick, process = self._quick(self._stream(NOISY_STDERR), probe=probe)
        snapshot = _wait_state(quick, "ready")
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["url"], URL)
        self.assertEqual(len(calls), 3, "前两次探测失败后要再探，通了才算开通")
        self.assertEqual(set(calls), {URL})
        self.assertEqual(process.terminated, 0)

    def test_probe_gives_up_only_when_the_time_limit_runs_out(self) -> None:
        # 旧实现按次数判死（6 次 × 2 秒约 10 秒），真机「地址出现到公网真通」要 5–15 秒，
        # 经常在边界上被误杀。现在按总时限判死：到点还没通才报错。
        attempts: list[str] = []

        def probe(url: str) -> tuple[bool, str]:
            attempts.append(url)
            return False, "等了 15 秒没有回应"

        quick, process = self._quick(
            self._stream(NOISY_STDERR), probe=probe, probe_interval=0.05, probe_timeout=0.4
        )
        snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("连不上", snapshot["message"])
        # 0.4 秒、每 0.05 秒一轮 → 至少 7 次；关键是不能再在第 6 次就判死。
        self.assertGreater(len(attempts), 6, "到时限才判死，不能探 6 次就放弃")
        self.assertEqual(process.terminated, 1)

    def test_probe_keeps_retrying_past_the_old_six_attempt_limit(self) -> None:
        # 锁住用户 2026-11-09 踩到的那个回归：前 8 次都连不上、第 9 次才通，也必须判 ready。
        calls: list[str] = []

        def probe(url: str) -> tuple[bool, str]:
            calls.append(url)
            if len(calls) < 9:
                return False, "连不上 %s：Remote end closed connection without response" % url
            return True, "公网访问正常"

        quick, process = self._quick(
            self._stream(NOISY_STDERR), probe=probe, probe_interval=0.02, probe_timeout=5.0
        )
        snapshot = _wait_state(quick, "ready")
        self.assertEqual(snapshot["state"], "ready")
        self.assertEqual(snapshot["url"], URL)
        self.assertEqual(len(calls), 9, "旧实现第 6 次就死了，现在要一直探到公网真能连上")
        self.assertEqual(process.terminated, 0)

    def test_probe_stops_as_soon_as_cloudflared_exits(self) -> None:
        # 进程都退了就别把剩下的几十秒等满：立刻按「刚启动就退出」报错，别让用户干等。
        hit = threading.Event()

        def probe(url: str) -> tuple[bool, str]:
            hit.set()
            return False, "连不上"

        quick, process = self._quick(
            self._stream(NOISY_STDERR), probe=probe, probe_interval=0.2, probe_timeout=10.0
        )
        self.assertTrue(hit.wait(3.0), "第一次探测应该已经发生")
        process._returncode = 1
        started = time.monotonic()
        snapshot = _wait_state(quick, "error")
        elapsed = time.monotonic() - started
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("中途", snapshot["message"])
        self.assertLess(elapsed, 3.0, "进程退出了必须立刻报错，不能把 10 秒时限等满")

    def test_snapshot_shows_progress_while_waiting_for_public_access(self) -> None:
        # 页面每 2 秒轮询 snapshot()：等公网生效期间必须能显示「已等几秒」，
        # 但地址不能提前填进 url（还没验证通的地址不能让用户照着用）。
        hit = threading.Event()

        def probe(url: str) -> tuple[bool, str]:
            hit.set()
            return False, "Remote end closed connection without response"

        quick, _process = self._quick(
            self._stream(NOISY_STDERR), probe=probe, probe_interval=5.0, probe_timeout=75.0
        )
        self.assertTrue(hit.wait(3.0), "第一次探测应该已经发生")
        snapshot = quick.snapshot()
        self.assertEqual(snapshot["state"], "starting")
        self.assertEqual(snapshot["url"], "")
        self.assertIn(URL, snapshot["message"])
        self.assertIn("已等", snapshot["message"])
        self.assertIn("最长", snapshot["message"])

    def test_dns_failure_tells_the_user_how_to_get_a_stable_address(self) -> None:
        # getaddrinfo 失败是「本机解析不到」，重试未必立刻好：文案要给出下一步（换 Tailscale）。
        quick, _process = self._quick(
            self._stream(NOISY_STDERR),
            probe=lambda url: (False, "连不上 %s：[Errno 11001] getaddrinfo failed" % url),
            probe_interval=0.05,
            probe_timeout=0.3,
        )
        snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("连不上", snapshot["message"])
        self.assertIn("Tailscale", snapshot["message"])

    def test_late_probe_result_cannot_overwrite_idle_after_stop(self) -> None:
        # stop() 把 idle 写回之后，后台线程不许再改状态：否则页面永远停在「正在等公网生效」，
        # 而 start() 见到 starting/downloading 直接返回旧快照——用户连「重试」都点不动，是死路。
        entered = threading.Event()
        gate = threading.Event()
        process = _BlockOnFirstPoll(self._stream(NOISY_STDERR), entered, gate)
        with mock.patch.object(cloudflare.subprocess, "Popen", return_value=process):
            quick = cloudflare.QuickTunnel(
                self.port,
                self.data_dir,
                probe=lambda url: (True, "公网访问正常"),
                ready_timeout=5.0,
                stop_timeout=0.2,
                probe_interval=0.0,
                probe_timeout=30.0,
            )
            self.addCleanup(quick.stop)
            quick.start()
            self.assertTrue(entered.wait(5.0), "探测循环应该已经走到「检查进程」这一步")
            thread = quick._thread
            quick.stop()
            gate.set()
            thread.join(5.0)
        self.assertFalse(thread.is_alive(), "关闭公网之后后台线程不许再挂着")
        self.assertEqual(quick.snapshot()["state"], "idle")
        self.assertEqual(quick.snapshot()["url"], "")

    def test_stop_interrupts_the_probe_wait_instead_of_hanging(self) -> None:
        # 「关闭公网」不能卡在探测的等待里：必须立刻打断，状态回 idle，也不许再跳成 error。
        seen = threading.Event()

        def probe(url: str) -> tuple[bool, str]:
            seen.set()
            return False, "连不上"

        quick, process = self._quick(self._stream(NOISY_STDERR), probe=probe, probe_interval=5.0)
        self.assertTrue(seen.wait(3.0), "第一次探测应该已经发生")
        started = time.monotonic()
        snapshot = quick.stop()
        elapsed = time.monotonic() - started
        self.assertEqual(snapshot["state"], "idle")
        self.assertLess(elapsed, 2.0, "关闭公网不能等下一轮探测（这里设了 5 秒）")
        self.assertEqual(process.terminated, 1)
        self.assertNotEqual(quick.snapshot()["state"], "error")

    def test_probe_failure_keeps_the_tunnel_down(self) -> None:
        quick, process = self._quick(
            self._stream(NOISY_STDERR),
            probe=lambda url: (False, "等了 15 秒没有回应"),
            probe_interval=0.1,
            probe_timeout=0.5,
        )
        snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("连不上", snapshot["message"])
        self.assertIn("等了 15 秒没有回应", snapshot["message"])
        self.assertEqual(process.terminated, 1)

    def test_stop_terminates_the_process_and_goes_idle(self) -> None:
        quick, process = self._quick(self._stream(NOISY_STDERR))
        self.assertEqual(_wait_state(quick, "ready")["state"], "ready")
        snapshot = quick.stop()
        self.assertEqual(process.terminated, 1)
        self.assertEqual(snapshot["state"], "idle")
        self.assertEqual(snapshot["url"], "")

    def test_start_twice_only_spawns_one_process(self) -> None:
        process = _FakeProcess(self._stream(NOISY_STDERR))
        with mock.patch.object(cloudflare.subprocess, "Popen", return_value=process) as popen:
            quick = cloudflare.QuickTunnel(self.port, self.data_dir, probe=lambda url: (True, ""))
            self.addCleanup(quick.stop)
            quick.start()
            quick.start()
            _wait_state(quick, "ready")
        self.assertEqual(popen.call_count, 1)

    def test_cannot_start_deletes_the_broken_binary_so_retry_can_download_again(self) -> None:
        # 大小对得上却起不来（损坏 / 被安全软件拦）：留着这份文件，用户下次点开通还是同一个错，
        # 只能自己进 data\bin 手动删。必须自己删掉，让「重试」真的能重下。
        with mock.patch.object(cloudflare.subprocess, "Popen", side_effect=OSError("拒绝访问")):
            quick = cloudflare.QuickTunnel(self.port, self.data_dir)
            self.addCleanup(quick.stop)
            quick.start()
            snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn("启动 cloudflared 失败", snapshot["message"])
        self.assertIn("拒绝访问", snapshot["message"])
        self.assertIn("重试", snapshot["message"])
        self.assertFalse(self.binary.exists(), "起不来的那份程序要删掉，否则重试永远还是它")

    def test_download_failure_inside_start_is_shown_not_swallowed(self) -> None:
        with mock.patch.object(
            cloudflare, "ensure_binary", side_effect=OSError("下载到的 cloudflared 跑不起来（退出码 1）。请点「重试」重新下载。")
        ), mock.patch.object(cloudflare.subprocess, "Popen", side_effect=AssertionError("不该启动进程")):
            quick = cloudflare.QuickTunnel(self.port, self.data_dir)
            self.addCleanup(quick.stop)
            quick.start()
            snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertIn("跑不起来", snapshot["message"])
        self.assertEqual(snapshot["url"], "")

    def test_command_forces_http2_protocol(self) -> None:
        # 实测（2026-11-09 用户这台机器）：默认让 cloudflared 自己挑会选 QUIC，而它到
        # Cloudflare 边缘的 UDP 不通——地址出来了、注册一次都没成功、约 40 秒后自己退出。
        # 所以协议必须固定走 http2，不是「碰巧命令行里出现过 http2 这个字符串」。
        process = _FakeProcess(self._stream(NOISY_STDERR))
        with mock.patch.object(cloudflare.subprocess, "Popen", return_value=process) as popen:
            quick = cloudflare.QuickTunnel(self.port, self.data_dir, probe=lambda url: (True, ""))
            self.addCleanup(quick.stop)
            quick.start()
            _wait_state(quick, "ready")
        command = list(popen.call_args.args[0])
        self.assertIn("--protocol", command, "必须显式指定协议，不许让 cloudflared 自己挑")
        index = command.index("--protocol")
        self.assertEqual(command[index + 1], "http2", "--protocol 后面必须紧跟着 http2")
        self.assertLess(command.index("--no-autoupdate"), index, "参数接在 --no-autoupdate 之后")

    def test_exited_after_url_says_address_was_obtained(self) -> None:
        # 用户 2026-11-09 撞到的那次：地址明明打出来了，进程中途退出，旧文案却说
        # 「没拿到公网地址」——他会以为地址白拿了，也不知道能点「重试」换一个。
        hit = threading.Event()

        def probe(url: str) -> tuple[bool, str]:
            hit.set()
            return False, "连不上"

        quick, _process = self._quick(
            self._stream(NOISY_STDERR), probe=probe, probe_interval=0.2, probe_timeout=10.0
        )
        self.assertTrue(hit.wait(3.0), "第一次探测应该已经发生")
        process = _process
        process._returncode = 1
        snapshot = _wait_state(quick, "error")
        message = snapshot["message"]
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["url"], "")
        self.assertIn(URL, message, "地址已经拿到了，文案必须把它写出来")
        self.assertIn("中途", message)
        self.assertNotIn("没拿到公网地址", message, "地址拿到了就不许再说没拿到")
        self.assertIn("重试", message)

    def test_failure_is_logged(self) -> None:
        # 真错因常常只在 cloudflared 的 stderr 里，noticehub.log 一条都没有：
        # 那次 QUIC 连不上只能手工跑 cloudflared 才查出来。失败必须留痕。
        process = _FakeProcess(self._stream(NOISY_STDERR))
        with mock.patch.object(
            cloudflare.subprocess, "Popen", return_value=process
        ), self.assertLogs("noticehub.cloudflare", level="WARNING") as logs:
            quick = cloudflare.QuickTunnel(
                self.port,
                self.data_dir,
                probe=lambda url: (False, "连不上 %s：连接被拒绝" % url),
                probe_interval=0.05,
                probe_timeout=0.3,
            )
            self.addCleanup(quick.stop)
            quick.start()
            snapshot = _wait_state(quick, "error")
        self.assertEqual(snapshot["state"], "error")
        self.assertTrue(
            any("快速通道失败" in line for line in logs.output),
            "失败要在日志里留一条带原因的记录：%r" % logs.output,
        )


if __name__ == "__main__":  # pragma: no cover - 手工单跑用
    unittest.main()
