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

from qq_live_digest.retry import (  # noqa: E402
    LLMRequestError,
    LLMResponseError,
    call_with_retries,
    is_transient_http,
    llm_should_retry,
)


class RetryTest(unittest.TestCase):
    def test_retries_transient_error_then_succeeds(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise LLMRequestError("rate limited", status=429)
            return "ok"

        result = call_with_retries(
            flaky,
            retries=3,
            backoff=0.0,
            should_retry=llm_should_retry,
            label="test",
        )
        self.assertEqual(result, "ok")
        self.assertEqual(calls["n"], 3)

    def test_auth_error_is_not_retried(self) -> None:
        calls = {"n": 0}

        def unauthorized() -> str:
            calls["n"] += 1
            raise LLMRequestError("bad key", status=401)

        with self.assertRaises(LLMRequestError):
            call_with_retries(
                unauthorized,
                retries=3,
                backoff=0.0,
                should_retry=llm_should_retry,
                label="test",
            )
        self.assertEqual(calls["n"], 1)

    def test_exhausted_retries_reraise_last_error(self) -> None:
        with self.assertRaises(LLMResponseError):
            call_with_retries(
                lambda: (_ for _ in ()).throw(LLMResponseError("bad json")),
                retries=2,
                backoff=0.0,
                should_retry=llm_should_retry,
                label="test",
            )

    def test_http_error_classification(self) -> None:
        self.assertTrue(is_transient_http(429))
        self.assertTrue(is_transient_http(503))
        self.assertFalse(is_transient_http(401))
        error = urllib.error.HTTPError("http://x", 503, "unavailable", {}, None)
        self.assertTrue(llm_should_retry(error))
        auth = urllib.error.HTTPError("http://x", 403, "forbidden", {}, None)
        self.assertFalse(llm_should_retry(auth))
        self.assertTrue(llm_should_retry(json.JSONDecodeError("x", "y", 0)))

    def test_backoff_sleeps_between_attempts(self) -> None:
        calls = {"n": 0}

        def flaky() -> str:
            calls["n"] += 1
            if calls["n"] < 2:
                raise LLMResponseError("bad json")
            return "ok"

        with mock.patch("qq_live_digest.retry.time.sleep") as sleeper:
            call_with_retries(
                flaky,
                retries=2,
                backoff=1.5,
                should_retry=llm_should_retry,
                label="test",
            )
        self.assertEqual(sleeper.call_count, 1)
        self.assertAlmostEqual(sleeper.call_args[0][0], 1.5)


if __name__ == "__main__":
    unittest.main()
