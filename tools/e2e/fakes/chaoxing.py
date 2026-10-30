"""W3 平台替身：学习通接口（登录 / 课程 / 作业 / 通知 / 课时）。

接线方式（`QQ_DIGEST_CHAOXING_BASE`）
------------------------------------
`qq_live_digest/chaoxing_receiver.py` 的 `_resolve_endpoints()` 读
`QQ_DIGEST_CHAOXING_BASE`：**只替换 scheme+host，path/query 保持写死的值**；不设置时
行为与写死主机完全一致（后端2 已落地，`tests/test_chaoxing.py` 覆盖）。
所以本替身给的环境变量是 `QQ_DIGEST_CHAOXING_BASE=http://127.0.0.1:<本替身端口>`，
`ChaoxingClient` 一 new 就指向这里，`validate_login()/my_courses()/notices()/work_list()`
全走本地。

已挂的六条路由（必须与产品 `ENDPOINTS` 的 path 一字不差）：

```
/apis/login/userLogin4Uname.do
/base
/mooc2-ans/visit/courselistdata
/pc/notice/getNoticeList
/mooc2/work/list
/pc/curriculum/getMyLessons
```

⚠️ 这个变量**只给 E2E 用**，不要写进用户 `.env`：写错会把真实 Cookie 发到别的地址。
路由按**路径后缀**匹配、忽略主机与 scheme，正好对应「只换 host、保留 path」的覆盖方式。
全部随机端口启动，`stop()` 关端口。
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

FID = "1234"

#: 产品 `ENDPOINTS` 的 path 部分（base 覆盖只换 scheme+host，path 不变），一字不差。
ROUTES = (
    "/apis/login/userLogin4Uname.do",
    "/base",
    "/mooc2-ans/visit/courselistdata",
    "/pc/notice/getNoticeList",
    "/mooc2/work/list",
    "/pc/curriculum/getMyLessons",
    # 「逐门课抓作业」链路：课程详情页（带作业签名）+ 单条作业详情页
    "/visit/stucoursemiddle",
    "/mooc2/work/detail",
)

#: 后端2 在 `chaoxing_receiver.py` 里开的覆盖开关（仅 E2E 用）。
BASE_ENV = "QQ_DIGEST_CHAOXING_BASE"

COURSE_LIST_HTML = """<!doctype html><html><body>
<div class="course-list">
  <div class="course-card"><a href="/visit/stucoursemiddle?courseid=200000001&clazzid=300000001&cpi=400000001">
    <span class="course-name" title="形势与政策"></span></a></div>
  <div class="course-card"><a href="/visit/stucoursemiddle?courseid=200000002&clazzid=300000002&cpi=400000002">
    <span class="course-name" title="大学英语"></span></a></div>
</div>
</body></html>"""

BASE_HTML = f"""<!doctype html><html><body data-fid="{FID}" class="base">
<input id="fid" value="{FID}">
<a href="/mooc2-ans/visit/courselistdata?courseType=1&courseFid={FID}">课程列表</a>
<a href="/visit/stucoursemiddle?courseid=200000001&clazzid=300000001&cpi=400000001">课程</a>
</body></html>"""

WORK_LIST_HTML = """<!doctype html><html><body><ul>
<li onclick="goTask(this);" data="/mooc2/work/detail?workId=w1">
  <p class="overHidden2 fl">第 3 章 课堂作业</p>
  <p class="status fl">未交</p>
  <p class="overHidden2 fl">作答时间：10-20 08:00 至 10-27 23:59</p>
</li>
<li onclick="goTask(this);" data="/mooc2/work/detail?workId=w2">
  <p class="overHidden2 fl">期中测试</p>
  <p class="status fl">已交</p>
  <p class="overHidden2 fl">作答时间：10-25 08:00 至 11-02 23:59</p>
</li>
</ul>
<input id="workEnc" value="enc-e2e">
<input id="openc" value="openc-e2e">
<input id="enc" value="stuenc-e2e">
</body></html>"""

#: 课程详情页：作业签名（extract_work_signatures 要求 workEnc/openc/enc 三个都在）。
COURSE_DETAIL_HTML = """<!doctype html><html><body class="course-detail">
<h2>形势与政策</h2>
<input id="workEnc" value="enc-e2e">
<input id="openc" value="openc-e2e">
<input id="enc" value="stuenc-e2e">
</body></html>"""

#: 单条作业详情页：截止时间必须在未来，否则接收端按「已过期」丢弃（chaoxing_receiver.py:414）。
WORK_DETAIL_HTML = """<!doctype html><html><body>
<p class="overHidden2 fl">第 3 章 课堂作业</p>
<p>作答时间：10-20 08:00 至 11-30 23:59</p>
<p>截止时间：11-30 23:59</p>
</body></html>"""

NOTICES = {
    "notices": {
        "list": [
            {"id": "n1", "title": "关于国庆节放假安排的通知", "createTime": "2026-10-20 10:00"},
            {"id": "n2", "title": "本周五班会改线上", "createTime": "2026-10-21 09:30"},
        ]
    }
}

LESSONS = {"data": [{"name": "第 1 章 绪论", "courseId": "200000001"}]}


def _make_handler(mode: str, delay: float, requests: list[dict[str, Any]], lock: threading.Lock):
    class Handler(BaseHTTPRequestHandler):
        server_version = "nh-fake-chaoxing/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002 - 基类签名
            return

        def _record(self) -> None:
            with lock:
                requests.append({"method": self.command, "path": self.path, "at": time.time()})

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def _html(self, text: str, status: int = 200) -> None:
            self._send(status, text.encode("utf-8"), "text/html; charset=utf-8")

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802 - 基类命名
            self._record()
            path = urllib.parse.urlsplit(self.path).path
            if mode == "timeout":
                time.sleep(max(0.0, delay))
                self._html("timeout", 200)
                return
            if mode == "expired":
                # 真站过期时的表现：401/403，或跳登录页（这里直接给 401，客户端会判 AuthError）
                self._send(401, b"login required", "text/html; charset=utf-8")
                return
            if path in ("/apis/login/userLogin4Uname.do",):
                self._json({"result": 1, "msg": {"name": "E2E 学生"}})
                return
            if path == "/base":
                self._html(BASE_HTML)
                return
            if path == "/mooc2-ans/visit/courselistdata":
                self._html(COURSE_LIST_HTML)
                return
            if path == "/pc/notice/getNoticeList":
                self._json(NOTICES)
                return
            if path == "/mooc2/work/list":
                self._html(WORK_LIST_HTML)
                return
            if path == "/pc/curriculum/getMyLessons":
                self._json(LESSONS)
                return
            if path == "/visit/stucoursemiddle":
                # 课程详情页：带作业签名，供「逐门课抓作业」用
                self._html(COURSE_DETAIL_HTML)
                return
            if path == "/mooc2/work/detail":
                # 单条作业详情：接收端从这里的「作答时间/截止时间」解截止日期
                self._html(WORK_DETAIL_HTML)
                return
            self._html("<html><body>not found</body></html>", 404)

    return Handler


class ChaoxingFake:
    def __init__(self, *, mode: str = "ok", delay: float = 20.0) -> None:
        if mode not in {"ok", "expired", "timeout"}:
            raise ValueError("mode 只支持 ok / expired / timeout，拿到 %r" % (mode,))
        self.mode = mode
        self.delay = delay
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(mode, delay, self.requests, self.lock))
        self.thread = threading.Thread(target=self.server.serve_forever, name="fake-chaoxing", daemon=True)

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:%d" % self.port

    def start(self) -> "ChaoxingFake":
        self.thread.start()
        return self

    def hit_count(self) -> int:
        with self.lock:
            return len(self.requests)

    def stop(self) -> None:
        try:
            self.server.shutdown()
        finally:
            self.server.server_close()
            self.thread.join(timeout=5)


def start(mode: str = "ok", delay: float = 20.0) -> dict[str, Any]:
    """统一入口：起学习通替身并返回 handle 字典。"""
    fake = ChaoxingFake(mode=mode, delay=delay).start()
    base = fake.base_url.rstrip("/")
    return {
        "name": "chaoxing",
        "kind": "http",
        "port": fake.port,
        "base_url": base,
        "env": {BASE_ENV: base},
        "info": {
            "mode": mode,
            "wiring": "ready: 注入 QQ_DIGEST_CHAOXING_BASE 后 ChaoxingClient 直接指向本替身（只换 scheme+host，path 不变）",
            "base_env": BASE_ENV,
            "routes": list(ROUTES),
            "fid": FID,
        },
        "impl": fake,
        "stop": fake.stop,
    }
