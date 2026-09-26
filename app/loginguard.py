"""登录防爆破：按用户名的全局失败计数 + 渐进延迟（内存态，重启即清）。

main.py 里原有的「IP+用户名 10 次锁 15 分钟」只能挡单个 IP；攻击者换 IP
轮流试同一个账号时，它一次都不会触发。这里再加一层只看用户名的计数：

- 前 ``free_fails`` 次失败不限速（老板手滑输错几次不受影响）；
- 之后每次失败，下一次允许尝试的等待时间翻倍（2s、4s、8s…），封顶
  ``max_delay`` 秒；等待期内的尝试直接 429，不再验密码；
- 最后一次失败满 ``window`` 秒没有新失败，计数自然清零；登录成功立即清零。

用户名是攻击者可控的，缓存键先做 sha256，缓存数量有上限；淘汰时优先保留
仍在等待期的账号，避免攻击者用海量随机用户名把受保护账号挤出缓存。
"""
import hashlib
import threading
import time


def _key(username: str) -> str:
    return hashlib.sha256(str(username or "").encode("utf-8", "replace")).hexdigest()


class UserFailureThrottle:
    def __init__(self, free_fails: int = 5, base_delay: float = 2.0,
                 max_delay: float = 900.0, window: float = 3600.0,
                 cache_max: int = 5000):
        self.free_fails = int(free_fails)
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.window = float(window)
        self.cache_max = int(cache_max)
        self._state: dict = {}   # key -> (失败次数, 最近一次失败时间)
        self._lock = threading.Lock()

    def _delay_for(self, fails: int) -> float:
        over = fails - self.free_fails
        if over < 0:
            return 0.0
        return min(self.max_delay, self.base_delay * (2 ** min(over, 30)))

    def _live(self, key: str, now: float):
        fails, last = self._state.get(key, (0, 0.0))
        if fails and now - last >= self.window:
            self._state.pop(key, None)
            return 0, 0.0
        return fails, last

    def retry_after(self, username: str, now=None) -> float:
        """还要等多少秒才允许再试；0 表示现在可以验密码。"""
        now = time.time() if now is None else now
        with self._lock:
            fails, last = self._live(_key(username), now)
            wait = last + self._delay_for(fails) - now if fails else 0.0
            return max(0.0, wait)

    def failures(self, username: str, now=None) -> int:
        now = time.time() if now is None else now
        with self._lock:
            return self._live(_key(username), now)[0]

    def record_failure(self, username: str, now=None) -> float:
        """记一次失败，返回下一次需要等待的秒数。"""
        now = time.time() if now is None else now
        key = _key(username)
        with self._lock:
            fails, _ = self._live(key, now)
            fails += 1
            self._state.pop(key, None)   # 重新插入到末尾，淘汰按最久未失败
            self._state[key] = (fails, now)
            self._trim(now)
            return self._delay_for(fails)

    def clear(self, username: str):
        with self._lock:
            self._state.pop(_key(username), None)

    def reset(self):
        with self._lock:
            self._state.clear()

    def __len__(self):
        return len(self._state)

    def _trim(self, now: float):
        limit = max(1, self.cache_max)
        if len(self._state) <= limit:
            return
        for key, (_, last) in list(self._state.items()):
            if now - last >= self.window:
                self._state.pop(key, None)
        if len(self._state) <= limit:
            return
        # 先淘汰还没进入限速的「噪声」账号，再按最久未失败淘汰。
        for key, (fails, _) in list(self._state.items()):
            if len(self._state) <= limit:
                break
            if fails <= self.free_fails:
                self._state.pop(key, None)
        while len(self._state) > limit:
            self._state.pop(next(iter(self._state)))
