"""公网面 API 级联调（不需要真实浏览器/DeepSeek 登录）。

覆盖：/docs 动态协议页、/push（tool_call 登记+去重+下发、answer 重组+下发、
错误信封自纠正页）、/result（未就绪/就绪/一次性消费）、/probe、无效 token。

运行：pytest pytests/test_public_api.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("PATH_TOKEN", "tok-abcdefghijklmnop")
os.environ.setdefault("WS_SECRET", "secret-xyz")
os.environ.setdefault("POLL_TIMEOUT", "0.3")
os.environ.setdefault("DATA_DIR", "data-test")

import httpx

from common.schemas import (
    AnswerEnvelope,
    AnswerNotify,
    ToolCallEnvelope,
    ToolCallNotify,
    b64url_encode,
)
from server.alerts import AlertManager
from server.audit import AuditLog
from server.context import ServerContext
from server.envelopes import ShardBuffer
from server.main import build_app
from server.pending import PendingCallRegistry
from server.ratelimit import RateLimiter
from server.sessions import SessionManager
from server.ws_server import WSHub


def _envelope_url(env) -> str:
    return b64url_encode(env.model_dump_json().encode("utf-8"))


def _build_ctx():
    from common.config import Settings

    settings = Settings()
    settings.path_token = "tok-abcdefghijklmnop"
    settings.poll_timeout = 0.3
    settings.data_dir = "data-test"

    shard = ShardBuffer(60)
    pending = PendingCallRegistry(60)
    sent: list = []

    class _Hub(WSHub):
        async def send(self, msg):  # type: ignore[override]
            sent.append(msg)
            return True

    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None  # 禁止真实发信

    manager = SessionManager(SimpleNamespace(), settings, alerts, shard, pending)
    session = SimpleNamespace(
        code="c1",
        session_key="u1",
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "查天气",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        pending=pending,
        shard=shard,
        request_id="req-1",
        last_active=0.0,
        docs_hint="",
        state="idle",
        turns=0,
    )
    manager._sessions["c1"] = session
    manager._by_key["u1"] = "c1"

    ctx = ServerContext(
        settings=settings,
        engine=SimpleNamespace(),
        manager=manager,
        hub=_Hub(),
        alerts=alerts,
        audit=AuditLog(),
        limiter=RateLimiter(100, 100, 100),
        shard=shard,
        pending=pending,
    )
    return ctx, sent, pending


def test_public_api():
    ctx, sent, pending = _build_ctx()
    app = build_app(ctx)
    token = "tok-abcdefghijklmnop"
    base = f"/{token}"

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # ---- docs 动态协议页 ----
            r = await client.get(f"{base}/docs", params={"code": "c1"})
            assert r.status_code == 200 and "get_weather" in r.text and "操作协议" in r.text
            assert "/push?code=c1" in r.text

            # ---- 无效 code ----
            r = await client.get(f"{base}/docs", params={"code": "nope"})
            assert r.status_code == 200 and "code 无效" in r.text

            # ---- 无效 token（仍 200 页面）----
            r = await client.get("/bad-token/docs", params={"code": "c1"})
            assert r.status_code == 200 and "无效入口" in r.text

            # ---- push: tool_call 登记 + 下发 ----
            env = ToolCallEnvelope(name="get_weather", arguments={"city": "北京"})
            r = await client.get(
                f"{base}/push", params={"code": "c1", "message": _envelope_url(env)}
            )
            assert r.status_code == 200 and "工具调用已受理" in r.text
            assert "result?code=c1&amp;call_id=" in r.text
            assert len(sent) == 1 and isinstance(sent[0], ToolCallNotify)
            call_id = sent[0].call_id
            assert sent[0].request_id == "req-1" and sent[0].name == "get_weather"

            # ---- push: 重复 tool_call（去重，不重复下发）----
            r = await client.get(
                f"{base}/push", params={"code": "c1", "message": _envelope_url(env)}
            )
            assert r.status_code == 200 and call_id in r.text
            assert len(sent) == 1

            # ---- result: 未就绪 ----
            r = await client.get(f"{base}/result", params={"code": "c1", "call_id": call_id})
            assert r.status_code == 200 and "尚未就绪" in r.text

            # ---- ②回传结果 → result 就绪 ----
            ok = await pending.set_result(call_id, "晴，20℃")
            assert ok
            r = await client.get(f"{base}/result", params={"code": "c1", "call_id": call_id})
            assert r.status_code == 200 and "晴，20℃" in r.text

            # ---- result: 一次性消费 ----
            r = await client.get(f"{base}/result", params={"code": "c1", "call_id": call_id})
            assert r.status_code == 200 and "已被消费" in r.text

            # ---- push: answer 分片（乱序）----
            r = await client.get(
                f"{base}/push",
                params={
                    "code": "c1",
                    "message": _envelope_url(
                        AnswerEnvelope(content="第三段", seq=2, total=2)
                    ),
                },
            )
            assert r.status_code == 200 and "已记录" in r.text
            assert len(sent) == 1  # 未齐不下发
            r = await client.get(
                f"{base}/push",
                params={
                    "code": "c1",
                    "message": _envelope_url(
                        AnswerEnvelope(content="第一段", seq=1, total=2)
                    ),
                },
            )
            assert r.status_code == 200 and "本轮结束" in r.text
            assert len(sent) == 2 and isinstance(sent[1], AnswerNotify)
            assert sent[1].content == "第一段第三段"

            # ---- push: 错误信封 → 200 自纠正页 + docs_hint ----
            r = await client.get(
                f"{base}/push",
                params={"code": "c1", "message": b64url_encode("{不是JSON".encode("utf-8"))},
            )
            assert r.status_code == 200 and "信封格式错误" in r.text and "纠正提示" in r.text
            r = await client.get(f"{base}/docs", params={"code": "c1"})
            assert "最近一次错误" in r.text

            # ---- probe ----
            r = await client.get(f"{base}/probe", params={"nonce": "北极星-7F3K"})
            assert r.status_code == 200 and "北极星-7F3K" in r.text

            # ---- 遥测日志 ----
            records = ctx.audit.recent(10)
            kinds = {rec["kind"] for rec in records}
            assert {"docs", "push", "result", "probe"} <= kinds
            print("telemetry kinds:", sorted(kinds))

    asyncio.run(run())
    print("PUBLIC API TESTS PASSED")


if __name__ == "__main__":
    test_public_api()
