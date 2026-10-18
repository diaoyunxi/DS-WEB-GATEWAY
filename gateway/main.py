"""部分② Gateway 入口：OpenAI 兼容端点（对调用方）。

启动：
    python -m gateway.main
或：
    uvicorn gateway.main:app --host 127.0.0.1 --port 8080
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from common.config import get_settings
from gateway.openai_api import router
from gateway.ws_client import get_ws_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    await get_ws_client().start()
    yield
    await get_ws_client().stop()


app = FastAPI(title="DS-Web-Gateway Gateway (②)", lifespan=lifespan)
app.include_router(router)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    uvicorn.run(
        app,
        host=settings.gateway_bind_host,
        port=settings.gateway_bind_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
