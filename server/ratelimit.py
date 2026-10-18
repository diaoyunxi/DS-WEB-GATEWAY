"""①服务端基础设施：限流（全局 QPS + 每 code QPS，不按 IP——IP 池轮换会误伤）。"""
from __future__ import annotations

import time


class TokenBucket:
    def __init__(self, qps: float, burst: int) -> None:
        self._rate = max(qps, 0.01)
        self._capacity = max(float(burst), 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()

    def try_consume(self, amount: float = 1.0) -> bool:
        now = time.monotonic()
        self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
        self._updated = now
        if self._tokens >= amount:
            self._tokens -= amount
            return True
        return False


class RateLimiter:
    def __init__(self, global_qps: float, per_code_qps: float, burst: int) -> None:
        self._global = TokenBucket(global_qps, burst)
        self._per_code: dict[str, TokenBucket] = {}
        self._per_code_qps = per_code_qps
        self._burst = burst

    def _bucket_for(self, code: str) -> TokenBucket:
        bucket = self._per_code.get(code)
        if bucket is None:
            bucket = TokenBucket(self._per_code_qps, self._burst)
            self._per_code[code] = bucket
        return bucket

    def check(self, code: str) -> bool:
        if not self._global.try_consume():
            return False
        return self._bucket_for(code).try_consume()

    def stats(self) -> dict:
        return {
            "global_qps": self._global._rate,
            "per_code_qps": self._per_code_qps,
            "codes": len(self._per_code),
        }
