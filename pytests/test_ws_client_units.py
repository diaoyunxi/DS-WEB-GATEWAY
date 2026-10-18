"""②Gateway WS 客户端单测：request_id 派发、断线fail等待方、连接生命周期。

运行：pytest pytests/test_ws_client_units.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from common.schemas import ChatAck, ErrorNotify, HelloAuth, PingMessage, PongMessage, ToolResultMessage
from gateway.ws_client import GatewayWSClient


def _client() -> GatewayWSClient:
    return GatewayWSClient(
        SimpleNamespace(resolved_ws_url="ws://127.0.0.1:1/{token}/ws", path_token="tok", ws_secret="s", ws_heartbeat=30)
    )


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 请求方 API

def test_register_unregister_and_waitlist():
    client = _client()
    queue = client.register("r1")
    assert client._waiters["r1"] is queue
    client.unregister("r1")
    assert "r1" not in client._waiters
    client.unregister("no-such")  # 幂等


def test_connected_property_and_wait_timeout():
    client = _client()
    assert not client.connected
    assert run(client.wait_connected(0.05)) is False
    client._connected.set()
    assert client.connected
    assert run(client.wait_connected(0.05)) is True


def test_send_without_connection_raises():
    client = _client()

    async def scenario():
        with pytest.raises(ConnectionError, match="未连接"):
            await client.send(PingMessage())

    run(scenario())


def test_stop_marks_closing_and_cancels_task():
    async def scenario():
        client = _client()
        await client.start()
        await client.stop()
        assert client._closing
        assert client._task.cancelled() or client._task.done()

    run(scenario())


# ---------------------------------------------------------------- 消息派发

def test_dispatch_routes_by_request_id():
    client = _client()
    queue = client.register("r1")
    client._dispatch(json.dumps({"type": "chat_ack", "request_id": "r1", "code": "c1"}))
    msg = queue.get_nowait()
    assert isinstance(msg, ChatAck) and msg.code == "c1"
    # 无等待方 → 静默丢弃（不抛异常）
    client._dispatch(json.dumps({"type": "error", "request_id": "ghost", "message": "x"}))


def test_dispatch_ignores_pong_and_bad_payloads(caplog):
    client = _client()
    client._dispatch(PongMessage().model_dump_json())  # pong 忽略
    client._dispatch("{不是JSON")  # 解析失败不崩
    client._dispatch(json.dumps({"type": "unknown-type", "request_id": "r"}))
    assert "解析失败" in caplog.text


def test_fail_waiters_notifies_all_pending():
    client = _client()
    q1 = client.register("r1")
    q2 = client.register("r2")
    client._fail_waiters("与①的 WS 连接中断，请稍后重试")
    for q in (q1, q2):
        msg = q.get_nowait()
        assert isinstance(msg, ErrorNotify)
        assert msg.message == "与①的 WS 连接中断，请稍后重试"


# ---------------------------------------------------------------- 连接循环（假 ws）

class _FakeWS:
    """假 websockets 连接：发送即记录，迭代产出预置消息。"""

    def __init__(self, incoming: list[str] | None = None, fail_after: int | None = None) -> None:
        self.sent: list[str] = []
        self.incoming = list(incoming or [])
        self.fail_after = fail_after
        self.closed = False

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            raise ConnectionError("boom")
        await asyncio.sleep(3600)


def test_run_loop_hello_dispatch_and_heartbeat():
    async def scenario():
        client = _client()
        client._settings.ws_heartbeat = 0.05
        fake = _FakeWS(
            incoming=[
                json.dumps({"type": "chat_ack", "request_id": "r1", "code": "c1"}),
                json.dumps({"type": "pong"}),
            ]
        )
        queue = client.register("r1")

        import gateway.ws_client as ws_client_mod

        original_connect = ws_client_mod.websockets.connect

        def fake_connect(url, **kwargs):
            return _AsyncCtx(fake)

        ws_client_mod.websockets.connect = fake_connect  # type: ignore[assignment]
        try:
            await client.start()
            assert await client.wait_connected(2.0)
            msg = await asyncio.wait_for(queue.get(), timeout=2.0)
            assert isinstance(msg, ChatAck)
            await asyncio.sleep(0.2)  # 覆盖心跳发送
            assert any('"type": "ping"' in s for s in fake.sent)
            assert fake.sent[0] == HelloAuth(secret="s").model_dump_json()
        finally:
            ws_client_mod.websockets.connect = original_connect  # type: ignore[assignment]
            await client.stop()

    run(scenario())


def test_run_loop_disconnect_fails_waiters():
    async def scenario():
        client = _client()
        fake = _FakeWS(fail_after=1)  # 收到心跳后连接失败

        import gateway.ws_client as ws_client_mod

        original_connect = ws_client_mod.websockets.connect

        def fake_connect(url, **kwargs):
            return _AsyncCtx(fake)

        ws_client_mod.websockets.connect = fake_connect  # type: ignore[assignment]
        try:
            client._settings.ws_heartbeat = 0.05
            await client.start()
            assert await client.wait_connected(2.0)
            queue = client.register("r-late")
            msg = await asyncio.wait_for(queue.get(), timeout=3.0)
            assert isinstance(msg, ErrorNotify)
            assert not client.connected
            assert client._ws is None
        finally:
            ws_client_mod.websockets.connect = original_connect  # type: ignore[assignment]
            await client.stop()

    run(scenario())


class _AsyncCtx:
    def __init__(self, ws) -> None:
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc):
        return False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
