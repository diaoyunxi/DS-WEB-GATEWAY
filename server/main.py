"""部分① Server 入口：公网面 + 管理面 + 浏览器集群。

启动：
    python -m server.main

监听：
- 公网面：SERVER_BIND_HOST:SERVER_BIND_PORT（经 Cloudflare Tunnel 暴露）
- 管理面：ADMIN_BIND_HOST:ADMIN_BIND_PORT（仅内网）
"""
from __future__ import annotations

import asyncio
import logging

import uvicorn
from fastapi import FastAPI

from common.config import get_settings
from server.admin_api import router as admin_router
from server.alerts import AlertManager
from server.audit import AuditLog
from server.browser import BrowserEngine
from server.context import ServerContext
from server.envelopes import ShardBuffer
from server.pending import PendingCallRegistry
from server.public_api import router as public_router
from server.ratelimit import RateLimiter
from server.sessions import SessionManager
from server.ws_server import WSHub, router as ws_router

logger = logging.getLogger("server.main")


def build_app(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title="DS-Web-Gateway Server (①)", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.include_router(public_router)
    app.include_router(ws_router)
    return app


def build_admin_app(ctx: ServerContext) -> FastAPI:
    app = FastAPI(title="DS-Web-Gateway Server Admin (①/内网)", docs_url=None, redoc_url=None)
    app.state.ctx = ctx
    app.include_router(admin_router)
    return app


async def serve() -> None:
    logging.basicConfig(
        level=getattr(logging, get_settings().log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    path_token, ws_secret = settings.ensure_secrets()
    logger.info("能力 URL 基址: %s", settings.external_base_url)
    logger.info("path_token = %s", path_token)
    logger.info("ws_secret  = %s", ws_secret)

    alerts = AlertManager(settings)
    engine = BrowserEngine(settings, alerts)
    try:
        await engine.start()
    except Exception as exc:  # noqa: BLE001
        await alerts.alert(
            "browser-start",
            "浏览器启动失败",
            f"{exc}（请确认已执行 pip install playwright && playwright install chromium）",
        )
        raise

    shard = ShardBuffer(settings.shard_buffer_ttl)
    pending = PendingCallRegistry(settings.pending_call_ttl)
    ctx = ServerContext(
        settings=settings,
        engine=engine,
        manager=SessionManager(engine, settings, alerts, shard, pending),
        hub=WSHub(),
        alerts=alerts,
        audit=AuditLog(),
        limiter=RateLimiter(settings.global_qps, settings.per_code_qps, settings.rate_limit_burst),
        shard=shard,
        pending=pending,
    )

    cleanup_task = asyncio.create_task(ctx.manager.cleanup_loop())

    public_app = build_app(ctx)
    admin_app = build_admin_app(ctx)
    public_server = uvicorn.Server(
        uvicorn.Config(
            public_app,
            host=settings.server_bind_host,
            port=settings.server_bind_port,
            log_level=settings.log_level.lower(),
        )
    )
    admin_server = uvicorn.Server(
        uvicorn.Config(
            admin_app,
            host=settings.admin_bind_host,
            port=settings.admin_bind_port,
            log_level=settings.log_level.lower(),
        )
    )
    try:
        await asyncio.gather(public_server.serve(), admin_server.serve())
    finally:
        cleanup_task.cancel()
        await engine.stop()


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
