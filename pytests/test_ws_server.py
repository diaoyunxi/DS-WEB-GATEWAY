"""WS 服务联调：hello/auth 鉴权、ping/pong 心跳、错误路径的 ErrorNotify。

运行：python pytests/test_ws_server.py 或 pytest pytests/test_ws_server.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("PATH_TOKEN", "tok-abcdefghijklmnop")
os.environ.setdefault("WS_SECRET", "secret-xyz")
os.environ.setdefault("DATA_DIR", "data-test")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from common.schemas import ChatMessage, HelloAuth, PingMessage
from server.alerts import AlertManager
from server.audit import AuditLog
from server.context import ServerContext
from server.envelopes import ShardBuffer
from server.main import build_app
from server.pending import PendingCallRegistry
from server.ratelimit import RateLimiter
from server.sessions import SessionManager
from server.ws_server import WSHub

TOKEN = "tok-abcdefghijklmnop"


def _build_app():
    from common.config import Settings

    settings = Settings()
    settings.path_token = TOKEN
    settings.ws_secret = "secret-xyz"
    settings.data_dir = "data-test"

    shard = ShardBuffer(60)
    pending = PendingCallRegistry(60)
    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None  # 禁止真实发信
    manager = SessionManager(SimpleNamespace(), settings, alerts, shard, pending)
    ctx = ServerContext(
        settings=settings,
        engine=SimpleNamespace(),
        manager=manager,
        hub=WSHub(),
        alerts=alerts,
        audit=AuditLog(),
        limiter=RateLimiter(100, 100, 100),
        shard=shard,
        pending=pending,
    )
    return build_app(ctx)


def test_ws_server():
    app = _build_app()
    client = TestClient(app)
    # ---- 错误密钥 → 4401 ----
    try:
        with client.websocket_connect(f"/{TOKEN}/ws") as ws:
            ws.send_text(HelloAuth(secret="wrong").model_dump_json())
            ws.receive_text()
        raise AssertionError("应当被拒绝")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401, exc.code

    # ---- 无效 token → 4401 ----
    try:
        with client.websocket_connect("/bad-token/ws") as ws:
            ws.send_text(HelloAuth(secret="secret-xyz").model_dump_json())
            ws.receive_text()
        raise AssertionError("应当被拒绝")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401, exc.code

    # ---- 正常链路：auth → ping/pong；chat 走错误路径回 ErrorNotify ----
    with client.websocket_connect(f"/{TOKEN}/ws") as ws:
        ws.send_text(HelloAuth(secret="secret-xyz").model_dump_json())
        ws.send_text(PingMessage().model_dump_json())
        msg = json.loads(ws.receive_text())
        assert msg["type"] == "pong", msg

        # chat：假 engine 无浏览器 → 走异常路径 → ErrorNotify（request_id 关联）
        ws.send_text(
            ChatMessage(
                request_id="req-ws-1", session_key="u1", prompt="hi", full_prompt="full"
            ).model_dump_json()
        )
        got = None
        for _ in range(5):
            msg = json.loads(ws.receive_text())
            if msg.get("type") == "error" and msg.get("request_id") == "req-ws-1":
                got = msg
                break
        assert got is not None, "未收到 ErrorNotify"
        print("chat 错误路径 ->", got["message"][:60])

        # 未知会话的 tool_result → ErrorNotify
        ws.send_text(
            json.dumps(
                {
                    "type": "tool_result",
                    "request_id": "req-ws-2",
                    "session_key": "nobody",
                    "call_id": "call_x",
                    "content": "r",
                }
            )
        )
        for _ in range(5):
            msg = json.loads(ws.receive_text())
            if msg.get("type") == "error" and msg.get("request_id") == "req-ws-2":
                break
        else:
            raise AssertionError("未收到 tool_result 的 ErrorNotify")

        # 未知会话 stop：静默无消息（不发错误）
        ws.send_text(json.dumps({"type": "stop", "request_id": "req-ws-3", "session_key": "nobody"}))

    print("WS SERVER TESTS PASSED")


# ---------------------------------------------------------------- 注入策略测试

class _FakeKeyboard:
    """分段 type 汇总为一条完整注入（Enter 时落库）。"""

    def __init__(self, sink: list) -> None:
        self._sink = sink
        self._buf: list[str] = []

    async def type(self, text: str, delay: int = 0) -> None:
        self._buf.append(text)

    async def insert_text(self, text: str) -> None:
        self._buf.append(text)

    async def press(self, key: str) -> None:
        if key == "Enter" and self._buf:
            self._sink.append("".join(self._buf))
            self._buf = []


class _FakeLocator:
    def __init__(self, n: int) -> None:
        self._n = n

    async def count(self) -> int:
        return self._n

    @property
    def first(self):
        return self

    async def click(self, **kwargs) -> None:
        pass

    async def get_attribute(self, name: str):
        return None


class _FakePage:
    """按选择器用途返回计数：input/done 存在，其余不存在（秒答路径）。"""

    _COUNTS = {"input": 1, "done": 1}

    def __init__(self, sink: list) -> None:
        self.keyboard = _FakeKeyboard(sink)

    def locator(self, selector: str):
        from server.config import SELECTORS

        for key, selectors in SELECTORS.items():
            if selector in selectors:
                return _FakeLocator(self._COUNTS.get(key, 0))
        return _FakeLocator(0)

    async def goto(self, url: str, **kwargs) -> None:
        pass

    async def click(self, selector: str, **kwargs) -> None:
        pass

    async def set_input_files(self, selector: str, paths) -> None:
        pass

    async def screenshot(self, **kwargs) -> str:
        return ""


class _FakeContext:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def add_init_script(self, script: str) -> None:
        pass

    async def add_cookies(self, cookies: list) -> None:
        pass

    async def new_page(self):
        return _FakePage(self._sink)

    async def close(self) -> None:
        pass


class _FakeEngine:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    async def new_context(self, cookies: list):
        return _FakeContext(self._sink)


def test_injection_strategy():
    """首轮=骨架+全文；增量轮=仅增量；第 N 轮重注入骨架。"""
    from common.config import Settings

    settings = Settings()
    settings.path_token = TOKEN
    settings.ws_secret = "secret-xyz"
    settings.data_dir = "data-test"
    settings.skeleton_every_n_turns = 2
    settings.generation_start_timeout = 1.0

    injected: list[str] = []
    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None
    shard = ShardBuffer(60)
    pending = PendingCallRegistry(60)
    manager = SessionManager(_FakeEngine(injected), settings, alerts, shard, pending)
    ctx = ServerContext(
        settings=settings,
        engine=_FakeEngine(injected),
        manager=manager,
        hub=WSHub(),
        alerts=alerts,
        audit=AuditLog(),
        limiter=RateLimiter(100, 100, 100),
        shard=shard,
        pending=pending,
    )
    client = TestClient(build_app(ctx))

    def send_chat_and_wait(request_id: str, expect_count: int) -> None:
        """开连接 → 发 chat → 收 chat_ack → 保持连接轮询直到注入数达标。"""
        import time as _time

        with client.websocket_connect(f"/{TOKEN}/ws") as ws:
            ws.send_text(HelloAuth(secret="secret-xyz").model_dump_json())
            ws.send_text(
                ChatMessage(
                    request_id=request_id,
                    session_key="u-inject",
                    prompt=f"增量-{request_id}",
                    full_prompt=f"全文-{request_id}",
                ).model_dump_json()
            )
            for _ in range(20):
                msg = json.loads(ws.receive_text())
                if msg.get("type") == "chat_ack" and msg.get("request_id") == request_id:
                    break
            else:
                raise AssertionError("未收到 chat_ack")
            # 连接保持打开：TestClient 退出 with 时会取消服务端挂起任务
            for _ in range(200):
                if len(injected) >= expect_count:
                    break
                _time.sleep(0.05)
            assert len(injected) >= expect_count, f"注入未达 {expect_count}：{injected!r}"
            # 留给后台 run() 收尾（turns 计数在生成结束后才 +1）
            _time.sleep(2.5)

    # 第一轮：新建上下文 → 骨架 + 全文
    send_chat_and_wait("r1", 1)
    assert injected[0].startswith("开始任务前，请先访问"), injected[0][:40]
    assert "全文-r1" in injected[0]

    # 第二轮：增量（turns=1，1 % 2 != 0）→ 仅增量
    send_chat_and_wait("r2", 2)
    assert injected[1] == "增量-r2", f"第二轮注入异常: {injected[1]!r}"

    # 第三轮：turns=2，2 % 2 == 0 → 重注入骨架
    send_chat_and_wait("r3", 3)
    assert injected[2].startswith("开始任务前，请先访问") and "增量-r3" in injected[2]
    print("INJECTION STRATEGY TESTS PASSED")


if __name__ == "__main__":
    test_ws_server()
    test_injection_strategy()
