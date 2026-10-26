import io
import logging
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from qq_live_digest.chaoxing_receiver import (
    ChaoxingClient,
    ChaoxingError,
    ChaoxingHTTPError,
    ENDPOINTS,
    parse_deadline,
    to_record,
)


class ChaoxingTests(unittest.TestCase):
    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_unauthorized_response_is_expired_cookie(self, opener):
        opener.side_effect = HTTPError("https://i.chaoxing.com/base", 403, "forbidden", {}, io.BytesIO(b""))
        with self.assertRaises(Exception) as ctx:
            ChaoxingClient("COOKIE-SHOULD-STAY-PRIVATE")._get("https://i.chaoxing.com/base")
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError
        self.assertIsInstance(ctx.exception, ChaoxingAuthError)
        self.assertNotIn("COOKIE-SHOULD-STAY-PRIVATE", str(ctx.exception))
        class LoginPage:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return "<html>请登录</html>".encode()
            def getcode(self): return 200
            def geturl(self): return "https://passport2.chaoxing.com/login"
        opener.side_effect = None
        opener.return_value = LoginPage()
        with self.assertRaises(ChaoxingAuthError):
            ChaoxingClient("COOKIE-SHOULD-STAY-PRIVATE")._get("https://i.chaoxing.com/base")

    def test_expired_status_endpoint_exposes_banner_fields_without_cookie(self):
        import urllib.request
        from qq_live_digest.config import Settings
        from qq_live_digest.store import Store
        from qq_live_digest.webapp import TaskWebServer
        secret = "COOKIE-MUST-NOT-LEAK"
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(tmp), chaoxing_cookie=secret)
            settings.chaoxing_status = {"ok": False, "expired": True, "last_sync_at": "2026-10-10T20:00:00+08:00", "last_error": "Cookie 已过期（需重新登录）"}
            server = TaskWebServer(settings, Store(Path(tmp) / "test.sqlite3"))
            server.start()
            self.addCleanup(server.stop)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server.server_address[1]}/api/chaoxing/status", headers={"X-Token": "test-token"})
            with urllib.request.urlopen(request) as response:
                body = response.read().decode("utf-8")
            self.assertIn('"expired": true', body)
            self.assertIn("读取学习通 Cookie", body)
            self.assertIn("https://passport2.chaoxing.com/login", body)
            self.assertIn('"has_cookie": true', body)
            self.assertNotIn(secret, body)

    def test_cookie_file_fallback_and_env_precedence(self):
        from qq_live_digest.config import Settings
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chaoxing.cookie"
            path.write_text("COOKIE-FROM-FILE", encoding="utf-8")
            base = {"QQ_DIGEST_DATA_DIR": tmp}
            self.assertEqual(Settings.from_env(base).chaoxing_cookie, "COOKIE-FROM-FILE")
            env = dict(base, QQ_DIGEST_CHAOXING_COOKIE="COOKIE-FROM-ENV")
            self.assertEqual(Settings.from_env(env).chaoxing_cookie, "COOKIE-FROM-ENV")

    def test_courses_endpoint_no_cookie_is_safe(self):
        import urllib.request
        from qq_live_digest.config import Settings
        from qq_live_digest.store import Store
        from qq_live_digest.webapp import TaskWebServer
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.sqlite3")
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(tmp))
            server = TaskWebServer(settings, store)
            server.start()
            self.addCleanup(server.stop)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server.server_address[1]}/api/chaoxing/courses", headers={"X-Token": "test-token"})
            with self.assertRaises(Exception) as ctx:
                urllib.request.urlopen(request)
            self.assertEqual(ctx.exception.code, 400)
            self.assertNotIn("secret", ctx.exception.read().decode("utf-8").lower())

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_courses_endpoint_auth_error_is_chinese_and_actionable(self, opener):
        import urllib.request
        from qq_live_digest.config import Settings
        from qq_live_digest.store import Store
        from qq_live_digest.webapp import TaskWebServer
        opener.side_effect = HTTPError("https://i.chaoxing.com/base", 403, "forbidden", {}, io.BytesIO(b""))
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(tmp), chaoxing_cookie="sid=1")
            server = TaskWebServer(settings, Store(Path(tmp) / "test.sqlite3")); server.start(); self.addCleanup(server.stop)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server.server_address[1]}/api/chaoxing/courses", headers={"X-Token": "test-token"})
            with self.assertRaises(Exception) as ctx: urllib.request.urlopen(request)
            body = ctx.exception.read().decode("utf-8")
            self.assertIn("学习通登录状态无效或已过期", body)
            self.assertNotIn("HTTP 502", body)

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_courses_endpoint_network_error_does_not_echo_cookie(self, opener):
        import urllib.request
        from urllib.error import URLError
        from qq_live_digest.config import Settings
        from qq_live_digest.store import Store
        from qq_live_digest.webapp import TaskWebServer
        secret = "SENSITIVE-COOKIE-VALUE"
        opener.side_effect = URLError("unreachable")
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(web_host="127.0.0.1", web_port=0, web_token="test-token", data_dir=Path(tmp), chaoxing_cookie=secret)
            server = TaskWebServer(settings, Store(Path(tmp) / "test.sqlite3"))
            server.start()
            self.addCleanup(server.stop)
            request = urllib.request.Request(f"http://127.0.0.1:{server.server.server_address[1]}/api/chaoxing/courses", headers={"X-Token": "test-token"})
            with self.assertRaises(Exception) as ctx:
                urllib.request.urlopen(request)
            body = ctx.exception.read().decode("utf-8")
            self.assertEqual(ctx.exception.code, 502)
            self.assertNotIn(secret, body)

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_my_courses_parses_synthetic_payload(self, opener):
        body = b'<div href="/visit/stucoursemiddle?courseid=1&clazzid=2&cpi=3"></div><span class="course-name overHidden2" title="Math"></span>'
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return body
            def getcode(self): return 200
        opener.return_value = Response()
        self.assertEqual(ChaoxingClient("fid=2847").my_courses(), [{"course_id": "1", "course_name": "Math", "clazz_id": "2", "cpi": "3"}])

    def test_record_and_stable_id(self):
        a = to_record("c1", "数学", "n9", "标题", "正文", received_at="2026-01-01T00:00:00+08:00")
        b = to_record("c1", "数学", "n9", "标题", "正文", received_at="2026-01-02T00:00:00+08:00")
        self.assertEqual(a["msg_id"], "chaoxing:c1:n9")
        self.assertEqual(a["msg_id"], b["msg_id"])
        self.assertEqual(set(a), {"msg_id", "source", "event", "group_id", "group_name", "sender_id", "sender_name", "ts", "received_at", "content"})

    def test_headers_redact_cookie_in_logger(self):
        cookie = "UID=secret-value; JSESSIONID=abc"
        client = ChaoxingClient(cookie)
        self.assertEqual(client._headers()["Cookie"], cookie)
        stream = io.StringIO()
        logger = logging.getLogger("chaoxing-test")
        logger.handlers[:] = [logging.StreamHandler(stream)]
        logger.warning("Cookie: <redacted>")
        self.assertNotIn(cookie, stream.getvalue())

    def test_url_constants(self):
        self.assertTrue(ENDPOINTS["notices"].startswith("https://notice.chaoxing.com/"))
        self.assertTrue(ENDPOINTS["work_list"].startswith("https://mooc1.chaoxing.com/"))

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_http_and_network_errors(self, opener):
        opener.side_effect = HTTPError("https://example.invalid", 503, "down", {}, io.BytesIO(b""))
        with self.assertRaises(ChaoxingHTTPError):
            ChaoxingClient("x").notices()
        opener.side_effect = URLError("timed out")
        with self.assertRaises(ChaoxingError):
            ChaoxingClient("x").notices()

    def test_deadline_from_constructed_html(self):
        import datetime as dt

        self.assertEqual(parse_deadline("<span>截止：07-01 18:51</span>", year=2026), "2026-07-01 18:51")
        self.assertEqual(parse_deadline("2026-07-01 18:51"), "2026-07-01 18:51")
        self.assertEqual(parse_deadline("<span>2026年7月1日 18:51</span>"), "2026-07-01 18:51")
        self.assertEqual(parse_deadline("<span>2026/7/1 18:51</span>"), "2026-07-01 18:51")
        self.assertIsNone(parse_deadline("没有截止时间"))
        self.assertIsNone(parse_deadline("18:51"))
        self.assertEqual(parse_deadline("开始时间：2026-10-08 11:40结束时间：2026-10-08 23:59"), "2026-10-08 23:59")
        self.assertEqual(parse_deadline("考试时间：2026-11-01 09:00"), "2026-11-01 09:00")

    def test_deadline_without_year_rolls_forward_only_when_long_past(self):
        import datetime as dt

        reference = dt.datetime(2026, 12, 20, 9, 0)
        self.assertEqual(parse_deadline("01-05 08:00", now=reference), "2027-01-05 08:00")
        self.assertEqual(parse_deadline("12-20 08:00", now=reference), "2026-12-20 08:00")
        self.assertEqual(parse_deadline("07-01 18:51", year=2026, now=reference), "2026-07-01 18:51")

    def test_work_records_skip_course_with_zero_identifier(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace

        class Client:
            def __init__(self): self.calls = 0
            def _get(self, *args, **kwargs):
                self.calls += 1
                return 200, b"", "work page"

        settings = SimpleNamespace(chaoxing_group_whitelist=())
        reader = ChaoxingReader(settings, lambda record: None)
        client = Client()
        self.assertEqual(
            reader._work_records(
                client,
                [{"course_id": "c1", "clazz_id": 0, "cpi": "3"}],
                baseline=False,
            ),
            [],
        )
        self.assertEqual(client.calls, 0)

    def test_work_records_run_concurrently_and_keep_serial_order(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace
        import qq_live_digest.chaoxing_receiver as receiver

        class Client:
            def _get(self, url, params=None):
                time.sleep(0.1)
                return 200, b"", "work page"
            def extract_work_signatures(self, page): return {"workEnc": "w", "openc": "o", "enc": "e"}
            def work_list(self, course_id, *, clazz_id, cpi, signatures):
                time.sleep(0.1)
                return [{"status": "未提交", "url": f"https://example.test/?workId={course_id}", "title": f"作业{course_id}"}]
            def work_detail(self, url):
                time.sleep(0.1)
                return "work detail"
            def parse_work_window(self, page): return ("", "2099-12-31 23:59")

        courses = [{"course_id": f"c{i}", "course_name": f"课{i}", "clazz_id": "2", "cpi": "3"} for i in range(8)]
        settings = SimpleNamespace(chaoxing_group_whitelist=())
        reader = ChaoxingReader(settings, lambda record: None)
        client = Client()
        with patch.object(receiver, "WORK_DETAIL_DELAY_SECONDS", 0.0):
            with patch.object(receiver, "WORK_WORKERS", 1):
                started = time.monotonic()
                serial = reader._work_records(client, courses, baseline=False)
                serial_elapsed = time.monotonic() - started
            with patch.object(receiver, "WORK_WORKERS", 4):
                started = time.monotonic()
                parallel = reader._work_records(client, courses, baseline=False)
                parallel_elapsed = time.monotonic() - started
        stable = lambda rows: [(row["msg_id"], row["group_name"], row["content"]) for row in rows]
        self.assertEqual(stable(parallel), stable(serial))
        self.assertEqual(len(serial), 8)
        self.assertEqual([f"chaoxing:work:c{i}:c{i}" for i in range(8)], [row["msg_id"] for row in serial])
        self.assertLess(parallel_elapsed, serial_elapsed * 0.6)

    def test_work_records_isolate_course_failure(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace

        class Client:
            def _get(self, url, params=None):
                if "courseid=bad" in url: raise ChaoxingError("boom")
                if "courseid=dead" in url: raise ChaoxingAuthError("expired")
                return 200, b"", "work page"
            def extract_work_signatures(self, page): return {"workEnc": "w", "openc": "o", "enc": "e"}
            def work_list(self, course_id, *, clazz_id, cpi, signatures):
                return [{"status": "未提交", "url": f"https://example.test/?workId={course_id}", "title": f"作业{course_id}"}]
            def work_detail(self, url): return "work detail"
            def parse_work_window(self, page): return ("", "2099-12-31 23:59")

        reader = ChaoxingReader(SimpleNamespace(chaoxing_group_whitelist=()), lambda record: None)
        client = Client()
        courses = [{"course_id": cid, "clazz_id": "2", "cpi": "3"} for cid in ("a", "bad", "b")]
        with patch("qq_live_digest.chaoxing_receiver.WORK_DETAIL_DELAY_SECONDS", 0.0):
            records = reader._work_records(client, courses, baseline=False)
        self.assertEqual(["chaoxing:work:a:a", "chaoxing:work:b:b"], [record["msg_id"] for record in records])
        with patch("qq_live_digest.chaoxing_receiver.WORK_DETAIL_DELAY_SECONDS", 0.0):
            with self.assertRaises(ChaoxingAuthError):
                reader._work_records(client, [{"course_id": "dead", "clazz_id": "2", "cpi": "3"}], baseline=False)

    def test_reader_baselines_existing_notices(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace
        class Store:
            def __init__(self): self.saved = None
            def chaoxing_sync_state(self): return {"notice_ids": []}
            def save_chaoxing_sync_state(self, state): self.saved = state
        class Client:
            reader = None
            def __init__(self, *args, **kwargs): pass
            def notices(self):
                rows = [{"uuid": "n1", "content": "课程名称：数学", "title": "旧"}]
                self.reader.stop_event.set()
                return rows
            def my_courses(self): return [{"course_id": "c1", "course_name": "数学"}]
        store, got = Store(), []
        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=("c1",), chaoxing_scan_interval_seconds=1800)
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=store)
            Client.reader = reader
            reader._run()
        self.assertEqual(got, [])
        self.assertEqual(store.saved["notice_ids"], ["n1"])

    def test_reader_runs_work_records_and_preserves_empty_value_fallbacks(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace

        class Store:
            def chaoxing_sync_state(self): return {"notice_ids": ["old"]}
            def save_chaoxing_sync_state(self, state): self.saved = state

        class Client(ChaoxingClient):
            reader = None
            def notices(self):
                self.reader.stop_event.set()
                return {"data": [
                    {"uuid": "matched", "content": "通知", "attachment": [{"courseId": "1"}]},
                    {"uuid": "unmatched", "content": "课程名称：数学"},
                ]}
            def my_courses(self):
                return [
                    {"course_id": "1", "course_name": "", "clazz_id": "2", "cpi": "3"},
                    {"course_id": None, "course_name": "数学", "clazz_id": "4", "cpi": "5"},
                ]
            def _get(self, url, params=None):
                self.work_url = url
                return 200, b"", "work page"
            def extract_work_signatures(self, page): return {"workEnc": "w", "openc": "o", "enc": "e"}
            def work_list(self, course_id, *, clazz_id, cpi, signatures):
                return [{"status": "未提交", "url": "https://example.test/?workId=w1", "title": "作业"}]
            def work_detail(self, url): return "work detail"
            def parse_work_window(self, page): return ("", "2099-12-31 23:59")

        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=(), chaoxing_scan_interval_seconds=1800)
        store, got, client = Store(), [], Client("x")
        client.reader = ChaoxingReader(settings, got.append, store=store)
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", return_value=client), patch("qq_live_digest.chaoxing_receiver.time.sleep"):
            client.reader._run()

        self.assertEqual([(r["msg_id"], r["group_name"]) for r in got], [
            ("chaoxing:1:matched", "1"),
            ("chaoxing:unknown:unmatched", "数学"),
            ("chaoxing:work:1:w1", "1"),
        ])
        self.assertIn("courseid=1&clazzid=2&cpi=3", client.work_url)
        self.assertNotIn("courseid=None", client.work_url)
        self.assertTrue(settings.chaoxing_status["ok"])
        self.assertCountEqual(store.saved["notice_ids"], ["old", "matched", "unmatched"])

    def test_reader_treats_null_notice_data_as_empty(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace

        class Store:
            def chaoxing_sync_state(self): return {"notice_ids": ["old"]}
            def save_chaoxing_sync_state(self, state): self.saved = state

        class Client:
            reader = None
            def __init__(self, *args, **kwargs): pass
            def notices(self):
                self.reader.stop_event.set()
                return {"data": None}
            def my_courses(self): return []

        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=(), chaoxing_scan_interval_seconds=1800)
        store, got = Store(), []
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=store)
            Client.reader = reader
            reader._run()

        self.assertEqual(got, [])
        self.assertTrue(settings.chaoxing_status["ok"])
        self.assertEqual(store.saved["notice_ids"], ["old"])

    def test_reader_skips_malformed_course_rows_and_persists_notices(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace

        class Store:
            def __init__(self): self.saved = None
            def chaoxing_sync_state(self): return {"notice_ids": ["old"]}
            def save_chaoxing_sync_state(self, state): self.saved = state

        class Client:
            reader = None
            def __init__(self, *args, **kwargs): pass
            def notices(self):
                Client.reader.stop_event.set()
                return [
                    {"uuid": "n1", "content": "通知一", "attachment": [{"courseId": "c1"}]},
                    {"uuid": "n2", "content": "通知二", "attachment": [{"courseId": "1"}]},
                ]
            def my_courses(self):
                return [{"course_id": "c1", "course_name": "数学"}, None, {"course_id": 1}]

        store, got = Store(), []
        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=(), chaoxing_scan_interval_seconds=1800)
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=store)
            Client.reader = reader
            reader._run()

        self.assertEqual([record["msg_id"] for record in got], ["chaoxing:c1:n1", "chaoxing:1:n2"])
        self.assertEqual(got[1]["group_name"], "1")
        self.assertTrue(settings.chaoxing_status["ok"])
        self.assertTrue(settings.chaoxing_status["last_sync_at"])
        self.assertCountEqual(store.saved["notice_ids"], ["old", "n1", "n2"])

    def test_reader_baseline_pushes_future_deadline(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace
        class Store:
            def chaoxing_sync_state(self): return {"notice_ids": []}
            def save_chaoxing_sync_state(self, state): self.saved = state
        class Client:
            reader = None
            def __init__(self, *args, **kwargs): pass
            def notices(self):
                Client.reader.stop_event.set()
                return [{"uuid": "future", "content": "课程名称：数学\n结束时间：2099-01-01 00:00"}]
            def my_courses(self): return [{"course_id": "c1", "course_name": "数学"}]
        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=("c1",), chaoxing_scan_interval_seconds=1800)
        got = []
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=Store()); Client.reader = reader; reader._run()
        self.assertEqual(len(got), 1)

    def test_reader_baseline_skips_expired_deadline(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingReader
        from types import SimpleNamespace
        class Store:
            def chaoxing_sync_state(self): return {"notice_ids": []}
            def save_chaoxing_sync_state(self, state): self.saved = state
        class Client:
            reader = None
            def __init__(self, *args, **kwargs): pass
            def notices(self):
                Client.reader.stop_event.set()
                return [{"uuid": "old", "content": "结束时间：2000-01-01 00:00"}]
            def my_courses(self): return [{"course_id": "c1", "course_name": "数学"}]
        settings = SimpleNamespace(chaoxing_cookie="x", http_timeout=1, chaoxing_group_whitelist=("c1",), chaoxing_scan_interval_seconds=1800)
        got = []
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=Store()); Client.reader = reader; reader._run()
        self.assertEqual(got, [])

    def test_fanyalogin_subsite_redirect_does_not_flip_cookie_to_expired(self):
        # issue #1：泛雅子站/作业页缺子站 Cookie 回落到登录页，登录本身仍有效，不能判过期
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace
        class Store:
            def chaoxing_sync_state(self): return {"notice_ids": ["old"]}
            def save_chaoxing_sync_state(self, state): self.saved = state
        class Client:
            reader = None
            made = 0
            def __init__(self, *args, **kwargs):
                Client.made += 1
                self.is_probe = Client.made > 1
                self.reader = Client.reader
            def validate_login(self):
                assert self.is_probe
            def notices(self):
                Client.reader.stop_event.set()
                return [{"uuid": "n1", "content": "通知", "attachment": [{"courseId": "c1"}]}]
            def my_courses(self): return [{"course_id": "c1", "course_name": "数学", "clazz_id": "2", "cpi": "3"}]
            def _get(self, url, params=None):
                raise ChaoxingAuthError("Chaoxing login session is invalid or expired")
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1, chaoxing_group_whitelist=(), chaoxing_scan_interval_seconds=1800)
        got = []
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader = ChaoxingReader(settings, got.append, store=Store()); Client.reader = reader; reader._run()
        self.assertEqual(Client.made, 2)  # 读数一次 + 失败后的登录复核一次
        self.assertFalse(settings.chaoxing_status["expired"])
        self.assertFalse(settings.chaoxing_status["ok"])
        self.assertIn("未标记为过期", settings.chaoxing_status["last_error"])

    def test_expired_only_when_login_probe_also_fails(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace
        class Client:
            def __init__(self, *args, **kwargs): pass
            def validate_login(self): raise ChaoxingAuthError("Chaoxing login session is invalid or expired")
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1)
        reader = ChaoxingReader(settings, lambda record: None)
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader._record_failure(ChaoxingAuthError("Chaoxing login session is invalid or expired"))
        self.assertTrue(settings.chaoxing_status["expired"])
        self.assertEqual(settings.chaoxing_status["last_error"], "Cookie 已过期（需重新登录）")

    def test_login_probe_network_error_keeps_previous_status(self):
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingError, ChaoxingReader
        from types import SimpleNamespace
        class Client:
            def __init__(self, *args, **kwargs): pass
            def validate_login(self): raise ChaoxingError("Chaoxing request failed")
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1)
        reader = ChaoxingReader(settings, lambda record: None)
        with patch("qq_live_digest.chaoxing_receiver.ChaoxingClient", Client):
            reader._record_failure(ChaoxingAuthError("Chaoxing login session is invalid or expired"))
        self.assertFalse(settings.chaoxing_status["expired"])  # 复核不可用时不凭空判过期
        self.assertIn("学习通登录复核失败", settings.chaoxing_status["last_error"])

    # ---- issue #1：校级泛雅子站账号（i.chaoxing.com 302 到 i.mooc.<校名>.edu.cn/base）----
    @staticmethod
    def _resp(body, url, status=200):
        class Response:
            def __init__(self):
                self._body = body.encode("utf-8") if isinstance(body, str) else body
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self): return self._body
            def getcode(self): return status
            def geturl(self): return url
        return Response()

    def _sso_opener(self, sso_body, page_body, page_url="https://i.mooc.xtu.edu.cn/base"):
        def opener(request, timeout=None):
            url = request.full_url
            if "userLogin4Uname.do" in url:
                if isinstance(sso_body, Exception):
                    raise sso_body
                return self._resp(sso_body, url)
            return self._resp(page_body, page_url)
        return opener

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_university_subsite_redirect_with_sso_result_one_is_valid(self, opener):
        """泛雅子站首页带 fanyalogin/请登录 不影响判活：SSO result=1 就是有效。"""
        opener.side_effect = self._sso_opener(
            '{"result": 1, "msg": {"name": "张三", "phone": "138****0000"}}',
            "<html>fanyalogin 请登录</html>",
        )
        ChaoxingClient("COOKIE").validate_login()  # 不抛异常即有效

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_university_subsite_account_is_not_marked_expired(self, opener):
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace
        opener.side_effect = self._sso_opener(
            '{"result": 1, "msg": {"name": "张三"}}',
            "<html>fanyalogin 请登录</html>",
        )
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1)
        reader = ChaoxingReader(settings, lambda record: None)
        reader._record_failure(ChaoxingAuthError("Chaoxing login session is invalid or expired"))
        self.assertFalse(settings.chaoxing_status["expired"])
        self.assertIn("未标记为过期", settings.chaoxing_status["last_error"])

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_sso_result_not_one_is_expired(self, opener):
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace
        opener.side_effect = self._sso_opener('{"result": 0, "msg": "未登录"}', "<html>ok</html>")
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1)
        reader = ChaoxingReader(settings, lambda record: None)
        reader._record_failure(ChaoxingAuthError("Chaoxing login session is invalid or expired"))
        self.assertTrue(settings.chaoxing_status["expired"])
        self.assertEqual(settings.chaoxing_status["last_error"], "Cookie 已过期（需重新登录）")

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_sso_login_page_falls_back_to_home_probe(self, opener):
        """SSO 被 302 到登录页（不是 JSON）时退回首页探测；首页也是登录页 → 判过期。"""
        opener.side_effect = self._sso_opener(
            "<html>请登录</html>",
            "<html>请登录</html>",
            page_url="https://passport2.chaoxing.com/login",
        )
        from qq_live_digest.chaoxing_receiver import ChaoxingAuthError, ChaoxingReader
        from types import SimpleNamespace
        settings = SimpleNamespace(chaoxing_cookie="COOKIE", http_timeout=1)
        reader = ChaoxingReader(settings, lambda record: None)
        reader._record_failure(ChaoxingAuthError("Chaoxing login session is invalid or expired"))
        self.assertTrue(settings.chaoxing_status["expired"])

    @patch("qq_live_digest.chaoxing_receiver.urlopen")
    def test_sso_network_error_falls_back_without_false_expiry(self, opener):
        """SSO 断网时退回首页探测；首页是泛雅子站且不含登录标记 → 仍然有效。"""
        opener.side_effect = self._sso_opener(
            URLError("sso unreachable"),
            "<html>我的课程</html>",
        )
        ChaoxingClient("COOKIE").validate_login()

    def test_work_signatures_accept_attribute_orders(self):
        c = ChaoxingClient("x")
        self.assertEqual(c.extract_work_signatures('<input id="workEnc" value="w"><input value="o" id="openc"><input name="enc" value="e">'), {"workEnc": "w", "openc": "o", "enc": "e"})

    def test_work_list_and_permission_error(self):
        text = '<li onclick="goTask(this);" data="https%3A%2F%2Fexample.com%2Fwork%3FworkId%3Dw1"><p class="overHidden2 fl">作业</p><p class="status fl">未提交</p>'
        self.assertEqual(ChaoxingClient.parse_work_list(text)[0]["url"], "https://example.com/work?workId=w1")
        with self.assertRaises(ChaoxingError): ChaoxingClient.parse_work_list("无权限的操作")

    def test_work_detail_window(self):
        self.assertEqual(ChaoxingClient.parse_work_window("<div>作答时间：10-08 11:40 至 10-08 23:59</div>"), ("10-08 11:40", "10-08 23:59"))

    def test_work_record_contract(self):
        record = to_record("c1", "数学", "work:c1:w1", "作业", "未提交", event="WORK_DUE")
        self.assertEqual(record["msg_id"], "chaoxing:work:c1:w1")
        self.assertEqual(record["event"], "WORK_DUE")

    def test_deadline_overview_joins_relative_work_url(self):
        client = ChaoxingClient("x")

        class Stub(ChaoxingClient):
            def work_list(self, course_id=None):
                return [{"title": "作业一", "url": "/mooc-ans/work/1"}]

            def work_detail(self, url):
                self.seen = url
                return "<div>截止时间 07-01 18:51</div>"

        stub = Stub("x")
        rows = stub.deadline_overview()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "作业一")
        self.assertTrue(rows[0]["deadline"].endswith("07-01 18:51"))
        self.assertEqual(stub.seen, "https://mooc1.chaoxing.com/mooc-ans/work/1")


if __name__ == "__main__":
    unittest.main()
