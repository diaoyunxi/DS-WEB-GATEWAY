"""②Gateway 渲染单测：增量/重放/图片提取/工具结果识别。

运行：pytest pytests/test_render.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from gateway.render import (
    _images_of,
    _text_of,
    extract_images,
    last_user_index,
    render_full,
    render_incremental,
    trailing_tool_results,
)


# ---------------------------------------------------------------- 基础解析

def test_text_of_variants():
    assert _text_of("纯文本") == "纯文本"
    assert (
        _text_of([{"type": "text", "text": "甲"}, {"type": "text", "text": "乙"}]) == "甲\n乙"
    )
    assert _text_of([{"type": "image_url", "image_url": {"url": "u"}}]) == ""
    assert _text_of(None) == ""
    assert _text_of([{"no_type": 1}, "not-dict"]) == ""


def test_images_of_variants():
    assert _images_of("str") == []
    assert _images_of(None) == []
    assert _images_of([{"type": "image_url", "image_url": {"url": "http://a/1.png"}}]) == [
        "http://a/1.png"
    ]
    # 空 url / 非 dict 部件被跳过
    assert _images_of(
        [{"type": "image_url", "image_url": {"url": ""}}, {"type": "image_url"}, "x"]
    ) == []


def test_last_user_index():
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "u2"},
    ]
    assert last_user_index(msgs) == 3
    assert last_user_index([{"role": "assistant", "content": "a"}]) == -1
    assert last_user_index([]) == -1


# ---------------------------------------------------------------- 增量 / 图片 / 工具结果

def test_render_incremental():
    msgs = [
        {"role": "user", "content": "旧消息"},
        {"role": "assistant", "content": "回复"},
        {"role": "user", "content": [{"type": "text", "text": "最新"}]},
    ]
    assert render_incremental(msgs) == "最新"
    assert render_incremental([{"role": "assistant", "content": "a"}]) == ""


def test_extract_images():
    msgs = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "u1"}}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "u2"}},
                {"type": "image_url", "image_url": {"url": "u3"}},
            ],
        },
    ]
    assert extract_images(msgs) == ["u2", "u3"]
    assert extract_images([{"role": "assistant", "content": "x"}]) == []


def test_trailing_tool_results():
    msgs = [
        {"role": "user", "content": "q"},
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": [{"type": "text", "text": "r2"}]},
    ]
    assert trailing_tool_results(msgs) == [("c1", "r1"), ("c2", "r2")]
    # 末尾不是 tool → 空
    assert trailing_tool_results(msgs + [{"role": "user", "content": "x"}]) == []
    assert trailing_tool_results([]) == []
    # 缺 tool_call_id → 空字符串
    assert trailing_tool_results([{"role": "tool", "content": "r"}]) == [("", "r")]


# ---------------------------------------------------------------- 重放

def test_render_full_all_roles():
    msgs = [
        {"role": "system", "content": "系统指令"},
        {"role": "user", "content": "问题"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"function": {"name": "get_weather"}},
                {"function": {"name": "search"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        {"role": "assistant", "content": "（无工具直答）"},
        {"role": "user", "content": "追问"},
        {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "f"}}]},
    ]
    text = render_full(msgs)
    assert text.startswith("【历史对话】（最后一条即本轮请求）")
    assert "[系统] 系统指令" in text
    assert "user: 问题" in text
    assert "assistant: （调用了工具：get_weather, search）" in text
    assert "[工具结果 c1] 结果" in text
    assert "assistant: （无工具直答）" in text
    assert "user: 追问" in text
    # content 为空且无 tool_calls 的消息被跳过
    assert text.count("assistant: （调用了工具") == 2


def test_render_full_truncation_keeps_system_and_recent():
    msgs = [{"role": "system", "content": "S" * 20}]
    for i in range(20):
        msgs.append({"role": "user", "content": f"m{i:02d}" + "x" * 50})
    text = render_full(msgs, max_chars=300)
    assert text.startswith("【历史对话】\n")
    assert "[系统] " + "S" * 20 in text  # 系统消息保留
    assert "已裁剪" in text  # 出现裁剪说明
    assert "user: m19" in text  # 最新消息保留
    assert "user: m00" not in text  # 最旧消息被裁掉


def test_render_full_under_budget():
    msgs = [{"role": "user", "content": "短"}]
    text = render_full(msgs, max_chars=1000)
    assert "（最后一条即本轮请求）" in text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
