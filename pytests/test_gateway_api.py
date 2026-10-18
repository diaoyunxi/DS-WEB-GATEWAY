"""②Gateway OpenAI 端点联调（mock ① 的 WS 行为，无需真实①/浏览器）。

覆盖：chat → tool_call（finish_reason=tool_calls）、role:tool 续轮 → answer、
stream SSE、/v1/models、Bearer 鉴权。

运行：python pytests/test_gateway_api.py 或 pytest pytests/test_gateway_api.py -q
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import httpx

import gateway.openai_api as openai_api
from common.schemas import (
    AnswerNotify,
    ChatAck,
    ChatMessage,
    ToolCallNotify,
    ToolResultMessage,
)
from gateway.main import app


class FakeWS:
    """模拟①：chat → 立即推 tool_call；tool_result → 推 answer。"""

    def __init__(self) -> None:
        self.sent: list = []
        self.waiters: dict[str, asyncio.Queue] = {}

    @property
    def connected(self) -> bool:
        return True

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        return True

    def register(self, request_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self.waiters[request_id] = queue
        return queue

    def unregister(self, request_id: str) -> None:
        self.waiters.pop(request_id, None)

    async def send(self, msg) -> None:
        self.sent.append(msg)
        request_id = msg.request_id
        queue = self.waiters.get(request_id)
        if queue is None:
            return
        if isinstance(msg, ChatMessage):
            queue.put_nowait(ChatAck(request_id=request_id, code="c1", created=True))
            queue.put_nowait(
                ToolCallNotify(
                    request_id=request_id,
                    session_key=msg.session_key,
                    code="c1",
                    call_id="call_1",
                    name="get_weather",
                    arguments={"city": "北京"},
                )
            )
        elif isinstance(msg, ToolResultMessage):
            queue.put_nowait(
                AnswerNotify(
                    request_id=request_id,
                    session_key=msg.session_key,
                    code="c1",
                    content="北京：晴，20℃",
                )
            )


def test_gateway_api():
    fake = FakeWS()
    openai_api.get_ws_client = lambda: fake  # type: ignore[assignment]

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            # ---- 第一轮：触发 tool_call ----
            body = {
                "model": "deepseek-web",
                "messages": [{"role": "user", "content": "北京天气如何？"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "description": "查天气",
                            "parameters": {
                                "type": "object",
                                "properties": {"city": {"type": "string"}},
                            },
                        },
                    }
                ],
            }
            r = await client.post("/v1/chat/completions", json=body)
            assert r.status_code == 200, r.text
            data = r.json()
            choice = data["choices"][0]
            assert choice["finish_reason"] == "tool_calls"
            call = choice["message"]["tool_calls"][0]
            assert call["id"] == "call_1"
            assert call["function"]["name"] == "get_weather"
            assert json.loads(call["function"]["arguments"])["city"] == "北京"
            chat_msg = fake.sent[0]
            assert isinstance(chat_msg, ChatMessage)
            assert chat_msg.tools[0]["function"]["name"] == "get_weather"
            assert chat_msg.prompt == "北京天气如何？"

            # ---- 第二轮：role:tool 续轮 → answer ----
            body2 = {
                "model": "deepseek-web",
                "messages": [
                    {"role": "user", "content": "北京天气如何？"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": '{"city": "北京"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "晴，20℃"},
                ],
            }
            r = await client.post("/v1/chat/completions", json=body2)
            assert r.status_code == 200, r.text
            data = r.json()
            assert data["choices"][0]["finish_reason"] == "stop"
            assert data["choices"][0]["message"]["content"] == "北京：晴，20℃"
            tr_msg = fake.sent[1]
            assert isinstance(tr_msg, ToolResultMessage) and tr_msg.call_id == "call_1"

            # ---- stream ----
            r = await client.post(
                "/v1/chat/completions",
                json={"model": "deepseek-web", "stream": True, "messages": body["messages"]},
            )
            assert r.status_code == 200
            text = r.text
            assert "data: [DONE]" in text
            assert '"tool_calls"' in text and '"finish_reason": "tool_calls"' in text
            assert '"role": "assistant"' in text

            # ---- /v1/models ----
            r = await client.get("/v1/models")
            assert r.status_code == 200 and r.json()["data"][0]["id"] == "deepseek-web"

            # ---- 鉴权 ----
            from common.config import get_settings

            settings = get_settings()
            settings.gateway_api_keys = "k-secret"
            r = await client.post("/v1/chat/completions", json=body)
            assert r.status_code == 401
            r = await client.post(
                "/v1/chat/completions",
                json=body,
                headers={"Authorization": "Bearer k-secret"},
            )
            assert r.status_code == 200
            settings.gateway_api_keys = ""

    asyncio.run(run())
    print("GATEWAY API TESTS PASSED")


if __name__ == "__main__":
    test_gateway_api()
