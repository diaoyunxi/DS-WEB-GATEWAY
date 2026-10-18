"""②Gateway：WS 客户端（②主动拨入①，指数退避重连，心跳 30s）。

- 连接即发 hello/auth（共享密钥）；
- 每个 HTTP 请求登记 request_id → asyncio.Queue，①的消息按 request_id 派发；
- 断线时全部在等请求收到 error 事件，退避重连后②无状态恢复。
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets

from common.config import get_settings
from common.schemas import (
    ErrorNotify,
    HelloAuth,
    PingMessage,
    PongMessage,
    parse_server_message,
)

logger = logging.getLogger("gateway.ws")

ws_client: "GatewayWSClient | None" = None


def get_ws_client() -> "GatewayWSClient":
    global ws_client
    if ws_client is None:
        ws_client = GatewayWSClient(get_settings())
    return ws_client


class GatewayWSClient:
    def __init__(self, settings) -> None:
        self._settings = settings
        self._ws = None
        self._waiters: dict[str, asyncio.Queue] = {}
        self._connected = asyncio.Event()
        self._closing = False
        self._task: asyncio.Task | None = None

    # ---------------------------------------------------------------- 生命周期

    async def start(self) -> None:
        self._closing = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._closing = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._task is not None:
            self._task.cancel()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self, timeout: float = 10.0) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------------------------------------------------------------- 连接循环

    async def _run(self) -> None:
        backoff = 1.0
        url = self._settings.resolved_ws_url.replace("{token}", self._settings.path_token)
        while not self._closing:
            try:
                logger.info("拨入① WS: %s…", url[:60])
                async with websockets.connect(url, ping_interval=None) as ws:
                    self._ws = ws
                    await ws.send(HelloAuth(secret=self._settings.ws_secret).model_dump_json())
                    self._connected.set()
                    backoff = 1.0
                    logger.info("WS 已连接")
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        async for raw in ws:
                            self._dispatch(raw)
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("WS 连接失败/中断: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_waiters("与①的 WS 连接中断，请稍后重试")
            if not self._closing:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _heartbeat(self, ws) -> None:
        while True:
            await asyncio.sleep(self._settings.ws_heartbeat)
            await ws.send(PingMessage().model_dump_json())

    def _dispatch(self, raw: str) -> None:
        try:
            msg = parse_server_message(json.loads(raw))
        except Exception as exc:  # noqa: BLE001
            logger.warning("WS 消息解析失败: %s", exc)
            return
        if isinstance(msg, PongMessage):
            return
        queue = self._waiters.get(msg.request_id)
        if queue is not None:
            queue.put_nowait(msg)
        else:
            logger.debug("无等待方，丢弃消息 type=%s request_id=%s", msg.type, msg.request_id)

    def _fail_waiters(self, message: str) -> None:
        for request_id, queue in list(self._waiters.items()):
            queue.put_nowait(
                ErrorNotify(request_id=request_id, message=message)
            )

    # ---------------------------------------------------------------- 请求方 API

    def register(self, request_id: str) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._waiters[request_id] = queue
        return queue

    def unregister(self, request_id: str) -> None:
        self._waiters.pop(request_id, None)

    async def send(self, msg) -> None:
        if self._ws is None:
            raise ConnectionError("与①的 WS 未连接")
        await self._ws.send(msg.model_dump_json())
