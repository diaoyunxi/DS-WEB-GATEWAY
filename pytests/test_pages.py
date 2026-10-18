"""①公网面页面渲染单测：200+非空、UTF-8、HTML 转义防注入。

运行：pytest pytests/test_pages.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from server import pages


def _assert_page_ok(html_text: str):
    assert html_text.startswith("<!doctype html>")
    assert 'lang="zh-CN"' in html_text and 'charset="utf-8"' in html_text
    assert html_text.rstrip().endswith("</html>")
    assert len(html_text) > 100


# ---------------------------------------------------------------- 错误页

def test_error_page_with_and_without_hint():
    page = pages.error_page("code 无效", "详情", "请纠正")
    _assert_page_ok(page)
    assert "code 无效" in page and "详情" in page and "纠正提示" in page
    assert "请纠正" in page
    assert "纠正提示" not in pages.error_page("t", "d")  # 无 hint 时不渲染


def test_error_page_escapes_html():
    page = pages.error_page("<script>alert(1)</script>", "a & b")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page and "a &amp; b" in page


# ---------------------------------------------------------------- 协议页

def test_docs_page_with_tools_and_hint():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "查天气",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        },
        # 兼容无 function 包裹的裸工具定义
        {"name": "bare_tool", "description": "裸定义", "parameters": {}},
    ]
    page = pages.docs_page(
        docs_url="https://x.example.com/tok/docs?code=c1",
        push_url="https://x.example.com/tok/push",
        result_url="https://x.example.com/tok/result",
        code="c1",
        tools=tools,
        shard_max_chars=4000,
        docs_hint="最近一次错误：信封格式错误",
    )
    _assert_page_ok(page)
    assert "操作协议" in page
    assert "get_weather" in page and "bare_tool" in page
    assert "查天气" in page and "city" in page
    assert "push?code=c1" in page
    assert "最近一次错误（请纠正）" in page and "信封格式错误" in page
    assert "4000" in page  # 分片上限写进协议


def test_docs_page_no_tools():
    page = pages.docs_page(
        docs_url="u", push_url="p", result_url="r", code="c1", tools=[], shard_max_chars=100
    )
    _assert_page_ok(page)
    assert "本会话未配置任何工具" in page


def test_docs_page_escapes_tool_name():
    page = pages.docs_page(
        docs_url="u",
        push_url="p",
        result_url="r",
        code="<c1>",
        tools=[{"name": "<img onerror=x>", "description": "d"}],
        shard_max_chars=10,
    )
    assert "<img onerror=x>" not in page
    assert "&lt;img onerror=x&gt;" in page
    assert "code = &lt;c1&gt;" in page


# ---------------------------------------------------------------- push / result / probe 页

def test_push_pages():
    _assert_page_ok(pages.push_recorded_page(2, 3))
    assert "2/3" in pages.push_recorded_page(2, 3)
    _assert_page_ok(pages.push_complete_page())
    assert "本轮结束" in pages.push_complete_page()
    accepted = pages.push_accepted_page("call_1", "https://x/result?code=c1&call_id=call_1")
    _assert_page_ok(accepted)
    assert "call_1" in accepted and "result?code=c1&amp;call_id=call_1" in accepted


def test_result_pages():
    page = pages.result_page("晴，20℃", truncated=True)
    _assert_page_ok(page)
    assert "晴，20℃" in page and "已截断" in page
    assert "已截断" not in pages.result_page("x", truncated=False)
    not_ready = pages.result_not_ready_page("https://x/result")
    _assert_page_ok(not_ready)
    assert "尚未就绪" in not_ready and "再次访问同一地址" in not_ready
    missing = pages.result_missing_page("该结果已被消费（一次性交付）。")
    _assert_page_ok(missing)
    assert "结果不可用" in missing and "已被消费" in missing


def test_result_page_escapes_content():
    page = pages.result_page("<b>x</b>", truncated=False)
    assert "<b>x</b>" not in page and "&lt;b&gt;x&lt;/b&gt;" in page


def test_probe_page():
    page = pages.probe_page("北极星-7F3K", "1.2.3.4", "Mozilla/5.0")
    _assert_page_ok(page)
    assert "北极星-7F3K" in page and "1.2.3.4" in page and "Mozilla/5.0" in page


def test_bad_token_page():
    page = pages.bad_token_page()
    _assert_page_ok(page)
    assert "无效入口" in page


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
