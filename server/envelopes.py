"""①服务端基础设施：answer 分片重组缓冲。

规则（见方案 §3.2）：
- 单条 content ≤ answer_shard_max_chars，超长必须分片（seq/total）；
- ①做乱序/去重/重组，缓冲 TTL 5 分钟；
- 同一 code 收到 seq=1 视为开启新的一组（支持连续多个 answer）。
"""
from __future__ import annotations

import asyncio
import time

from common.schemas import AnswerEnvelope


class ShardBuffer:
    def __init__(self, ttl: int = 300) -> None:
        self._ttl = ttl
        self._groups: dict[str, dict] = {}  # code -> {"total": int, "parts": {seq: str}, "expire": float}
        self._lock = asyncio.Lock()

    async def add(self, code: str, env: AnswerEnvelope) -> str | None:
        """加入一片；返回完整内容（已重组）或 None（尚缺分片）。

        乱序容错：seq=1 晚到时（此前已收到其它分片）不重启分组；
        仅当分组已含 seq=1（重复开启）或 total 变化时才开启新组。
        """
        async with self._lock:
            group = self._groups.get(code)
            restart = (
                group is None
                or group["total"] != env.total
                or (env.seq == 1 and 1 in group["parts"])
            )
            if restart:
                group = {"total": env.total, "parts": {}, "expire": time.time() + self._ttl}
                self._groups[code] = group
            group["parts"][env.seq] = env.content  # 同 seq 重复抓取 → 覆盖去重
            group["expire"] = time.time() + self._ttl
            if len(group["parts"]) == group["total"]:
                content = "".join(group["parts"][i] for i in range(1, group["total"] + 1))
                del self._groups[code]
                return content
            return None

    async def progress(self, code: str) -> tuple[int, int]:
        async with self._lock:
            group = self._groups.get(code)
            if not group:
                return 0, 0
            return len(group["parts"]), group["total"]

    def sweep(self) -> int:
        now = time.time()
        expired = [c for c, g in self._groups.items() if g["expire"] < now]
        for code in expired:
            del self._groups[code]
        return len(expired)

    def stats(self) -> dict:
        return {"codes": len(self._groups), "ttl": self._ttl}
