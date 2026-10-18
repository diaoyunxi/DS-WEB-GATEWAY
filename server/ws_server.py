"""①服务端：WS 服务（②主动拨入①，共享密钥鉴权，心跳 30s）。

控制面消息：
- ②→①：hello/auth、chat、tool_result、stop、ping
- ①→②：chat_ack、tool_call、answer、error、pong
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from common.schemas import (
    ChatAck,
    ChatMessage,
    ErrorNotify,
    HelloAuth,
    PingMessage,
    PongMessage,
    StopMessage,
    ToolResultMessage,
    parse_gateway_message,
)

from .browser import ThreadSuspended
from .sessions import CapacityError, save_session_images

logger = logging.getLogger("server.ws")

router = APIRouter()


class WSHub:
    """②↔① 的唯一活跃连接（②重连时顶掉旧连接）。"""

    def __init__(self) -> None:
        self._ws: WebSocket | None = None

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def attach(self, ws: WebSocket) -> None:
        if self._ws is not None and self._ws is not ws:
            try:
                await self._ws.close(4000, "replaced")
            except Exception:  # noqa: BLE001
                pass
        self._ws = ws

    def detach(self, ws: WebSocket) -> None:
        if self._ws is ws:
            self._ws = None

    async def send(self, msg) -> bool:
        if self._ws is None:
            return False
        try:
            await self._ws.send_text(msg.model_dump_json())
            return True
        except Exception:  # noqa: BLE001
            self._ws = None
            return False


def _ctx(ws: WebSocket):
    return ws.app.state.ctx


@router.websocket("/{token}/ws")
async def ws_endpoint(token: str, websocket: WebSocket) -> None:
    ctx = _ctx(websocket)
    if token not in ctx.settings.valid_tokens:
        await websocket.close(code=4401, reason="invalid token")
        return
    await websocket.accept()

    # ---- 鉴权：首条消息必须是 hello + 共享密钥 ----
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        msg = parse_gateway_message(json.loads(raw))
    except Exception:  # noqa: BLE001
        await websocket.close(code=4401, reason="auth failed")
        return
    if not isinstance(msg, HelloAuth) or msg.secret != ctx.settings.ws_secret:
        await websocket.close(code=4401, reason="bad secret")
        return

    await ctx.hub.attach(websocket)
    logger.info("② 已拨入 WS（token=%s…）", token[:8])

    # ---- 心跳 ----
    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(ctx.settings.ws_heartbeat)
            if not await ctx.hub.send(PingMessage()):
                return

    hb_task = asyncio.create_task(heartbeat())
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = parse_gateway_message(json.loads(raw))
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS 消息解析失败: %s", exc)
                continue
            if isinstance(msg, PingMessage):
                await ctx.hub.send(PongMessage())
            elif isinstance(msg, ChatMessage):
                asyncio.create_task(_handle_chat(ctx, msg))
            elif isinstance(msg, ToolResultMessage):
                await _handle_tool_result(ctx, msg)
            elif isinstance(msg, StopMessage):
                await _handle_stop(ctx, msg)
    except WebSocketDisconnect:
        logger.info("② 断开 WS")
    except Exception as exc:  # noqa: BLE001
        logger.warning("WS 连接异常: %s", exc)
    finally:
        hb_task.cancel()
        ctx.hub.detach(websocket)


# ---------------------------------------------------------------- 消息处理

async def _handle_chat(ctx, msg: ChatMessage) -> None:
    try:
        session, created = await ctx.manager.get_or_create(msg.session_key, msg.tools)
    except CapacityError as exc:
        await ctx.hub.send(
            ErrorNotify(request_id=msg.request_id, session_key=msg.session_key, message=str(exc))
        )
        return
    except Exception as exc:  # noqa: BLE001
        logger.exception("创建上下文失败")
        await ctx.alerts.alert("context-error", "创建上下文失败", str(exc))
        await ctx.hub.send(
            ErrorNotify(request_id=msg.request_id, session_key=msg.session_key, message=str(exc))
        )
        return

    await ctx.hub.send(
        ChatAck(request_id=msg.request_id, code=session.code, created=created)
    )

    async def run() -> None:
        async with session.lock:
            if session.state == "suspended":
                await ctx.hub.send(
                    ErrorNotify(
                        request_id=msg.request_id,
                        session_key=session.session_key,
                        code=session.code,
                        message=f"线程已挂起：{session.suspend_reason or '风控'}，请人工介入",
                    )
                )
                return
            session.request_id = msg.request_id
            session.state = "generating"
            # 骨架 prompt：固定一句，指向动态协议页（首轮必注入；长线程每 N 轮重注入）
            skeleton = (
                "开始任务前，请先访问 "
                f"{ctx.settings.docs_url(ctx.settings.path_token, session.code)} "
                "获取操作协议与工具说明，并严格遵守。"
            )
            # 新建上下文（或曾被驱逐）→ 重放模式全文；否则只注入增量
            text = msg.full_prompt if created else msg.prompt
            if created or session.turns % ctx.settings.skeleton_every_n_turns == 0:
                text = f"{skeleton}\n\n{text}"
            try:
                if msg.images:
                    paths = save_session_images(session, msg.images, ctx.settings)
                    if paths:
                        ok = await session.driver.upload_images(paths)
                        if not ok:
                            logger.warning("图片上传未确认完成（code=%s…）", session.code[:8])
                if msg.web_search:
                    await session.driver.ensure_web_search()
                await session.driver.input_text(text)
                await session.driver.submit_with_fallback()
                await session.driver.wait_generation()
                session.turns += 1
                session.state = "idle"
                session.last_active = time.time()
            except ThreadSuspended as exc:
                session.state = "suspended"
                session.suspend_reason = str(exc)
                await ctx.hub.send(
                    ErrorNotify(
                        request_id=msg.request_id,
                        session_key=session.session_key,
                        code=session.code,
                        message=f"线程挂起：{exc}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                session.state = "idle"
                logger.exception("对话轮执行失败")
                shot = await ctx.engine.screenshot(session.page, "turn-error")
                await ctx.alerts.alert("turn-error", "对话轮执行失败", f"{exc} shot={shot}")
                await ctx.hub.send(
                    ErrorNotify(
                        request_id=msg.request_id,
                        session_key=session.session_key,
                        code=session.code,
                        message=f"对话轮执行失败：{exc}",
                    )
                )

    session.gen_task = asyncio.create_task(run())


async def _handle_tool_result(ctx, msg: ToolResultMessage) -> None:
    session = ctx.manager.get_by_key(msg.session_key)
    if session is None:
        await ctx.hub.send(
            ErrorNotify(
                request_id=msg.request_id,
                session_key=msg.session_key,
                message="会话不存在（可能已被驱逐），请重新发起请求",
            )
        )
        return
    # 后续 answer/tool_call 归属本轮 HTTP 请求
    session.request_id = msg.request_id
    content = msg.content[: ctx.settings.result_max_bytes]
    ok = await session.pending.set_result(msg.call_id, content)
    if not ok:
        await ctx.hub.send(
            ErrorNotify(
                request_id=msg.request_id,
                session_key=msg.session_key,
                code=session.code,
                message=f"call_id {msg.call_id} 不存在或已被消费",
            )
        )
        return
    logger.info("工具结果已交付 call_id=%s（code=%s…）", msg.call_id, session.code[:8])


async def _handle_stop(ctx, msg: StopMessage) -> None:
    session = ctx.manager.get_by_key(msg.session_key)
    if session is None:
        return
    if session.gen_task is not None and not session.gen_task.done():
        session.gen_task.cancel()
    try:
        await session.driver.stop_generation()
    except Exception as exc:  # noqa: BLE001
        logger.warning("停止生成异常: %s", exc)
    session.state = "idle"
    session.request_id = None
    logger.info("线程已停止并复位（code=%s…）", session.code[:8])
