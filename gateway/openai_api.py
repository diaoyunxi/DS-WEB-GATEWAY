"""②Gateway：对调用方的 OpenAI 兼容端点。

- POST /v1/chat/completions：兼容 messages/tools/tool_choice/stream/user；
  stream=true 时输出标准 SSE chunk（role delta、tool_calls delta、content、finish_reason）；
- GET /v1/models：返回 deepseek-web；
- 鉴权：调用方 Bearer API Key（GATEWAY_API_KEYS 为空则不鉴权）；
- ②无状态化：所有会话状态在①，②重启不丢任务。

工具循环（方案 §2.2）：模型抓 /push(tool_call) → ①推② → ②以 SSE 下发
tool_calls + finish_reason="tool_calls" → 调用方本地执行 → 下一轮请求携带
role:tool → ②经 WS 发 tool_result → ①唤醒 /result → 模型同轮继续。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from common.config import get_settings
from common.schemas import (
    AnswerNotify,
    ChatAck,
    ChatMessage,
    ErrorNotify,
    StopMessage,
    ToolCallNotify,
    ToolResultMessage,
)
from gateway.render import extract_images, render_full, render_incremental, trailing_tool_results
from gateway.ws_client import get_ws_client

logger = logging.getLogger("gateway.api")

router = APIRouter()

_MAX_IMAGE_DOWNLOAD = 10 * 1024 * 1024
_IMAGE_DOWNLOAD_TIMEOUT = 20.0


class ChatCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str = "deepseek-web"
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None
    stream: bool = False
    user: str | None = None


class UpstreamError(Exception):
    """①侧错误，以 502 返回调用方。"""


# ---------------------------------------------------------------- 鉴权

def verify_api_key(request: Request) -> None:
    settings = get_settings()
    keys = settings.api_keys
    if not keys:
        return
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token not in keys:
        raise HTTPException(status_code=401, detail="API Key 无效")


# ---------------------------------------------------------------- 图片代下载

async def _to_data_uri(url: str) -> str | None:
    """http(s) 图片代下载转 base64 data URI；data URI 原样返回。"""
    if url.startswith("data:"):
        return url
    if not url.startswith(("http://", "https://")):
        return None
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(url, timeout=_IMAGE_DOWNLOAD_TIMEOUT)
            resp.raise_for_status()
            content = resp.content
            if len(content) > _MAX_IMAGE_DOWNLOAD:
                logger.warning("代下载图片超过 %.1fMB，跳过", _MAX_IMAGE_DOWNLOAD / 1e6)
                return None
            mime = resp.headers.get("content-type", "image/png").split(";")[0].strip()
            if mime not in ("image/png", "image/jpeg", "image/webp"):
                logger.warning("代下载图片类型不支持: %s", mime)
                return None
            return f"data:{mime};base64,{base64.b64encode(content).decode('ascii')}"
    except Exception as exc:  # noqa: BLE001
        logger.warning("代下载图片失败 %s: %s", url[:80], exc)
        return None


# ---------------------------------------------------------------- SSE 工具

def _chunk(chunk_id: str, model: str, delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _completion(chunk_id: str, model: str, content: str, tool_calls: list[dict] | None,
                finish_reason: str) -> dict:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": chunk_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


# ---------------------------------------------------------------- 主端点

@router.post("/v1/chat/completions")
async def chat_completions(
    request: Request, body: ChatCompletionRequest, _: None = Depends(verify_api_key)
):
    settings = get_settings()
    ws = get_ws_client()
    if not ws.connected and not await ws.wait_connected(5.0):
        raise HTTPException(status_code=503, detail="与①的 WS 未连接，请稍后重试")

    session_key = body.user or request.headers.get("x-ds-session") or "default"
    request_id = uuid.uuid4().hex
    queue = ws.register(request_id)
    chunk_id = f"chatcmpl-{request_id[:24]}"
    model = settings.gateway_model_name

    try:
        tool_results = trailing_tool_results(body.messages)
        if tool_results:
            # 工具结果续轮：只回传结果，不注入新消息
            for call_id, content in tool_results:
                if not call_id:
                    raise HTTPException(status_code=400, detail="role:tool 消息缺少 tool_call_id")
                await ws.send(
                    ToolResultMessage(
                        request_id=request_id,
                        session_key=session_key,
                        call_id=call_id,
                        content=content,
                    )
                )
        else:
            images = extract_images(body.messages)
            data_uris: list[str] = []
            for url in images[: settings.max_images_per_turn]:
                uri = await _to_data_uri(url)
                if uri:
                    data_uris.append(uri)
            await ws.send(
                ChatMessage(
                    request_id=request_id,
                    session_key=session_key,
                    prompt=render_incremental(body.messages),
                    full_prompt=render_full(body.messages, settings.replay_max_chars),
                    tools=body.tools or [],
                    images=data_uris,
                    web_search=True,
                )
            )
    except ConnectionError as exc:
        ws.unregister(request_id)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except HTTPException:
        ws.unregister(request_id)
        raise

    if body.stream:
        return StreamingResponse(
            _stream(request_id, session_key, queue, chunk_id, model),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- 非流式 ----
    try:
        outcome = await _pump(request_id, queue)
    except UpstreamError as exc:
        ws.unregister(request_id)
        return JSONResponse(
            {"error": {"message": str(exc), "type": "upstream_error", "code": 502}},
            status_code=502,
        )
    ws.unregister(request_id)

    if isinstance(outcome, ToolCallNotify):
        tool_calls = [
            {
                "id": outcome.call_id,
                "type": "function",
                "function": {
                    "name": outcome.name,
                    "arguments": json.dumps(outcome.arguments, ensure_ascii=False),
                },
            }
        ]
        return JSONResponse(
            _completion(chunk_id, model, "", tool_calls, finish_reason="tool_calls")
        )
    return JSONResponse(_completion(chunk_id, model, outcome.content, None, finish_reason="stop"))


async def _pump(request_id: str, queue: asyncio.Queue):
    """等待本轮结果：tool_call（finish=tool_calls）或 answer（finish=stop）。"""
    settings = get_settings()
    ws = get_ws_client()
    try:
        while True:
            msg = await asyncio.wait_for(queue.get(), timeout=settings.request_timeout)
            if isinstance(msg, ChatAck):
                if msg.error:
                    raise UpstreamError(msg.error)
                continue
            if isinstance(msg, ErrorNotify):
                raise UpstreamError(msg.message)
            if isinstance(msg, (ToolCallNotify, AnswerNotify)):
                return msg
    except asyncio.TimeoutError as exc:
        await ws.send(StopMessage(request_id=request_id))
        raise UpstreamError("等待①响应超时") from exc
    except UpstreamError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError(str(exc)) from exc


async def _stream(
    request_id: str,
    session_key: str,
    queue: asyncio.Queue,
    chunk_id: str,
    model: str,
):
    ws = get_ws_client()
    finished = False
    try:
        yield _chunk(chunk_id, model, {"role": "assistant"})
        try:
            outcome = await _pump(request_id, queue)
        except UpstreamError as exc:
            yield _chunk(chunk_id, model, {"content": f"[网关错误] {exc}"})
            yield _chunk(chunk_id, model, {}, finish_reason="stop")
            yield "data: [DONE]\n\n"
            finished = True
            return
        if isinstance(outcome, ToolCallNotify):
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": outcome.call_id,
                        "type": "function",
                        "function": {
                            "name": outcome.name,
                            "arguments": json.dumps(outcome.arguments, ensure_ascii=False),
                        },
                    }
                ]
            }
            yield _chunk(chunk_id, model, delta)
            yield _chunk(chunk_id, model, {}, finish_reason="tool_calls")
        else:
            yield _chunk(chunk_id, model, {"content": outcome.content})
            yield _chunk(chunk_id, model, {}, finish_reason="stop")
        yield "data: [DONE]\n\n"
        finished = True
    finally:
        if not finished:
            # 调用方断开 SSE → 通知①停止并复位线程
            try:
                await ws.send(StopMessage(request_id=request_id, session_key=session_key))
            except Exception:  # noqa: BLE001
                pass
        ws.unregister(request_id)


@router.get("/v1/models")
async def list_models(_: None = Depends(verify_api_key)) -> dict:
    settings = get_settings()
    return {
        "object": "list",
        "data": [
            {
                "id": settings.gateway_model_name,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "ds-web-gateway",
            }
        ],
    }
