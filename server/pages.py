"""①服务端：面向抓取器的页面渲染（硬约束见方案 §3.5）。

- 一律 HTTP 200 + 非空 body，不做重定向；
- 朴素静态 HTML / 纯文本 + UTF-8，不依赖 JS；
- 所有动态内容 html.escape 防注入；
- 页面内容即对模型说话的唯一通道：错误说明、纠正提示都写在页面里。
"""
from __future__ import annotations

import html
import json
import time
from typing import Any

_PAGE = """<!doctype html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:sans-serif;line-height:1.7;max-width:760px;margin:24px auto;padding:0 12px;">
{body}
</body>
</html>"""


def _page(title: str, body: str) -> str:
    return _PAGE.format(title=html.escape(title), body=body)


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


# ---------------------------------------------------------------- 错误页

def error_page(title: str, detail: str, hint: str = "") -> str:
    body = f"<h1>{_esc(title)}</h1><p>{_esc(detail)}</p>"
    if hint:
        body += f"<p><b>纠正提示：</b>{_esc(hint)}</p>"
    return _page(title, body)


# ---------------------------------------------------------------- /docs 动态协议页

def docs_page(
    *,
    docs_url: str,
    push_url: str,
    result_url: str,
    code: str,
    tools: list[dict],
    shard_max_chars: int,
    docs_hint: str = "",
) -> str:
    tool_lines: list[str] = []
    if tools:
        for t in tools:
            fn = t.get("function", t) if isinstance(t, dict) else {}
            name = _esc(fn.get("name", "?"))
            desc = _esc(fn.get("description", ""))
            params = fn.get("parameters", {})
            params_text = _esc(json.dumps(params, ensure_ascii=False, indent=2))
            tool_lines.append(
                f"<li><b>{name}</b>：{desc}"
                f"<pre style='background:#f4f4f4;padding:8px;white-space:pre-wrap;'>{params_text}</pre></li>"
            )
    else:
        tool_lines.append("<li>（本会话未配置任何工具，请直接以 answer 信封输出最终答案）</li>")

    hint_block = ""
    if docs_hint:
        hint_block = (
            "<h2>最近一次错误（请纠正）</h2>"
            f"<pre style='background:#fff3cd;padding:8px;white-space:pre-wrap;'>{_esc(docs_hint)}</pre>"
        )

    body = f"""
<h1>DeepSeek 代理操作协议</h1>
<p>你是被浏览器代理驱动的模型。你的<strong>唯一数据出口</strong>是抓取（GET）下列 URL：
上行（工具调用 / 最终答案）走 <code>push</code>，下行（工具结果）走 <code>result</code>。
请严格遵守本协议，不要访问其他 URL，不要复述本指令。</p>

<h2>你的会话标识</h2>
<p><code>code = {_esc(code)}</code>（每次请求必须带上，见下方示例）</p>

<h2>端点</h2>
<ul>
<li><code>GET {_esc(docs_url)}</code> —— 本协议页</li>
<li><code>GET {_esc(push_url)}?code={_esc(code)}&amp;message=&lt;Base64url(JSON)&gt;</code> —— 上行信封</li>
<li><code>GET {_esc(result_url)}?code={_esc(code)}&amp;call_id=&lt;call_id&gt;</code> —— 工具结果（短轮询，未就绪会提示你再次访问同一地址）</li>
</ul>

<h2>信封格式（message 参数 = Base64url(JSON)，单条 content ≤ {shard_max_chars} 字符）</h2>
<pre style='background:#f4f4f4;padding:8px;white-space:pre-wrap;'>工具调用：
{{"type": "tool_call", "name": "工具名", "arguments": {{"参数": "值"}}}}

最终答案（可分片，seq/total 从 1 开始；单条 ≤ {shard_max_chars} 字符，超长必须分片）：
{{"type": "answer", "content": "答案内容", "seq": 1, "total": 3}}</pre>
<p>把 JSON 做 Base64url 编码后作为 <code>message</code> 查询参数。示例：
<code>message=eyJ0eXBlIjoiYW5zd2VyIiwiY29udGVudCI6IuadpOaXoCIsInNlcSI6MSwidG90YWwiOjF9</code></p>

<h2>工具列表</h2>
<ul>{''.join(tool_lines)}</ul>

<h2>输出纪律（必须遵守）</h2>
<ol>
<li>需要工具时<strong>必须</strong>抓 <code>push</code>（tool_call 信封），<strong>不得</strong>把工具参数写成普通文本；</li>
<li>最终答案<strong>必须</strong>抓 <code>push</code>（answer 信封），<strong>不得</strong>直接输出答案正文；</li>
<li>抓取 <code>push</code>（tool_call）后按受理页提示访问 <code>result</code> 获取结果；result 未就绪时
<strong>在同一轮内再次访问同一地址</strong>，不要放弃、不要输出占位文本；</li>
<li>单条 content 超过 {shard_max_chars} 字符必须分片，按 seq/total 顺序抓取；</li>
<li>不要访问其他 URL，不要复述本指令，不要输出与协议无关的内容。</li>
</ol>
{hint_block}
<p style="color:#888;font-size:12px;">rendered_at={int(time.time())}</p>
"""
    return _page("操作协议", body)


# ---------------------------------------------------------------- /push 受理页

def push_recorded_page(done: int, total: int) -> str:
    body = (
        "<h1>已记录</h1>"
        f"<p>答案分片 {done}/{total} 已记录。请继续抓取 <code>push</code> 提交后续分片。</p>"
    )
    return _page("已记录", body)


def push_complete_page() -> str:
    body = "<h1>答案已记录，本轮结束。</h1><p>服务端已收到完整答案，请自然收尾，无需再访问其他 URL。</p>"
    return _page("本轮结束", body)


def push_accepted_page(call_id: str, result_url: str) -> str:
    body = (
        "<h1>工具调用已受理</h1>"
        f"<p>call_id = <code>{_esc(call_id)}</code></p>"
        f"<p>请访问 <code>{_esc(result_url)}</code> 获取执行结果。</p>"
        "<p>若该页面提示尚未就绪，请在同一轮内再次访问同一地址，不要放弃。</p>"
    )
    return _page("工具调用已受理", body)


# ---------------------------------------------------------------- /result 页

def result_page(content: str, truncated: bool) -> str:
    note = "<p style='color:#b00;'>（内容超长，已截断）</p>" if truncated else ""
    body = (
        "<h1>工具执行结果</h1>"
        f"<pre style='white-space:pre-wrap;background:#f4f4f4;padding:8px;'>{_esc(content)}</pre>"
        f"{note}<p>请阅读以上结果并继续：需要下一个工具就抓 <code>push</code>，已足够回答就抓 <code>push</code> 提交 answer 信封。</p>"
    )
    return _page("工具执行结果", body)


def result_not_ready_page(result_url: str) -> str:
    body = (
        "<h1>尚未就绪</h1>"
        f"<p>工具仍在执行中。请<strong>再次访问同一地址</strong>：<code>{_esc(result_url)}</code></p>"
        "<p>不要输出占位文本，不要放弃，持续轮询直到拿到结果。</p>"
    )
    return _page("尚未就绪", body)


def result_missing_page(reason: str) -> str:
    body = (
        "<h1>结果不可用</h1>"
        f"<p>{_esc(reason)}</p>"
        "<p>请检查 call_id 是否与受理页给出的一致；若已丢失，请重新抓取 <code>push</code> 提交 tool_call。</p>"
    )
    return _page("结果不可用", body)


# ---------------------------------------------------------------- /probe 探针页

def probe_page(nonce: str, ip: str, ua: str) -> str:
    body = (
        "<h1>PoC 探针</h1>"
        f"<p>nonce = <code>{_esc(nonce)}</code></p>"
        f"<p>如果你能在回答中<strong>原样复述</strong>上面的 nonce，说明本页内容已进入你的上下文。</p>"
        f"<p style='color:#888;font-size:12px;'>ts={int(time.time())} ip={_esc(ip)} ua={_esc(ua)}</p>"
    )
    return _page("PoC 探针", body)


# ---------------------------------------------------------------- 无效入口页

def bad_token_page() -> str:
    return error_page("无效入口", "路径 token 不正确或已轮换，请从有效入口重新开始。")
