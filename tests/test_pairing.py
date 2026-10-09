from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from qq_live_digest import pairing  # noqa: E402


class PairingTest(unittest.TestCase):
    """手机 App 配对：手机显示 6 位码，用户在电脑上点「允许」才发令牌。"""

    def setUp(self) -> None:
        pairing.reset()
        self.addCleanup(pairing.reset)

    def test_start_returns_a_six_digit_code_and_the_phone_gets_the_token_only_after_approval(self) -> None:
        started = pairing.start(device="安卓手机（192.168.1.5）", source="192.168.1.5", now=1000.0)
        code = started["code"]
        secret = started["secret"]
        self.assertEqual(len(code), 6)
        self.assertTrue(code.isdigit())
        self.assertEqual(started["expires_in"], pairing.PAIR_TTL_SECONDS)
        # 电脑端能看到「谁在等」，好让用户核对是不是自己的手机
        waiting = pairing.pending(now=1002.0)
        self.assertEqual([row["code"] for row in waiting], [code])
        self.assertEqual(waiting[0]["device"], "安卓手机（192.168.1.5）")
        self.assertEqual(waiting[0]["waiting_seconds"], 2)
        # 还没批准：手机只能看到「还没好」，拿不到令牌
        self.assertEqual(pairing.status(code, secret, now=1002.0)["status"], "pending")
        # 别人的 secret 问不出任何东西
        self.assertIsNone(pairing.status(code, "wrong-secret", now=1002.0))
        self.assertIsNone(pairing.status("000000", secret, now=1002.0))
        self.assertTrue(pairing.decide(code, True, now=1003.0)["ok"])
        after = pairing.status(code, secret, now=1003.0)
        self.assertEqual(after["status"], "approved")
        self.assertEqual(pairing.pending(now=1003.0), [])

    def test_deny_expiry_duplicate_decision_and_forget(self) -> None:
        started = pairing.start(source="10.0.0.9", now=2000.0)
        code = started["code"]
        self.assertTrue(pairing.decide(code, False, now=2001.0)["ok"])
        self.assertEqual(pairing.status(code, started["secret"], now=2001.0)["status"], "denied")
        # 已经处理过的码不能重复处理
        self.assertFalse(pairing.decide(code, True, now=2002.0)["ok"])
        # 过期的码也不行
        self.assertFalse(pairing.decide("123456", True, now=2002.0)["ok"])
        stale = pairing.start(source="10.0.0.10", now=3000.0)
        self.assertIsNone(pairing.status(stale["code"], stale["secret"], now=3000.0 + pairing.PAIR_TTL_SECONDS + 1))
        self.assertEqual(pairing.pending(now=3000.0 + pairing.PAIR_TTL_SECONDS + 1), [])
        pairing.forget(stale["code"])
        self.assertIsNone(pairing.status(stale["code"], stale["secret"], now=3000.0))

    def test_cooldown_and_pending_limit_protect_the_desktop(self) -> None:
        pairing.start(source="10.0.0.1", now=4000.0)
        with self.assertRaises(ValueError):
            pairing.start(source="10.0.0.1", now=4000.5)
        for index in range(pairing.MAX_PENDING - 1):
            pairing.start(source=f"10.0.0.{index + 2}", now=4005.0)
        self.assertEqual(len(pairing.pending(now=4006.0)), pairing.MAX_PENDING)
        with self.assertRaises(ValueError):
            pairing.start(source="10.0.0.99", now=4007.0)


if __name__ == "__main__":
    unittest.main()
