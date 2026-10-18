"""②Gateway：messages → 注入文本渲染。

- 增量模式（默认）：只取最后一条 user 消息的文本；
- 重放模式：把调用方历史渲染为单条结构化文本（【历史对话】/【本轮请求】分节），
  超预算时从最旧开始裁剪；
- 图片提取：最后一条 user 消息的 image_url（http(s) 由②代下载转 data URI）。
"""
from __future__ import annotations

from typing import Any

# 单条消息 content 的两种形态：str 或 [{"type":"text","text":...}, {"type":"image_url",...}]


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(t for t in parts if t)
    return ""


def _images_of(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    urls = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url:
                urls.append(url)
    return urls


def last_user_index(messages: list[dict]) -> int:
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            return i
    return -1


def render_incremental(messages: list[dict]) -> str:
    """增量模式：最后一条 user 消息的文本。"""
    idx = last_user_index(messages)
    if idx < 0:
        return ""
    return _text_of(messages[idx].get("content"))


def extract_images(messages: list[dict]) -> list[str]:
    """最后一条 user 消息里的图片 URL 列表。"""
    idx = last_user_index(messages)
    if idx < 0:
        return []
    return _images_of(messages[idx].get("content"))


def trailing_tool_results(messages: list[dict]) -> list[tuple[str, str]]:
    """末尾连续的 role:tool 消息 → [(tool_call_id, content)]。"""
    results: list[tuple[str, str]] = []
    for msg in reversed(messages):
        if msg.get("role") != "tool":
            break
        results.append((str(msg.get("tool_call_id", "")), _text_of(msg.get("content"))))
    results.reverse()
    return results


def render_full(messages: list[dict], max_chars: int = 24000) -> str:
    """重放模式：历史渲染为单条结构化文本，超预算从最旧裁剪。"""
    lines: list[str] = []
    for msg in messages:
        role = msg.get("role", "?")
        content = _text_of(msg.get("content"))
        if not content:
            continue
        if role == "tool":
            lines.append(f"[工具结果 {msg.get('tool_call_id', '')}] {content}")
        elif role == "assistant":
            calls = msg.get("tool_calls") or []
            if calls:
                names = ", ".join(str(c.get("function", {}).get("name", "?")) for c in calls)
                lines.append(f"assistant: （调用了工具：{names}）")
                if content:
                    lines.append(f"assistant: {content}")
            else:
                lines.append(f"assistant: {content}")
        elif role == "system":
            lines.append(f"[系统] {content}")
        else:
            lines.append(f"user: {content}")

    body = "\n".join(lines)
    if len(body) <= max_chars:
        return f"【历史对话】（最后一条即本轮请求）\n{body}"

    # 超预算：保留系统消息 + 尽可能多的最近消息
    head = [ln for ln in lines if ln.startswith("[系统]")]
    tail: list[str] = []
    budget = max_chars - sum(len(ln) + 1 for ln in head) - 64
    for ln in reversed(lines):
        if ln.startswith("[系统]"):
            continue
        if budget - (len(ln) + 1) < 0:
            break
        tail.insert(0, ln)
        budget -= len(ln) + 1
    dropped = len([ln for ln in lines if not ln.startswith("[系统]")]) - len(tail)
    body = "\n".join(head + [f"（更早的 {dropped} 条消息因超出网页上下文预算已裁剪）"] + tail)
    return f"【历史对话】\n{body}"
