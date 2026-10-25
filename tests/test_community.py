"""社区在线数的契约：key 要可复现，网络出任何事都不能冒到调用方。"""

from __future__ import annotations

import io
import unittest
import urllib.error
from unittest import mock

from qq_live_digest import community


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


class FetchCountsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
