"""①服务端基础设施：审计日志与抓取遥测（内存环形缓冲，仅观测不拦截）。"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass


@dataclass
class FetchRecord:
    ts: float
    ip: str
    ua: str
    path: str
    code: str
    kind: str
    summary: str


class AuditLog:
    def __init__(self, capacity: int = 5000) -> None:
        self._records: deque[FetchRecord] = deque(maxlen=capacity)

    def record(self, ip: str, ua: str, path: str, code: str, kind: str, summary: str = "") -> None:
        self._records.append(
            FetchRecord(
                ts=time.time(),
                ip=ip,
                ua=ua[:200],
                path=path[:200],
                code=code,
                kind=kind,
                summary=summary[:200],
            )
        )

    def recent(self, n: int = 100, code: str | None = None) -> list[dict]:
        records = [r for r in self._records if code is None or r.code == code]
        return [asdict(r) for r in list(records)[-n:]]

    def ip_stats(self) -> dict[str, int]:
        stats: dict[str, int] = {}
        for r in self._records:
            stats[r.ip] = stats.get(r.ip, 0) + 1
        return dict(sorted(stats.items(), key=lambda kv: -kv[1]))

    def stats(self) -> dict:
        return {"total": len(self._records), "unique_ips": len(self.ip_stats())}
