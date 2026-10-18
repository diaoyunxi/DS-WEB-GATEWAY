"""端到端联调：真实 uvicorn 起① + 真实 GatewayWSClient 拨入（桩掉浏览器启动）。

验证：WS URL 拼接、hello/auth、消息按 request_id 派发、chat 错误路径
（浏览器未启动 → ErrorNotify）、hub 连接状态。

运行：python pytests/test_ws_e2e.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import uvicorn

from common.config import get_settings
from common.schemas import ChatMessage, ErrorNotify
from server.alerts import AlertManager
from server.audit import AuditLog
from server.browser import BrowserEngine
from server.context import ServerContext
from server.envelopes import ShardBuffer
from server.main import build_app
from server.pending import PendingCallRegistry
from server.ratelimit import RateLimiter
from server.sessions import SessionManager
from server.ws_server import WSHub
from gateway.ws_client import GatewayWSClient

PORT = 18443


async def main() -> None:
    settings = get_settings()
    settings.path_token = "tok-e2e-token"
    settings.ws_secret = "secret-e2e"
    settings.data_dir = "data-test"
    settings.ws_heartbeat = 1  # 加速心跳观测

    # 桩掉浏览器启动/停止（不下载 Chromium 也能联调 WS 链路）
    BrowserEngine.start = lambda self: asyncio.sleep(0)  # type: ignore[assignment]
    BrowserEngine.stop = lambda self: asyncio.sleep(0)  # type: ignore[assignment]

    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None  # 禁止真实发信
    shard = ShardBuffer(60)
    pending = PendingCallRegistry(60)
    ctx = ServerContext(
        settings=settings,
        engine=BrowserEngine(settings, alerts),
        manager=SessionManager(BrowserEngine(settings, alerts), settings, alerts, shard, pending),
        hub=WSHub(),
        alerts=alerts,
        audit=AuditLog(),
        limiter=RateLimiter(100, 100, 100),
        shard=shard,
        pending=pending,
    )
    app = build_app(ctx)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    assert server.started, "uvicorn 未启动"

    settings.ws_url = f"ws://127.0.0.1:{PORT}/{{token}}/ws"
    client = GatewayWSClient(settings)
    await client.start()
    assert await client.wait_connected(10.0), "WS 客户端未连上①"
    # 客户端发完 hello 即自报connected；服务端 attach 略有先后，轮询等待
    for _ in range(50):
        if ctx.hub.connected:
            break
        await asyncio.sleep(0.1)
    assert ctx.hub.connected, "①侧 hub 未记录连接"

    # 心跳：等两个周期确认不断线
    await asyncio.sleep(2.5)
    assert ctx.hub.connected, "心跳期间连接断开"

    # chat → 浏览器未启动 → ErrorNotify（按 request_id 派发）
    request_id = "req-e2e-1"
    queue = client.register(request_id)
    await client.send(
        ChatMessage(request_id=request_id, session_key="u1", prompt="hi", full_prompt="full")
    )
    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
    assert isinstance(msg, ErrorNotify), msg
    assert msg.request_id == request_id
    print("端到端 ErrorNotify ->", msg.message[:50])

    # 未知会话 tool_result → ErrorNotify
    from common.schemas import ToolResultMessage

    request_id2 = "req-e2e-2"
    queue2 = client.register(request_id2)
    await client.send(
        ToolResultMessage(
            request_id=request_id2, session_key="nobody", call_id="call_x", content="r"
        )
    )
    msg2 = await asyncio.wait_for(queue2.get(), timeout=15.0)
    assert isinstance(msg2, ErrorNotify) and msg2.request_id == request_id2
    print("端到端 tool_result 错误路径 ->", msg2.message[:50])

    # 审计：WS 连接已记录抓取面日志（无抓取，仅确认 hub 状态）
    await client.stop()
    server.should_exit = True
    await server_task
    print("WS E2E TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
