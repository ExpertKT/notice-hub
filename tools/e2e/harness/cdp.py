"""最小 Chrome DevTools Protocol 客户端：只依赖标准库，驱动本机 Edge。

为什么不用 Playwright：`requirements-dev.txt` 里虽然定了 playwright==1.64.1，但本机
`.venv` 与系统 Python 都没装（红线：要装东西先报负责人），所以这里用标准库自己实现
WebSocket + CDP：导航 / 求值 / 真鼠标点击 / 键盘 / 截图 / console 与网络事件采集。

用法：
    browser = Browser(headful=False)
    browser.start()
    page = browser.page
    page.navigate("http://127.0.0.1:18001/")
    page.evaluate("document.title")
    browser.stop()
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import time
import urllib.parse
import urllib.request

RESERVED_PORTS = {8765, 8766, 8767, 8768}

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

CREATE_NO_WINDOW = 0x08000000


def find_edge() -> str:
    for path in EDGE_CANDIDATES:
        if os.path.isfile(path):
            return path
    raise RuntimeError("找不到 msedge.exe：E2E 依赖本机安装的 Microsoft Edge")


def free_port(*, exclude: set[int] | None = None, low: int = 18000) -> int:
    """要一个空闲端口；避开 8765-8768（用户实例）与 Windows 保留段。"""
    exclude = set(exclude or set()) | RESERVED_PORTS
    for _ in range(80):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port >= low and port not in exclude:
            return port
    raise RuntimeError("找不到可用端口")


class WebSocket:
    """够 CDP 用的最小 RFC6455 客户端：文本帧、分片、ping 自动回 pong。"""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        # 握手时一次 recv 可能把首帧一起读回来：多出来的字节必须留着，丢掉会让第一个
        # CDP 请求永远等不到响应（表现为 Page.enable 超时）。
        self._buffer = bytearray()
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(request.encode("ascii"))
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status:
            raise RuntimeError(f"WebSocket 握手失败：{status!r}")

    # ---------------------------------------------------------------- 底层读写
    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("WebSocket 握手期间连接被关闭")
            self._buffer += chunk
        index = self._buffer.index(marker) + len(marker)
        head = bytes(self._buffer[:index])
        del self._buffer[:index]  # 首帧剩余字节留给 _read_exact
        return head

    def _read_exact(self, size: int) -> bytes:
        while len(self._buffer) < size:
            chunk = self.sock.recv(max(4096, size - len(self._buffer)))
            if not chunk:
                raise ConnectionError("WebSocket 连接已关闭")
            self._buffer += chunk
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read_exact(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if second & 0x80 else b""
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        size = len(payload)
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", size)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", size)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    # ------------------------------------------------------------------ 对外
    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self, timeout: float | None = None) -> str:
        """读一条完整文本消息；timeout 给定时超时抛 socket.timeout。"""
        if timeout is not None:
            self.sock.settimeout(timeout)
        chunks = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("WebSocket 被远端关闭")
            if opcode in (0x1, 0x2, 0x0):
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8", errors="replace")
                continue

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    """请求/响应配对 + 事件缓冲。events 由调用方 drain() 取走。"""

    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self._next_id = 1
        self.events: list[dict] = []

    def call(self, method: str, timeout: float = 30.0, **params) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self.ws.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                message = json.loads(self.ws.recv(timeout=remaining))
            except socket.timeout:
                raise TimeoutError(f"CDP {method} 超过 {timeout}s 没有响应") from None
            if message.get("id") == request_id:
                if "error" in message:
                    raise RuntimeError(f"CDP {method} 失败：{message['error']}")
                return message.get("result") or {}
            self.events.append(message)

    def drain(self) -> list[dict]:
        events, self.events = self.events, []
        return events

    def pump(self, seconds: float) -> list[dict]:
        """在 seconds 内持续收集事件（不阻塞业务）。"""
        collected: list[dict] = []
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                collected.append(json.loads(self.ws.recv(timeout=remaining)))
            except socket.timeout:
                break
        return collected


class Page:
    """一个标签页上的操作集合。"""

    def __init__(self, cdp: CDP) -> None:
        self.cdp = cdp
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[str] = []
        # requestId → 方法名：失败请求要能报出 method/url/status，否则只有 URL 看不出是哪个接口。
        self.request_methods: dict[str, str] = {}

    # ------------------------------------------------------------ 事件归类
    def collect(self, *, timeout: float = 0.15) -> None:
        events = self.cdp.events + self.cdp.pump(timeout)
        self.cdp.events = []
        for event in events:
            self._handle_event(event)

    def _handle_event(self, event: dict) -> None:
        method = event.get("method")
        params = event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            detail = params.get("exceptionDetails") or {}
            text = str((detail.get("exception") or {}).get("description") or detail.get("text") or "未知异常")
            self.page_errors.append(text.split("\n")[0][:500])
        elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
            parts = []
            for item in params.get("args") or []:
                parts.append(str(item.get("value", item.get("description", "")))[:300])
            self.console_errors.append(" ".join(parts)[:500])
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if str(entry.get("level")) == "error":
                self.console_errors.append(str(entry.get("text") or "")[:500])
        elif method == "Network.requestWillBeSent":
            request = params.get("request") or {}
            self.request_methods[str(params.get("requestId"))] = str(request.get("method") or "GET")
        elif method == "Network.responseReceived":
            response = params.get("response") or {}
            status = int(response.get("status") or 0)
            if status >= 400:
                verb = self.request_methods.get(str(params.get("requestId")), "")
                self.failed_requests.append(f"{(verb + ' ') if verb else ''}{status} {response.get('url')}")
        elif method == "Network.loadingFailed":
            url = params.get("url") or ""
            reason = str(params.get("errorText") or params.get("blockedReason") or "failed")
            if not params.get("canceled"):
                verb = self.request_methods.get(str(params.get("requestId")), "")
                self.failed_requests.append(f"{(verb + ' ') if verb else ''}{reason} {url}")

    # ---------------------------------------------------------------- 导航
    def navigate(self, url: str, *, timeout: float = 20.0) -> None:
        self.cdp.call("Page.navigate", url=url, timeout=timeout)
        self.wait_ready(timeout=timeout)

    def wait_ready(self, *, timeout: float = 20.0) -> None:
        deadline = time.monotonic() + timeout
        while True:
            state = self.evaluate("document.readyState")
            if state == "complete":
                break
            if time.monotonic() > deadline:
                raise TimeoutError(f"页面 {timeout}s 内没有就绪（readyState={state}）")
            time.sleep(0.05)
        # 首屏脚本还要跑一轮，给个短窗口收事件
        self.collect(timeout=0.2)

    # ---------------------------------------------------------------- 求值
    def evaluate(self, expression: str, *, timeout: float = 30.0):
        result = self.cdp.call(
            "Runtime.evaluate",
            timeout=timeout,
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
            userGesture=True,
        )
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            text = str((details.get("exception") or {}).get("description") or details.get("text") or "")
            raise AssertionError(f"页面脚本执行失败：{text.splitlines()[0] if text else details}")
        return ((result.get("result") or {}).get("value"))

    # ---------------------------------------------------------------- 鼠标
    def click(self, selector: str) -> None:
        rect = self.evaluate(
            "(function(){var el=document.querySelector(%s);if(!el)return null;"
            # 先滚到视口中间：右栏/长页面里的按钮常在首屏外，直接按旧坐标点会打到别处。
            "el.scrollIntoView({block:'center',inline:'center'});"
            "var r=el.getBoundingClientRect();"
            "var hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);"
            "return {x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height,"
            "hit:hit?(hit.id?'#'+hit.id:hit.tagName.toLowerCase()+(hit.className?'.'+String(hit.className).split(' ')[0]:'')):null,"
            "onTop:!!hit&&(hit===el||el.contains(hit))};})()"
            % json.dumps(selector)
        )
        if not rect or rect["w"] <= 0 or rect["h"] <= 0:
            raise AssertionError(f"元素不可点击（不存在或尺寸为 0）：{selector}")
        if not rect["onTop"]:
            # 被遮罩盖住时，鼠标事件不会到目标元素上——直接说清谁盖着，别让它静默不生效。
            raise AssertionError(
                f"元素被遮挡，点击不会生效：{selector}（该点最上层是 {rect['hit'] or '无元素'}）"
            )
        x, y = round(rect["x"]), round(rect["y"])
        # buttons=1（左键按下）必须给：缺了它 Chromium 认为「没有按键按住」，
        # 页面收不到 click，tab 之类的 JS 不会切换。
        self.cdp.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y, button="none", buttons=0)
        self.cdp.call(
            "Input.dispatchMouseEvent",
            type="mousePressed",
            x=x,
            y=y,
            button="left",
            buttons=1,
            clickCount=1,
        )
        self.cdp.call(
            "Input.dispatchMouseEvent",
            type="mouseReleased",
            x=x,
            y=y,
            button="left",
            buttons=0,
            clickCount=1,
        )
        self.collect(timeout=0.15)

    # ---------------------------------------------------------------- 文本
    def fill(self, selector: str, value: str) -> None:
        ok = self.evaluate(
            "(function(){var el=document.querySelector(%s);if(!el)return false;"
            "el.focus();"
            "var setter=Object.getOwnPropertyDescriptor(el.constructor.prototype,'value');"
            "if(setter&&setter.set){setter.set.call(el,%s);}else{el.value=%s;}"
            "el.dispatchEvent(new Event('input',{bubbles:true}));"
            "el.dispatchEvent(new Event('change',{bubbles:true}));return true;})()"
            % (json.dumps(selector), json.dumps(value), json.dumps(value))
        )
        if not ok:
            raise AssertionError(f"找不到输入框：{selector}")
        self.collect(timeout=0.1)

    def press(self, selector: str, key: str) -> None:
        keys = {
            "Enter": ("Enter", 13, "\r"),
            "Tab": ("Tab", 9, "\t"),
            "Escape": ("Escape", 27, ""),
            "Backspace": ("Backspace", 8, ""),
        }
        if key not in keys:
            raise ValueError(f"press 暂不支持按键：{key}")
        name, code, text = keys[key]
        self.evaluate(
            "(function(){var el=document.querySelector(%s);if(el)el.focus();return true;})()" % json.dumps(selector)
        )
        self.cdp.call(
            "Input.dispatchKeyEvent",
            type="keyDown",
            key=name,
            windowsVirtualKeyCode=code,
            nativeVirtualKeyCode=code,
            text=text,
        )
        self.cdp.call(
            "Input.dispatchKeyEvent",
            type="keyUp",
            key=name,
            windowsVirtualKeyCode=code,
            nativeVirtualKeyCode=code,
        )
        self.collect(timeout=0.1)

    # ---------------------------------------------------------------- 截图
    def screenshot(self, path: str) -> str:
        data = self.cdp.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)["data"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(data))
        return path

    def set_viewport(self, width: int, height: int) -> None:
        self.cdp.call(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=1,
            mobile=False,
        )


class Browser:
    """启动/关闭一个独立的 Edge 实例（独立 profile，互不干扰）。"""

    def __init__(self, *, headful: bool = False, profile_dir: str, launch_timeout: float = 30.0) -> None:
        self.headful = headful
        self.profile_dir = profile_dir
        self.launch_timeout = launch_timeout
        self.port = free_port()
        self.proc: subprocess.Popen | None = None
        self.ws: WebSocket | None = None
        self.cdp: CDP | None = None
        self.page: Page | None = None

    def start(self) -> Page:
        executable = find_edge()
        args = [
            executable,
            f"--remote-debugging-port={self.port}",
            f"--user-data-dir={self.profile_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-gpu",
            "--window-size=1280,900",
        ]
        if not self.headful:
            args.append("--headless=new")
        args.append("about:blank")
        creationflags = CREATE_NO_WINDOW if os.name == "nt" else 0
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        target = self._wait_target()
        self.ws = WebSocket(target["webSocketDebuggerUrl"])
        self.cdp = CDP(self.ws)
        self.page = Page(self.cdp)
        for domain in ("Page", "Runtime", "Log", "Network", "DOM"):
            self.cdp.call(f"{domain}.enable")
        return self.page

    def _wait_target(self) -> dict:
        deadline = time.monotonic() + self.launch_timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                for target in targets:
                    if target.get("type") == "page" and target.get("webSocketDebuggerUrl"):
                        return target
            except Exception as error:  # noqa: BLE001 - 浏览器还没起来
                last_error = str(error)
            time.sleep(0.2)
        raise RuntimeError(f"Edge 调试端口 {self.port} 在 {self.launch_timeout}s 内没就绪（{last_error}）")

    def stop(self) -> None:
        if self.ws is not None:
            self.ws.close()
            self.ws = None
        if self.proc is not None and self.proc.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:  # pragma: no cover - 只在 Windows 上跑
                self.proc.kill()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                pass
        self.proc = None
