from __future__ import annotations

import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from noticehub.retry import (  # noqa: E402
    LLMRequestError,
    LLMResponseError,
    call_with_retries,
    is_transient_http,
    llm_should_retry,
)


def _scripted(*outcomes):
    """按顺序依次抛出或返回：前几次失败、第 N 次成功的调用方用它，省掉手写计数器。"""
    remaining = list(outcomes)
    counter = {"attempts": 0}

    def call():
        counter["attempts"] += 1
        current = remaining.pop(0) if remaining else outcomes[-1]
        if isinstance(current, Exception):
            raise current
        return current

    return call, counter


def _always_raising(error: Exception):
    """永远抛同一个错的函数，用来验「不重试」和「重试耗尽」。"""
    def call():
        raise error
    return call


def _run(target, **overrides):
    """默认参数固定成测试口径，单个用例只覆盖自己关心的那一两项。"""
    options = {"retries": 3, "backoff": 0.0, "should_retry": llm_should_retry, "label": "test"}
    options.update(overrides)
    return call_with_retries(target, **options)


class RetryTest(unittest.TestCase):
    def test_retries_transient_error_then_succeeds(self) -> None:
        flaky, attempts = _scripted(
            LLMRequestError("rate limited", status=429),
            LLMRequestError("rate limited", status=429),
            "ok",
        )
        result = _run(flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["attempts"], 3)

    def test_auth_error_is_not_retried(self) -> None:
        unauthorized, attempts = _scripted(LLMRequestError("bad key", status=401))
        with self.assertRaises(LLMRequestError):
            _run(unauthorized)
        self.assertEqual(attempts["attempts"], 1)

    def test_exhausted_retries_reraise_last_error(self) -> None:
        with self.assertRaises(LLMResponseError):
            _run(_always_raising(LLMResponseError("bad json")), retries=2)

    def test_http_error_classification(self) -> None:
        self.assertTrue(is_transient_http(429))
        self.assertTrue(is_transient_http(503))
        self.assertFalse(is_transient_http(401))
        unavailable = urllib.error.HTTPError("http://x", 503, "unavailable", {}, None)
        self.assertTrue(llm_should_retry(unavailable))
        forbidden = urllib.error.HTTPError("http://x", 403, "forbidden", {}, None)
        self.assertFalse(llm_should_retry(forbidden))
        self.assertTrue(llm_should_retry(json.JSONDecodeError("x", "y", 0)))

    def test_backoff_sleeps_between_attempts(self) -> None:
        flaky, _ = _scripted(LLMResponseError("bad json"), "ok")
        with mock.patch("noticehub.retry.time.sleep") as sleeper:
            _run(flaky, retries=2, backoff=1.5)
        self.assertEqual(sleeper.call_count, 1)
        self.assertAlmostEqual(sleeper.call_args[0][0], 1.5)
