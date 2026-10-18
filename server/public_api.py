"""①服务端：公网面（面向 DeepSeek 抓取器，路径带高熵 token）。

端点（见方案 §3.1）：
- GET /{token}/docs?code=xxx   动态协议页（按 code 渲染该会话可用 tools）
- GET /{token}/push?code&message  上行信封（tool_call 登记 / answer 分片重组）
- GET /{token}/result?code&call_id  工具结果短轮询（挂起 10–15s，一次性消费）
- GET /{token}/probe?nonce      PoC 探针 + 连通性自检

硬约束（§3.5）：一律 200 + 非空 body；朴素静态 HTML/UTF-8；不重定向；
无 cookie/localStorage 可依赖，状态全在 URL（code + call_id）。
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from common.schemas import (
    AnswerNotify,
    EnvelopeError,
    ToolCallEnvelope,
    ToolCallNotify,
    decode_envelope,
    envelope_summary,
)

from . import pages

logger = logging.getLogger("server.public")

router = APIRouter()


def _ctx(request: Request):
    return request.app.state.ctx


def _client_ip(request: Request) -> str:
    ctx = _ctx(request)
    if ctx.settings.trust_forwarded_for:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _telemetry(request: Request, ctx, kind: str, code: str, summary: str = "") -> None:
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    ctx.audit.record(ip=ip, ua=ua, path=request.url.path, code=code, kind=kind, summary=summary)
    logger.info("抓取 %s ip=%s ua=%s code=%s %s", kind, ip, ua[:60], code[:8] or "-", summary)


def _rate_limited(ctx, code: str) -> bool:
    return not ctx.limiter.check(code)


# ---------------------------------------------------------------- /docs

@router.get("/{token}/docs", response_class=HTMLResponse)
async def docs(request: Request, token: str, code: str = Query(default="")) -> HTMLResponse:
    ctx = _ctx(request)
    _telemetry(request, ctx, "docs", code)
    if token not in ctx.settings.valid_tokens:
        return HTMLResponse(pages.bad_token_page())
    if _rate_limited(ctx, code):
        return HTMLResponse(pages.error_page("频率超限", "请放慢抓取频率后重试。"))
    session = ctx.manager.get_by_code(code)
    if session is None:
        return HTMLResponse(
            pages.error_page(
                "code 无效",
                "该 code 不对应任何活跃会话（可能已过期或被驱逐）。",
                "请从调用方重新发起对话以获取新的 code。",
            )
        )
    docs_url = ctx.settings.docs_url(token, code)
    return HTMLResponse(
        pages.docs_page(
            docs_url=docs_url,
            push_url=ctx.settings.push_url(token),
            result_url=ctx.settings.result_url(token),
            code=code,
            tools=session.tools,
            shard_max_chars=ctx.settings.answer_shard_max_chars,
            docs_hint=session.docs_hint,
        )
    )


# ---------------------------------------------------------------- /push

@router.get("/{token}/push", response_class=HTMLResponse)
async def push(
    request: Request,
    token: str,
    code: str = Query(default=""),
    message: str = Query(default=""),
) -> HTMLResponse:
    ctx = _ctx(request)
    if token not in ctx.settings.valid_tokens:
        _telemetry(request, ctx, "push-bad-token", code)
        return HTMLResponse(pages.bad_token_page())
    session = ctx.manager.get_by_code(code)
    if session is None:
        _telemetry(request, ctx, "push-bad-code", code)
        return HTMLResponse(
            pages.error_page(
                "code 无效",
                "该 code 不对应任何活跃会话（可能已过期或被驱逐）。",
                "请从调用方重新发起对话以获取新的 code。",
            )
        )
    if _rate_limited(ctx, code):
        return HTMLResponse(pages.error_page("频率超限", "请放慢抓取频率后重试。"))
    try:
        env = decode_envelope(message)
    except EnvelopeError as exc:
        reason = str(exc)
        session.docs_hint = reason
        _telemetry(request, ctx, "push-bad-envelope", code, reason[:80])
        return HTMLResponse(
            pages.error_page(
                "信封格式错误",
                reason,
                'message 必须是 Base64url(JSON)，且 type 为 tool_call 或 answer；'
                "请重新访问协议页核对格式后再次抓取。",
            )
        )

    if isinstance(env, ToolCallEnvelope):
        session.last_active = time.time()
        call, is_new = await session.pending.register(code, env.name, env.arguments)
        _telemetry(request, ctx, "push", code, envelope_summary(env))
        if is_new:
            ok = await ctx.hub.send(
                ToolCallNotify(
                    request_id=session.request_id or "",
                    session_key=session.session_key,
                    code=code,
                    call_id=call.call_id,
                    name=env.name,
                    arguments=env.arguments,
                )
            )
            if not ok:
                logger.error("tool_call 下发失败：② 未连接（call_id=%s）", call.call_id)
        else:
            logger.info("重复 tool_call 抓取，返回已受理（call_id=%s）", call.call_id)
        result_url = f"{ctx.settings.result_url(token)}?code={code}&call_id={call.call_id}"
        return HTMLResponse(pages.push_accepted_page(call.call_id, result_url))

    # answer 信封：分片重组
    content = await session.shard.add(code, env)
    _telemetry(request, ctx, "push", code, envelope_summary(env))
    if content is None:
        done, total = await session.shard.progress(code)
        return HTMLResponse(pages.push_recorded_page(done, total))
    session.docs_hint = ""
    session.turns += 1
    ok = await ctx.hub.send(
        AnswerNotify(
            request_id=session.request_id or "",
            session_key=session.session_key,
            code=code,
            content=content,
            seq=1,
            total=1,
            final=True,
        )
    )
    if not ok:
        logger.error("answer 下发失败：② 未连接（code=%s…）", code[:8])
    return HTMLResponse(pages.push_complete_page())


# ---------------------------------------------------------------- /result

@router.get("/{token}/result", response_class=HTMLResponse)
async def result(
    request: Request,
    token: str,
    code: str = Query(default=""),
    call_id: str = Query(default=""),
) -> HTMLResponse:
    ctx = _ctx(request)
    if token not in ctx.settings.valid_tokens:
        _telemetry(request, ctx, "result-bad-token", code)
        return HTMLResponse(pages.bad_token_page())
    session = ctx.manager.get_by_code(code)
    if session is None:
        _telemetry(request, ctx, "result-bad-code", code)
        return HTMLResponse(pages.result_missing_page("该 code 不对应任何活跃会话。"))
    call = session.pending.get(call_id)
    if call is None:
        _telemetry(request, ctx, "result-bad-call", code, call_id)
        return HTMLResponse(pages.result_missing_page("call_id 不存在或已过期。"))
    session.last_active = time.time()
    result_url = f"{ctx.settings.result_url(token)}?code={code}&call_id={call_id}"
    if call.consumed:
        _telemetry(request, ctx, "result-consumed", code, call_id)
        return HTMLResponse(pages.result_missing_page("该结果已被消费（一次性交付）。"))
    _telemetry(request, ctx, "result", code, call_id)
    content = await session.pending.wait_result(call_id, ctx.settings.poll_timeout)
    if content is None:
        return HTMLResponse(pages.result_not_ready_page(result_url))
    delivered = await session.pending.consume(call_id)
    if delivered is None:
        return HTMLResponse(pages.result_missing_page("该结果已被消费（一次性交付）。"))
    truncated = False
    if len(delivered.encode("utf-8")) > ctx.settings.result_max_bytes:
        delivered = delivered.encode("utf-8")[: ctx.settings.result_max_bytes].decode(
            "utf-8", errors="ignore"
        )
        truncated = True
    return HTMLResponse(pages.result_page(delivered, truncated))


# ---------------------------------------------------------------- /probe

@router.get("/{token}/probe", response_class=HTMLResponse)
async def probe(request: Request, token: str, nonce: str = Query(default="")) -> HTMLResponse:
    ctx = _ctx(request)
    ip = _client_ip(request)
    ua = request.headers.get("user-agent", "")
    ctx.audit.record(ip=ip, ua=ua, path=request.url.path, code="", kind="probe", summary=nonce)
    logger.info("探针抓取 ip=%s ua=%s nonce=%s", ip, ua[:60], nonce)
    if token not in ctx.settings.valid_tokens:
        return HTMLResponse(pages.bad_token_page())
    return HTMLResponse(pages.probe_page(nonce, ip, ua))
