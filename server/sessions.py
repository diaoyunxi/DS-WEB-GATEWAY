"""①服务端：会话管理（code → BrowserContext + page + 串行锁 + pending 暂存区）。

规则（见方案 §4）：
- 每 code 绑定一个 BrowserContext + 一个 page + 一把串行锁（同会话并发排队）；
- 上下文池 LRU：空闲 context_idle_ttl 可配置驱逐（驱逐 = 丢失网页线程连续性，
  下一次 chat 由 ② 以重放模式注入全文）；
- 并发上限 max_contexts（默认 3），超出排队 acquire_timeout 后报错；
- 图片写 per-session 临时目录（修复固定文件名并发串图），TTL 清理。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .browser import BrowserEngine, DeepSeekDriver
from .pending import PendingCallRegistry

logger = logging.getLogger("server.sessions")

_IMAGE_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_DATA_URI_RE = re.compile(r"^data:(image/(?:png|jpeg|webp));base64,([A-Za-z0-9+/=\s]+)$")


class CapacityError(Exception):
    """浏览器上下文达到并发上限。"""


@dataclass
class Session:
    code: str
    session_key: str
    context: Any
    page: Any
    driver: DeepSeekDriver
    lock: asyncio.Lock
    tools: list[dict] = field(default_factory=list)
    turns: int = 0
    created_at: float = 0.0
    last_active: float = 0.0
    state: str = "idle"  # idle / generating / suspended
    request_id: str | None = None
    suspend_reason: str | None = None
    docs_hint: str = ""  # 最近一次信封错误（渲染进 /docs 供模型自我纠正）
    images_dir: str = ""
    gen_task: asyncio.Task | None = None
    shard: Any = None       # ShardBuffer（全局共享，按 code 键控）
    pending: PendingCallRegistry | None = None


def load_cookies(settings) -> list[dict]:
    """从 cookies_file / cookies_json 读取 Playwright cookie 数组。"""
    raw = ""
    if settings.cookies_file:
        path = Path(settings.cookies_file)
        if path.is_file():
            raw = path.read_text(encoding="utf-8")
        else:
            logger.warning("cookies_file 不存在: %s", path)
    elif settings.cookies_json:
        raw = settings.cookies_json
    if not raw.strip():
        return []
    try:
        cookies = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("cookies JSON 解析失败: %s", exc)
        return []
    if not isinstance(cookies, list):
        logger.error("cookies 必须是数组")
        return []
    normalized = []
    for c in cookies:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        c = dict(c)
        c.setdefault("domain", ".deepseek.com")
        c.setdefault("path", "/")
        normalized.append(c)
    logger.info("已注入 %d 条 cookies", len(normalized))
    return normalized


def parse_data_uri(uri: str) -> tuple[str, bytes] | None:
    match = _DATA_URI_RE.match(uri.strip())
    if not match:
        return None
    try:
        return match.group(1), base64.b64decode(re.sub(r"\s+", "", match.group(2)))
    except Exception:  # noqa: BLE001
        return None


def save_session_images(session: Session, uris: list[str], settings) -> list[str]:
    """把 base64 data URI 写入 per-session 临时目录，返回本地路径列表。"""
    paths: list[str] = []
    for idx, uri in enumerate(uris[: settings.max_images_per_turn]):
        parsed = parse_data_uri(uri)
        if parsed is None:
            logger.warning("跳过第 %d 张图：不是受支持的图片 data URI", idx)
            continue
        mime, raw = parsed
        if len(raw) > settings.max_image_bytes:
            logger.warning("跳过第 %d 张图：超过 %.1fMB", idx, settings.max_image_bytes / 1e6)
            continue
        ext = _IMAGE_MIME_EXT[mime]
        path = Path(session.images_dir) / f"{int(time.time() * 1000)}-{idx}.{ext}"
        path.write_bytes(raw)
        paths.append(str(path))
    if len(uris) > settings.max_images_per_turn:
        logger.warning("图片数 %d 超过每轮上限 %d，已截断", len(uris), settings.max_images_per_turn)
    return paths


class SessionManager:
    def __init__(self, engine: BrowserEngine, settings, alerts, shard, pending) -> None:
        self._engine = engine
        self._settings = settings
        self._alerts = alerts
        self._shard = shard
        self._pending = pending
        self._sessions: dict[str, Session] = {}   # code -> session
        self._by_key: dict[str, str] = {}         # session_key -> code
        self._sem = asyncio.Semaphore(settings.max_contexts)

    # ---------------------------------------------------------------- 查询

    def get_by_code(self, code: str) -> Session | None:
        return self._sessions.get(code)

    def get_by_key(self, session_key: str) -> Session | None:
        code = self._by_key.get(session_key)
        return self._sessions.get(code) if code else None

    # ---------------------------------------------------------------- 创建/获取

    async def get_or_create(self, session_key: str, tools: list[dict]) -> tuple[Session, bool]:
        session = self.get_by_key(session_key)
        if session is not None:
            if tools:
                session.tools = tools
            session.last_active = time.time()
            return session, False
        try:
            await asyncio.wait_for(
                self._sem.acquire(), timeout=self._settings.acquire_timeout
            )
        except asyncio.TimeoutError as exc:
            raise CapacityError(
                f"浏览器上下文已达上限（{self._settings.max_contexts}），请稍后重试"
            ) from exc
        try:
            code = secrets.token_hex(16)  # 128bit 高熵
            cookies = load_cookies(self._settings)
            context = await self._engine.new_context(cookies)
            page = await context.new_page()
            driver = DeepSeekDriver(self._engine, self._settings, page)
            await driver.goto_home()
            images_dir = str(Path(self._settings.data_dir) / "images" / code)
            Path(images_dir).mkdir(parents=True, exist_ok=True)
            session = Session(
                code=code,
                session_key=session_key,
                context=context,
                page=page,
                driver=driver,
                lock=asyncio.Lock(),
                tools=tools or [],
                created_at=time.time(),
                last_active=time.time(),
                images_dir=images_dir,
                shard=self._shard,
                pending=self._pending,
            )
            self._sessions[code] = session
            self._by_key[session_key] = code
            logger.info("新上下文 code=%s… session_key=%s", code[:8], session_key)
            return session, True
        except Exception:
            self._sem.release()
            raise

    # ---------------------------------------------------------------- 驱逐/清理

    async def evict(self, code: str, reason: str = "") -> None:
        session = self._sessions.pop(code, None)
        if session is None:
            return
        if self._by_key.get(session.session_key) == code:
            del self._by_key[session.session_key]
        if session.gen_task is not None and not session.gen_task.done():
            session.gen_task.cancel()
        try:
            await session.context.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(session.images_dir, ignore_errors=True)
        self._sem.release()
        logger.info("上下文 %s… 已驱逐（%s），下次同会话将走重放模式", code[:8], reason)

    async def cleanup_loop(self) -> None:
        """LRU 驱逐 + 分片/pending/图片 TTL 清理。"""
        while True:
            try:
                await asyncio.sleep(30)
                now = time.time()
                for code, session in list(self._sessions.items()):
                    if session.state == "generating":
                        continue
                    if now - session.last_active > self._settings.context_idle_ttl:
                        await self.evict(code, "idle-ttl")
                self._shard.sweep()
                self._pending.sweep()
                self._sweep_images()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.error("清理循环异常: %s", exc)

    def _sweep_images(self) -> None:
        root = Path(self._settings.data_dir) / "images"
        if not root.is_dir():
            return
        now = time.time()
        for session_dir in root.iterdir():
            if not session_dir.is_dir():
                continue
            for f in session_dir.iterdir():
                try:
                    if now - f.stat().st_mtime > self._settings.image_ttl:
                        f.unlink(missing_ok=True)
                except OSError:
                    continue

    # ---------------------------------------------------------------- 统计

    def stats(self) -> dict:
        sessions = []
        for s in self._sessions.values():
            sessions.append(
                {
                    "code": s.code,
                    "session_key": s.session_key,
                    "state": s.state,
                    "turns": s.turns,
                    "created_at": s.created_at,
                    "last_active": s.last_active,
                    "tools": len(s.tools),
                    "suspend_reason": s.suspend_reason,
                    "docs_hint": s.docs_hint,
                }
            )
        return {
            "contexts": len(self._sessions),
            "max_contexts": self._settings.max_contexts,
            "sessions": sessions,
        }
