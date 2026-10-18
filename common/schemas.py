"""共享数据模型：上行信封（模型→①）与 WS 消息（①↔②）。

信封走 HTTP query（Base64url(JSON)），WS 消息走 WebSocket（JSON）。
所有模型均用 pydantic v2 强校验——非法输入的错误信息会直接渲染进
返回给抓取器的页面，回流给模型自我纠正。
"""
from __future__ import annotations

import base64
import json
from typing import Any, Literal, Union

from pydantic import BaseModel, Field, field_validator


class EnvelopeError(ValueError):
    """信封解码/校验失败（msg 可直接展示给模型）。"""


# ---------------------------------------------------------------- 上行信封

class ToolCallEnvelope(BaseModel):
    """模型请求调用工具。"""

    type: Literal["tool_call"] = "tool_call"
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class AnswerEnvelope(BaseModel):
    """模型上交最终答案（可分片，seq/total 从 1 开始）。"""

    type: Literal["answer"] = "answer"
    content: str = ""
    seq: int = Field(ge=1)
    total: int = Field(ge=1)

    @field_validator("total")
    @classmethod
    def _total_must_cover_seq(cls, v: int, info: Any) -> int:
        seq = info.data.get("seq")
        if seq is not None and v < seq:
            raise ValueError("total 必须大于等于 seq")
        return v


Envelope = Union[ToolCallEnvelope, AnswerEnvelope]

_ENVELOPE_TYPES: dict[str, type[BaseModel]] = {
    "tool_call": ToolCallEnvelope,
    "answer": AnswerEnvelope,
}


def b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def decode_envelope(message: str) -> Envelope:
    """解码 Base64url(JSON) 信封；失败抛 EnvelopeError。"""
    if not message:
        raise EnvelopeError("message 参数为空")
    if len(message) > 16384:
        raise EnvelopeError("message 超出长度上限（16KB），请按协议分片")
    try:
        raw = b64url_decode(message)
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeError(f"message 不是合法 Base64url：{exc}") from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeError(f"message 解码后不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise EnvelopeError("信封必须是 JSON 对象")
    etype = data.get("type")
    model = _ENVELOPE_TYPES.get(etype) if isinstance(etype, str) else None
    if model is None:
        raise EnvelopeError(f"未知信封 type={etype!r}，仅支持 tool_call / answer")
    try:
        return model.model_validate(data)  # type: ignore[return-value]
    except Exception as exc:  # noqa: BLE001
        raise EnvelopeError(f"信封字段校验失败：{exc}") from exc


def envelope_summary(env: Envelope) -> str:
    """审计日志用的摘要（不落完整内容）。"""
    if isinstance(env, ToolCallEnvelope):
        return f"tool_call:{env.name}"
    return f"answer:{env.seq}/{env.total}:{len(env.content)}chars"


# ---------------------------------------------------------------- WS 消息（②→①）

class HelloAuth(BaseModel):
    type: Literal["hello"] = "hello"
    secret: str


class ChatMessage(BaseModel):
    """②→①：发起一轮对话（新 user 消息或携带工具结果的续轮）。"""

    type: Literal["chat"] = "chat"
    request_id: str = Field(min_length=1, max_length=128)
    session_key: str = Field(default="default", max_length=256)
    prompt: str = ""                       # 增量注入文本（本轮 user 消息）
    full_prompt: str = ""                  # 重放模式全文（新建上下文时由①选用）
    tools: list[dict[str, Any]] = Field(default_factory=list)
    images: list[str] = Field(default_factory=list)  # base64 data URI
    web_search: bool = True


class ToolResultMessage(BaseModel):
    """②→①：调用方执行完工具，回传结果（唤醒挂起中的 /result）。"""

    type: Literal["tool_result"] = "tool_result"
    request_id: str
    session_key: str = "default"
    call_id: str
    content: str


class StopMessage(BaseModel):
    """②→①：调用方断开/取消 → 点网页停止按钮并复位线程。"""

    type: Literal["stop"] = "stop"
    request_id: str = ""
    session_key: str = "default"


class PingMessage(BaseModel):
    type: Literal["ping"] = "ping"


GatewayMessage = Union[HelloAuth, ChatMessage, ToolResultMessage, StopMessage, PingMessage]

_GATEWAY_TYPES: dict[str, type[BaseModel]] = {
    "hello": HelloAuth,
    "chat": ChatMessage,
    "tool_result": ToolResultMessage,
    "stop": StopMessage,
    "ping": PingMessage,
}


# ---------------------------------------------------------------- WS 消息（①→②）

class ChatAck(BaseModel):
    type: Literal["chat_ack"] = "chat_ack"
    request_id: str
    code: str = ""
    created: bool = False
    error: str | None = None


class ToolCallNotify(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    request_id: str
    session_key: str
    code: str
    call_id: str
    name: str
    arguments: dict[str, Any]


class AnswerNotify(BaseModel):
    type: Literal["answer"] = "answer"
    request_id: str
    session_key: str
    code: str
    content: str
    seq: int = 1
    total: int = 1
    final: bool = True


class ErrorNotify(BaseModel):
    type: Literal["error"] = "error"
    request_id: str
    session_key: str = "default"
    code: str = ""
    message: str


class PongMessage(BaseModel):
    type: Literal["pong"] = "pong"


ServerMessage = Union[ChatAck, ToolCallNotify, AnswerNotify, ErrorNotify, PongMessage]

_SERVER_TYPES: dict[str, type[BaseModel]] = {
    "chat_ack": ChatAck,
    "tool_call": ToolCallNotify,
    "answer": AnswerNotify,
    "error": ErrorNotify,
    "pong": PongMessage,
}


def parse_message(data: Any, table: dict[str, type[BaseModel]], union_name: str) -> BaseModel:
    """按 type 判别器解析 WS 消息；失败抛 ValueError。"""
    if not isinstance(data, dict):
        raise ValueError(f"{union_name} 必须是 JSON 对象")
    mtype = data.get("type")
    model = table.get(mtype) if isinstance(mtype, str) else None
    if model is None:
        raise ValueError(f"未知 {union_name} type={mtype!r}")
    return model.model_validate(data)


def parse_gateway_message(data: Any) -> GatewayMessage:
    return parse_message(data, _GATEWAY_TYPES, "GatewayMessage")  # type: ignore[return-value]


def parse_server_message(data: Any) -> ServerMessage:
    return parse_message(data, _SERVER_TYPES, "ServerMessage")  # type: ignore[return-value]
