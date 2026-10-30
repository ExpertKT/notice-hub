"""社区在线数的契约：key 要可复现，网络出任何事都不能冒到调用方。"""

from __future__ import annotations

import io
import os
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from qq_live_digest import community, webapp


class _Response(io.BytesIO):
    """假响应：够 urlopen(...) as response + response.read() 用。"""

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _response(body: str) -> _Response:
    return _Response(body.encode("utf-8"))


class KeyTests(unittest.TestCase):
    def test_bucket_key_is_fixed_for_a_fixed_timestamp(self) -> None:
        # 1700000000 落在 1700000000 // 300 == 5666666 这个桶里
        self.assertEqual(community.bucket_key(1700000000), "online-5666666")

    def test_bucket_key_rolls_over_every_five_minutes(self) -> None:
        self.assertEqual(community.bucket_key(299), "online-0")
        self.assertEqual(community.bucket_key(300), "online-1")

    def test_day_key_uses_utc_date(self) -> None:
        # 1700000000 = 2023-11-14 22:13:20 UTC
        self.assertEqual(community.day_key(1700000000), "today-20231114")
        self.assertEqual(community.day_key(0), "today-19700101")


class PingTests(unittest.TestCase):
    def setUp(self) -> None:
        community._PINGED.clear()
        self.addCleanup(community._PINGED.clear)
        # 这组用例验的是计数机制本身，显式打开开关；默认关的行为由 TelemetryGateTests 覆盖。
        patcher = mock.patch.object(community, "telemetry_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ping_sends_each_key_only_once(self) -> None:
        with mock.patch.object(community.urllib.request, "urlopen", return_value=_response("{}")) as urlopen:
            community.ping(1700000000)
            community.ping(1700000000)
            community.ping(1700000001)  # 同一个 5 分钟桶、同一天：仍然是那两个 key
        self.assertEqual(urlopen.call_count, 2)
        urls = [call.args[0] for call in urlopen.call_args_list]
        self.assertTrue(urls[0].endswith("/hit/notice-hub/online-5666666"), urls[0])
        self.assertTrue(urls[1].endswith("/hit/notice-hub/today-20231114"), urls[1])

    def test_ping_next_bucket_sends_again(self) -> None:
        with mock.patch.object(community.urllib.request, "urlopen", return_value=_response("{}")) as urlopen:
            community.ping(0)
            community.ping(300)  # 换桶了，online 要再报一次；today 还在同一天
        self.assertEqual(urlopen.call_count, 3)

    def test_ping_swallows_network_errors(self) -> None:
        boom = urllib.error.URLError("no network")
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=boom):
            community.ping(1700000000)  # 不抛就算过
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=TimeoutError("slow")):
            community.ping(1700000001)

    def test_failed_ping_is_not_retried(self) -> None:
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=OSError("down")) as urlopen:
            community.ping(1700000000)
            community.ping(1700000000)
        self.assertEqual(urlopen.call_count, 2, "失败也不该重试，否则每次开页面都要等超时")

    def test_ping_forwards_custom_timeout(self) -> None:
        # 页面请求里只给 1 秒：第三方慢的时候不能把首屏拖住
        with mock.patch.object(
            community.urllib.request, "urlopen", side_effect=[_response('{"value": 1}'), _response('{"value": 1}')]
        ) as urlopen:
            community.ping(1700000000, timeout=1.0)
        self.assertEqual([call.kwargs.get("timeout") for call in urlopen.call_args_list], [1.0, 1.0])


class FetchCountsTests(unittest.TestCase):
    def setUp(self) -> None:
        community._COUNTS_CACHE.clear()
        patcher = mock.patch.object(community, "telemetry_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        community._COUNTS_CACHE.clear()

    def test_parses_both_counters(self) -> None:
        # 两次 GET 各要一个新响应：假响应读完就空了，复用会把第二次读成空串
        with mock.patch.object(
            community.urllib.request, "urlopen", side_effect=[_response('{"value": 42}'), _response('{"value": 42}')]
        ):
            counts = community.fetch_counts()
        self.assertEqual(counts, {"online": 42, "today": 42})

    def test_network_failure_yields_none_fields(self) -> None:
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            counts = community.fetch_counts()
        self.assertEqual(counts, {"online": None, "today": None})

    def test_garbage_body_yields_none_fields(self) -> None:
        with mock.patch.object(community.urllib.request, "urlopen", return_value=_response("<html>502</html>")):
            counts = community.fetch_counts()
        self.assertEqual(counts, {"online": None, "today": None})

    def test_http_error_yields_none_fields(self) -> None:
        error = urllib.error.HTTPError("https://example", 503, "busy", {}, None)  # type: ignore[arg-type]
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=error):
            counts = community.fetch_counts()
        self.assertEqual(counts, {"online": None, "today": None})

    def test_result_is_cached_for_sixty_seconds(self) -> None:
        # 页面每次都拉这个接口：60 秒内的重复调用不打计数端点，别把境外服务拖死
        with mock.patch.object(
            community.urllib.request, "urlopen", side_effect=[_response('{"value": 7}'), _response('{"value": 9}')]
        ) as urlopen:
            first = community.fetch_counts()
            second = community.fetch_counts()
        self.assertEqual(first, {"online": 7, "today": 9})
        self.assertEqual(second, first, "缓存期内返回同一份结果")
        self.assertEqual(urlopen.call_count, 2, "第二次调用不再发请求")

    def test_all_none_is_not_cached(self) -> None:
        # 拿不到不缓存：下一轮调用再试，别把「暂不可用」钉死 60 秒
        with mock.patch.object(community.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            community.fetch_counts()
        with mock.patch.object(
            community.urllib.request, "urlopen", side_effect=[_response('{"value": 5}'), _response('{"value": 5}')]
        ) as urlopen:
            counts = community.fetch_counts()
        self.assertEqual(counts, {"online": 5, "today": 5})
        self.assertEqual(urlopen.call_count, 2)

    def test_missing_online_is_not_cached_even_when_today_works(self) -> None:
        # 只有 today 拿到、online 没拿到时也不能缓存：否则 null 会被锁 60 秒，
        # 页面整段时间都显示「暂不可用」，而下一轮本来就能拿到在线数。
        community._COUNTS_CACHE.clear()
        with mock.patch.object(
            community.urllib.request,
            "urlopen",
            side_effect=[
                urllib.error.URLError("bucket missing"),
                _response('{"value": 9}'),
                _response('{"value": 4}'),
                _response('{"value": 9}'),
            ],
        ) as urlopen:
            first = community.fetch_counts()
            second = community.fetch_counts()
        self.assertEqual(first, {"online": None, "today": 9})
        self.assertEqual(second, {"online": 4, "today": 9}, "第二轮必须真的再问一次，不能吃缓存")
        self.assertEqual(urlopen.call_count, 4)

    def test_two_requests_share_a_short_timeout(self) -> None:
        # 两次串行 GET 各 2s：最坏 4s，比原来的 10s 好得多
        with mock.patch.object(
            community.urllib.request, "urlopen", side_effect=[_response('{"value": 1}'), _response('{"value": 1}')]
        ) as urlopen:
            community.fetch_counts()
        self.assertEqual([call.kwargs.get("timeout") for call in urlopen.call_args_list], [2.0, 2.0])


class CountsRouteTests(unittest.TestCase):
    """接口层：有数就回数，没数也回 200（页面据此显示「暂不可用」，不是错误页）。"""

    def setUp(self) -> None:
        patcher = mock.patch.object(community, "telemetry_enabled", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _payload(self) -> dict:
        handler = object.__new__(webapp._Handler)
        return handler._community_counts_payload()

    def test_route_returns_numbers(self) -> None:
        with mock.patch.object(
            webapp.community, "fetch_counts", return_value={"online": 7, "today": 9}
        ) as fetch:
            payload = self._payload()
        self.assertEqual(payload, {"ok": True, "counts": {"online": 7, "today": 9}})
        fetch.assert_called_once_with()

    def test_route_survives_network_failure(self) -> None:
        with mock.patch.object(
            webapp.community, "fetch_counts", return_value={"online": None, "today": None}
        ):
            payload = self._payload()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["counts"], {"online": None, "today": None})

    def test_real_fetch_counts_never_raises_on_dead_endpoint(self) -> None:
        # 不 mock：把 BASE_URL 指到本机一个必然拒绝的端口，确认整条链路是 None + 不抛
        community._COUNTS_CACHE.clear()  # 别让前面用例的缓存结果漏进来
        with mock.patch.object(community, "BASE_URL", "http://127.0.0.1:1"):
            counts = community.fetch_counts(timeout=0.2)
        self.assertEqual(counts, {"online": None, "today": None})

    def test_counts_route_triggers_ping_with_short_timeout(self) -> None:
        # 页面一打开就会拉这个接口，这里顺带上报在线；必须放后台线程且给足 3 秒：
        # abacus 端点实测要 0.9–1.3 秒才回，给 1 秒会必然超时、上报永远写不进去。
        started: list[dict] = []

        class _FakeThread:
            def __init__(self, target, kwargs=None, **rest):
                started.append({"target": target, "kwargs": kwargs or {}})

            def start(self) -> None:
                return None

        handler = object.__new__(webapp._Handler)
        handler.server = mock.Mock()
        handler.server.settings.community_telemetry = True
        handler._json = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(webapp.threading, "Thread", _FakeThread):
            handler._route_get_api_community_counts("/api/community/counts")
        self.assertEqual(len(started), 1)
        self.assertIs(started[0]["target"], community.ping)
        self.assertEqual(started[0]["kwargs"], {"timeout": 3.0})

    def test_counts_route_does_not_ping_when_telemetry_is_off(self) -> None:
        """遥测关掉时：调用点直接跳过，线程都不该建。"""
        started: list[dict] = []

        class _FakeThread:
            def __init__(self, target, kwargs=None, **rest):
                started.append({"target": target, "kwargs": kwargs or {}})

            def start(self) -> None:
                return None

        handler = object.__new__(webapp._Handler)
        handler.server = mock.Mock()
        handler.server.settings.community_telemetry = False
        handler._json = mock.Mock()  # type: ignore[method-assign]
        with mock.patch.object(webapp.threading, "Thread", _FakeThread):
            handler._route_get_api_community_counts("/api/community/counts")
        self.assertEqual(started, [])
        handler._json.assert_called_once()

    def test_onboarding_route_no_longer_pings(self) -> None:
        # ping 原先挂在 /api/onboarding 上，但首屏早就不调这个接口了 —— 挂错地方等于没上报。
        handler = object.__new__(webapp._Handler)
        handler.server = mock.Mock()
        handler.server.store.meta_get.return_value = ""
        handler._json = mock.Mock()  # type: ignore[method-assign]

        def _boom(*_a, **_k):
            raise AssertionError("/api/onboarding 不该再触发 community.ping")

        with mock.patch.object(webapp.threading, "Thread", _boom):
            handler._route_get_api_onboarding("/api/onboarding")
        handler._json.assert_called_once()


class TelemetryGateTests(unittest.TestCase):
    """开关关时：一个请求都不发、不写缓存、返回值契约不变。"""

    def test_disabled_ping_sends_nothing(self) -> None:
        community._PINGED.clear()
        with mock.patch.object(community.urllib.request, "urlopen") as urlopen:
            community.ping(1700000000, enabled=False)
        self.assertEqual(urlopen.call_count, 0)

    def test_disabled_fetch_counts_is_none_and_not_cached(self) -> None:
        community._COUNTS_CACHE.clear()
        with mock.patch.object(community.urllib.request, "urlopen") as urlopen:
            counts = community.fetch_counts(enabled=False)
        self.assertEqual(counts, {"online": None, "today": None})
        self.assertEqual(urlopen.call_count, 0)
        self.assertEqual(community._COUNTS_CACHE, {}, "关了就不能把 null 写进缓存")

    def test_switch_is_read_live_from_env(self) -> None:
        # 设置页保存时会把新值写进 os.environ（settings_api 里同时写 .env 与环境），
        # 所以这里每次现读：关掉立刻生效，不需要重启。
        with mock.patch.dict(os.environ, {community.TELEMETRY_ENV: "1"}):
            self.assertTrue(community.telemetry_enabled())
        with mock.patch.dict(os.environ, {community.TELEMETRY_ENV: "0"}):
            self.assertFalse(community.telemetry_enabled())

    def test_switch_defaults_off_when_unset(self) -> None:
        env = {key: value for key, value in os.environ.items() if key != community.TELEMETRY_ENV}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(community.telemetry_enabled())

    def test_disabled_is_logged_only_once(self) -> None:
        community._DISABLED_LOGGED = False
        self.addCleanup(setattr, community, "_DISABLED_LOGGED", False)
        with mock.patch.object(community.urllib.request, "urlopen") as urlopen:
            with self.assertLogs("qq_live_digest.community", level="INFO") as logs:
                community.ping(1700000000, enabled=False)
                community.fetch_counts(enabled=False)
                community.fetch_counts(enabled=False)
        self.assertEqual(urlopen.call_count, 0)
        lines = [line for line in logs.output if "社区计数已关闭" in line]
        self.assertEqual(len(lines), 1, "关闭态只该记一次，别每条消息刷一行")


class CommunityFrontendTests(unittest.TestCase):
    """前端那一半：取数要带 token，且必须等 api() 定义好再跑。"""

    @staticmethod
    def _guide_source() -> str:
        path = Path(webapp.__file__).resolve().parent / "web_static" / "js" / "guide.js"
        return path.read_text(encoding="utf-8")

    def test_counts_fetch_goes_through_api_helper(self) -> None:
        # 直连 fetch 不带 X-Token → 401 → 那一行永远是「暂不可用」。
        source = self._guide_source()
        start = source.index("function initCommunityCounts()")
        body = source[start:]
        self.assertIn("api('/api/community/counts'", body)
        self.assertNotIn("fetch('/api/community/counts'", body)

    def test_counts_init_waits_for_dom_ready(self) -> None:
        # guide.js 在 api() 之前加载；同步调用会在 api 未定义时静默退出。
        source = self._guide_source()
        tail = source[source.index("function initCommunityCounts()"):]
        self.assertIn("DOMContentLoaded", tail)


if __name__ == "__main__":
    unittest.main()
