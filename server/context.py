"""①服务端：运行时单例容器（挂在 FastAPI app.state 上）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .alerts import AlertManager
from .audit import AuditLog
from .browser import BrowserEngine
from .envelopes import ShardBuffer
from .pending import PendingCallRegistry
from .ratelimit import RateLimiter
from .sessions import SessionManager


@dataclass
class ServerContext:
    settings: Any
    engine: BrowserEngine
    manager: SessionManager
    hub: Any                  # WSHub
    alerts: AlertManager
    audit: AuditLog
    limiter: RateLimiter
    shard: ShardBuffer
    pending: PendingCallRegistry
