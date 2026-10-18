"""①服务端：管理面（仅内网监听，不进公网）。

活跃线程列表、线程状态、pending_call、风控告警、抓取遥测日志、
选择器自检、强制停止/驱逐、截图下载。可选 admin_token 头校验。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

logger = logging.getLogger("server.admin")

router = APIRouter(prefix="/admin")


def _ctx(request: Request):
    return request.app.state.ctx


def _check_admin(request: Request) -> None:
    ctx = _ctx(request)
    if ctx.settings.admin_token:
        token = request.headers.get("x-admin-token", "")
        if token != ctx.settings.admin_token:
            raise HTTPException(status_code=401, detail="admin token 无效")


@router.get("/stats")
async def stats(request: Request) -> dict:
    _check_admin(request)
    ctx = _ctx(request)
    return {
        "sessions": ctx.manager.stats(),
        "pending_calls": ctx.pending.stats(),
        "shard_buffer": ctx.shard.stats(),
        "rate_limit": ctx.limiter.stats(),
        "audit": ctx.audit.stats(),
        "ws_connected": ctx.hub.connected,
        "smtp_configured": ctx.alerts.smtp_configured,
    }


@router.get("/sessions")
async def sessions(request: Request) -> dict:
    _check_admin(request)
    return {"sessions": _ctx(request).manager.stats()["sessions"]}


@router.get("/pending")
async def pending(request: Request) -> dict:
    _check_admin(request)
    ctx = _ctx(request)
    calls = []
    for call in ctx.pending._calls.values():
        calls.append(
            {
                "call_id": call.call_id,
                "code": call.code,
                "name": call.name,
                "arguments": call.arguments,
                "created_at": call.created_at,
                "ready": call.event.is_set(),
                "consumed": call.consumed,
            }
        )
    return {"pending_calls": calls}


@router.get("/logs")
async def logs(request: Request, n: int = 100, code: str = "") -> dict:
    _check_admin(request)
    ctx = _ctx(request)
    return {
        "records": ctx.audit.recent(n=n, code=code or None),
        "ip_stats": ctx.audit.ip_stats(),
    }


@router.get("/alerts")
async def alerts(request: Request) -> dict:
    _check_admin(request)
    return {"alerts": _ctx(request).alerts.recent()}


@router.post("/sessions/{code}/stop")
async def stop_session(request: Request, code: str) -> dict:
    _check_admin(request)
    ctx = _ctx(request)
    session = ctx.manager.get_by_code(code)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if session.gen_task is not None and not session.gen_task.done():
        session.gen_task.cancel()
    try:
        await session.driver.stop_generation()
    except Exception as exc:  # noqa: BLE001
        logger.warning("停止异常: %s", exc)
    session.state = "idle"
    session.request_id = None
    return {"ok": True}


@router.post("/sessions/{code}/evict")
async def evict_session(request: Request, code: str) -> dict:
    _check_admin(request)
    ctx = _ctx(request)
    await ctx.manager.evict(code, "manual")
    return {"ok": True}


@router.post("/selector-check")
async def selector_check(request: Request) -> dict:
    """选择器自检：对每个活跃会话跑一遍（每日自检 / 网页改版时人工触发）。"""
    _check_admin(request)
    ctx = _ctx(request)
    reports: dict[str, dict] = {}
    for code, session in ctx.manager._sessions.items():
        try:
            reports[code] = await session.driver.check_selectors()
        except Exception as exc:  # noqa: BLE001
            reports[code] = {"error": str(exc)}
    return {"reports": reports}


@router.get("/screenshots/{filename}")
async def screenshot(request: Request, filename: str) -> FileResponse:
    _check_admin(request)
    path = Path(_ctx(request).settings.data_dir) / "screenshots" / Path(filename).name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(str(path), media_type="image/png")
