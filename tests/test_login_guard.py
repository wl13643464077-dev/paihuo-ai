"""登录防爆破：按用户名全局渐进限速 + 用户不存在时的假哈希校验。"""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from app import auth, loginguard


class UserFailureThrottleCase(unittest.TestCase):
    def test_first_failures_are_free_then_delay_doubles_and_caps(self):
        guard = loginguard.UserFailureThrottle(
            free_fails=3, base_delay=2, max_delay=10, window=3600)
        now = 1_000_000.0
        for _ in range(2):
            guard.record_failure("boss", now)
            self.assertEqual(0, guard.retry_after("boss", now))
        self.assertEqual(0, guard.retry_after("boss", now))
        self.assertEqual(2, guard.record_failure("boss", now))   # 第 3 次
        self.assertAlmostEqual(2, guard.retry_after("boss", now))
        self.assertAlmostEqual(1, guard.retry_after("boss", now + 1))
        self.assertEqual(0, guard.retry_after("boss", now + 2))
        self.assertEqual(4, guard.record_failure("boss", now + 2))
        self.assertEqual(8, guard.record_failure("boss", now + 6))
        self.assertEqual(10, guard.record_failure("boss", now + 14))   # 封顶
        self.assertEqual(10, guard.record_failure("boss", now + 24))
        # 其他账号不受影响
        self.assertEqual(0, guard.retry_after("other", now + 24))

    def test_counter_ignores_ip_rotation_and_clears_on_success(self):
        guard = loginguard.UserFailureThrottle(free_fails=2, base_delay=60)
        now = 5_000.0
        # 攻击者每次换 IP，但计数只看用户名
        for i in range(3):
            guard.record_failure("owner1", now + i)
        self.assertGreater(guard.retry_after("owner1", now + 3), 0)
        guard.clear("owner1")
        self.assertEqual(0, guard.retry_after("owner1", now + 3))
        self.assertEqual(0, guard.failures("owner1", now + 3))

    def test_counter_expires_after_quiet_window(self):
        guard = loginguard.UserFailureThrottle(
            free_fails=1, base_delay=5, window=100)
        guard.record_failure("u", 0)
        guard.record_failure("u", 1)
        self.assertEqual(2, guard.failures("u", 50))
        self.assertEqual(0, guard.failures("u", 101 + 1))
        self.assertEqual(0, guard.retry_after("u", 102))

    def test_cache_is_bounded_and_keeps_throttled_account(self):
        guard = loginguard.UserFailureThrottle(
            free_fails=1, base_delay=600, cache_max=5)
        now = 10_000.0
        for _ in range(3):
            guard.record_failure("victim", now)
        for i in range(50):
            guard.record_failure(f"noise-{i}", now + 1)
        self.assertLessEqual(len(guard), 5)
        self.assertGreater(guard.retry_after("victim", now + 2), 0)

    def test_concurrent_failures_are_all_counted(self):
        guard = loginguard.UserFailureThrottle(free_fails=1000)
        barrier = threading.Barrier(20)

        def fail(_):
            barrier.wait()
            guard.record_failure("target", 1.0)

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(fail, range(20)))
        self.assertEqual(20, guard.failures("target", 2.0))


class DummyHashCase(unittest.TestCase):
    def test_missing_user_still_runs_pbkdf2_check(self):
        real_check = auth.check_pw
        seen = []

        def spy(pw, stored):
            seen.append(stored)
            return real_check(pw, stored)

        with patch.object(auth, "check_pw", side_effect=spy):
            self.assertFalse(auth.verify_login_password("guess-2026!", None))
            self.assertFalse(auth.verify_login_password("guess-2026!", ""))
        self.assertEqual(2, len(seen))
        self.assertTrue(all(s.startswith("pbkdf2:") for s in seen))
        self.assertEqual(seen[0], auth.dummy_password_hash())

    def test_real_hash_still_verifies(self):
        stored = auth.hash_pw("correct-horse-2026")
        self.assertTrue(auth.verify_login_password("correct-horse-2026", stored))
        self.assertFalse(auth.verify_login_password("wrong", stored))
        self.assertFalse(auth.verify_login_password(None, stored))


if __name__ == "__main__":
    unittest.main()
