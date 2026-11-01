"""``noticehub.retry`` 的重试语义：哪些错重试、试几次、间隔多少、失败抛什么。"""

from __future__ import annotations

import json
import pathlib
import sys
import unittest
import urllib.error
from unittest import mock

_HERE = pathlib.Path(__file__).resolve()
_ROOT = _HERE.parents[1]
if str(_ROOT) not in sys.path:
    sys.path.append(str(_ROOT))

import noticehub.retry as retry  # noqa: E402

FAILED_STATUS = retry.LLMRequestError
BAD_BODY = retry.LLMResponseError


def sequence(*steps):
    """按脚本依次产生结果或抛错；用尽后一直重复最后一步。"""
    pending = list(steps)
    tally = {"calls": 0}

    def invoke():
        tally["calls"] += 1
        step = pending.pop(0) if pending else steps[-1]
        if isinstance(step, BaseException):
            raise step
        return step

    return invoke, tally


def stuck(error: BaseException):
    """永远抛同一个错：用来验「不该重试」和「试满就停」。"""

    def invoke():
        raise error

    return invoke


def attempt(target, **tweaks):
    """统一测试口径的调用参数，单个用例只覆盖自己那一两项。"""
    args = {"retries": 3, "backoff": 0.0, "should_retry": retry.llm_should_retry, "label": "probe"}
    args.update(tweaks)
    return retry.call_with_retries(target, **args)


class RetryTest(unittest.TestCase):
    def test_retries_transient_error_then_succeeds(self) -> None:
        flaky, tally = sequence(
            FAILED_STATUS("rate limited", status=429),
            FAILED_STATUS("rate limited", status=429),
            "done",
        )
        self.assertEqual(attempt(flaky), "done")
        self.assertEqual(tally["calls"], 3)

    def test_auth_error_is_not_retried(self) -> None:
        denied, tally = sequence(FAILED_STATUS("bad key", status=401))
        with self.assertRaises(FAILED_STATUS):
            attempt(denied)
        self.assertEqual(tally["calls"], 1)

    def test_exhausted_retries_reraise_last_error(self) -> None:
        with self.assertRaises(BAD_BODY):
            attempt(stuck(BAD_BODY("bad json")), retries=2)

    def test_http_error_classification(self) -> None:
        self.assertTrue(retry.is_transient_http(429))
        self.assertTrue(retry.is_transient_http(503))
        self.assertFalse(retry.is_transient_http(401))
        self.assertTrue(
            retry.llm_should_retry(urllib.error.HTTPError("http://probe", 503, "unavailable", {}, None))
        )
        self.assertFalse(
            retry.llm_should_retry(urllib.error.HTTPError("http://probe", 403, "forbidden", {}, None))
        )
        self.assertTrue(retry.llm_should_retry(json.JSONDecodeError("x", "y", 0)))

    def test_backoff_sleeps_between_attempts(self) -> None:
        flaky, _tally = sequence(BAD_BODY("bad json"), "done")
        with mock.patch.object(retry.time, "sleep") as sleeper:
            attempt(flaky, retries=2, backoff=1.5)
        self.assertEqual(sleeper.call_count, 1)
        self.assertAlmostEqual(sleeper.call_args[0][0], 1.5)
