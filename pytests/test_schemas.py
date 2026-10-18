"""共享数据模型单测：Base64url 编解码、信封解码/校验、WS 消息判别解析。

运行：pytest pytests/test_schemas.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from common.schemas import (
    AnswerEnvelope,
    ChatAck,
    EnvelopeError,
    ErrorNotify,
    HelloAuth,
    PingMessage,
    ToolCallEnvelope,
    b64url_decode,
    b64url_encode,
    decode_envelope,
    envelope_summary,
    parse_gateway_message,
    parse_message,
    parse_server_message,
)


# ---------------------------------------------------------------- Base64url

def test_b64url_roundtrip_and_padding():
    raw = '{"type": "answer", "content": "中文", "seq": 1, "total": 1}'.encode("utf-8")
    encoded = b64url_encode(raw)
    assert "=" not in encoded  # 无填充
    assert b64url_decode(encoded) == raw
    # 长度 %4 == 1 的非法输入应抛异常（base64 严格模式）
    with pytest.raises(Exception):
        b64url_decode("A")


# ---------------------------------------------------------------- 信封解码

def test_decode_envelope_tool_call():
    env = ToolCallEnvelope(name="get_weather", arguments={"city": "北京"})
    decoded = decode_envelope(b64url_encode(env.model_dump_json().encode("utf-8")))
    assert isinstance(decoded, ToolCallEnvelope)
    assert decoded.name == "get_weather"
    assert decoded.arguments == {"city": "北京"}


def test_decode_envelope_answer_defaults():
    decoded = decode_envelope(
        b64url_encode(b'{"type":"answer","content":"hi","seq":2,"total":3}')
    )
    assert isinstance(decoded, AnswerEnvelope)
    assert (decoded.seq, decoded.total) == (2, 3)


def test_decode_envelope_errors():
    with pytest.raises(EnvelopeError, match="message 参数为空"):
        decode_envelope("")
    with pytest.raises(EnvelopeError, match="超出长度上限"):
        decode_envelope("A" * 16385)
    with pytest.raises(EnvelopeError, match="不是合法 Base64url"):
        decode_envelope("!!!not-base64!!!")
    with pytest.raises(EnvelopeError, match="不是合法 JSON"):
        decode_envelope(b64url_encode("{不是JSON".encode("utf-8")))
    with pytest.raises(EnvelopeError, match="必须是 JSON 对象"):
        decode_envelope(b64url_encode(b"[1,2,3]"))
    with pytest.raises(EnvelopeError, match="未知信封 type"):
        decode_envelope(b64url_encode(b'{"type":"unknown"}'))
    with pytest.raises(EnvelopeError, match="字段校验失败"):
        decode_envelope(b64url_encode(b'{"type":"tool_call","name":""}'))
    with pytest.raises(EnvelopeError, match="字段校验失败"):
        # total < seq 被 field_validator 拒绝
        decode_envelope(b64url_encode(b'{"type":"answer","seq":3,"total":2}'))


def test_envelope_summary():
    tool = ToolCallEnvelope(name="x")
    ans = AnswerEnvelope(content="一二三", seq=2, total=3)
    assert envelope_summary(tool) == "tool_call:x"
    assert envelope_summary(ans) == "answer:2/3:3chars"


# ---------------------------------------------------------------- WS 消息解析

def test_parse_gateway_message_valid():
    msg = parse_gateway_message({"type": "ping"})
    assert isinstance(msg, PingMessage)
    msg = parse_gateway_message({"type": "hello", "secret": "s"})
    assert isinstance(msg, HelloAuth)


def test_parse_message_errors():
    with pytest.raises(ValueError, match="必须是 JSON 对象"):
        parse_message(["not", "dict"], {}, "TestMessage")
    with pytest.raises(ValueError, match="未知 TestMessage type"):
        parse_message({"type": "nope"}, {}, "TestMessage")


def test_parse_server_message_valid():
    msg = parse_server_message({"type": "chat_ack", "request_id": "r1", "code": "c1"})
    assert isinstance(msg, ChatAck)
    msg = parse_server_message({"type": "error", "request_id": "r1", "message": "boom"})
    assert isinstance(msg, ErrorNotify)
    with pytest.raises(ValueError, match="未知 ServerMessage type"):
        parse_server_message({"type": "hello"})


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
