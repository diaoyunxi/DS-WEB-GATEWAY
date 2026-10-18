"""①服务端基础设施：pending_call 管理（tool_call 登记与 /result 交付）。

规则（见方案 §3.1 / §6）：
- 登记按 code + name + 参数哈希去重：模型重复抓取只返回同一 call_id，不重复下发；
- /result 绑定 call_id，一次性消费，过期即焚；
- 结果由 ② 经 WS tool_result 消息唤醒。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class PendingCall:
    call_id: str
    code: str
    name: str
    arguments: dict
    arg_hash: str
    created_at: float
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: str | None = None
    consumed: bool = False


class PendingCallRegistry:
    def __init__(self, ttl: int = 600) -> None:
        self._ttl = ttl
        self._calls: dict[str, PendingCall] = {}
        self._by_hash: dict[str, str] = {}  # arg_hash -> call_id
        self._lock = asyncio.Lock()

    @staticmethod
    def _hash(code: str, name: str, arguments: dict) -> str:
        raw = f"{code}|{name}|{json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    async def register(self, code: str, name: str, arguments: dict) -> tuple[PendingCall, bool]:
        """登记工具调用；返回 (call, 是否首次登记)。重复抓取返回同一 call。"""
        arg_hash = self._hash(code, name, arguments)
        async with self._lock:
            existing_id = self._by_hash.get(arg_hash)
            if existing_id is not None and existing_id in self._calls:
                return self._calls[existing_id], False
            call_id = "call_" + secrets.token_urlsafe(9)
            call = PendingCall(
                call_id=call_id,
                code=code,
                name=name,
                arguments=arguments,
                arg_hash=arg_hash,
                created_at=time.time(),
            )
            self._calls[call_id] = call
            self._by_hash[arg_hash] = call_id
            return call, True

    def get(self, call_id: str) -> PendingCall | None:
        return self._calls.get(call_id)

    async def set_result(self, call_id: str, content: str) -> bool:
        """②回传工具结果：唤醒挂起中的 /result。已消费/不存在返回 False。"""
        async with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.consumed:
                return False
            call.result = content
            call.event.set()
            return True

    async def wait_result(self, call_id: str, timeout: float) -> str | None:
        """等待结果就绪（/result 短挂起）；超时返回 None。"""
        call = self._calls.get(call_id)
        if call is None:
            return None
        try:
            await asyncio.wait_for(call.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return call.result

    async def consume(self, call_id: str) -> str | None:
        """一次性消费：返回结果并标记已消费；重复消费返回 None。"""
        async with self._lock:
            call = self._calls.get(call_id)
            if call is None or call.consumed:
                return None
            call.consumed = True
            return call.result

    def sweep(self) -> int:
        now = time.time()
        expired = [
            cid
            for cid, call in self._calls.items()
            if now - call.created_at > self._ttl and not call.event.is_set()
        ]
        for cid in expired:
            call = self._calls.pop(cid)
            self._by_hash.pop(call.arg_hash, None)
        return len(expired)

    def stats(self) -> dict:
        return {
            "total": len(self._calls),
            "ready": sum(1 for c in self._calls.values() if c.event.is_set()),
            "consumed": sum(1 for c in self._calls.values() if c.consumed),
            "ttl": self._ttl,
        }
