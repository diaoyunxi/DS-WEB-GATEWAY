"""①会话管理单测：cookies 加载、data URI 解析、图片落盘、SessionManager 生命周期。

运行：pytest pytests/test_sessions_units.py -q
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from common.config import Settings
from server.alerts import AlertManager
from server.envelopes import ShardBuffer
from server.pending import PendingCallRegistry
from server.sessions import (
    CapacityError,
    SessionManager,
    load_cookies,
    parse_data_uri,
    save_session_images,
)


def _settings(**kwargs) -> Settings:
    base = dict(path_token="tok-abcdefghijklmnop", ws_secret="s", data_dir="data-test")
    base.update(kwargs)
    return Settings(**base)


# ---------------------------------------------------------------- cookies

def test_load_cookies_from_file(tmp_path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        json.dumps([{"name": "a", "value": "1"}]), encoding="utf-8"
    )
    cookies = load_cookies(_settings(cookies_file=str(cookie_file)))
    assert cookies == [{"name": "a", "value": "1", "domain": ".deepseek.com", "path": "/"}]


def test_load_cookies_missing_file(caplog):
    assert load_cookies(_settings(cookies_file="no-such-file.json")) == []
    assert "cookies_file 不存在" in caplog.text


def test_load_cookies_inline():
    cookies = load_cookies(_settings(cookies_json='[{"name":"a","value":"1","domain":".x.com"}]'))
    assert cookies[0]["domain"] == ".x.com"  # 已有 domain 不覆盖
    assert cookies[0]["path"] == "/"


def test_load_cookies_invalid_inputs():
    assert load_cookies(_settings()) == []  # 两者皆空
    assert load_cookies(_settings(cookies_json="  ")) == []
    assert load_cookies(_settings(cookies_json="{不是JSON")) == []  # 解析失败
    assert load_cookies(_settings(cookies_json='{"name":"a"}')) == []  # 非数组
    assert load_cookies(_settings(cookies_json='[{"value":"1"},{"name":"b"},"x"]')) == []


# ---------------------------------------------------------------- data URI

def test_parse_data_uri_valid():
    uri = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()
    mime, raw = parse_data_uri(uri)
    assert mime == "image/png" and raw == b"\x89PNG fake"
    # 允许 base64 中含空白（换行/空格，re.sub 清除后再解码）
    mime2, raw2 = parse_data_uri("data:image/jpeg;base64,an\n  Bn")
    assert mime2 == "image/jpeg" and raw2 == b"jpg"


def test_parse_data_uri_invalid():
    assert parse_data_uri("data:text/plain;base64,x") is None  # 不支持的 MIME
    assert parse_data_uri("https://example.com/a.png") is None
    assert parse_data_uri("data:image/png;base64,!!!not-base64!!!") is None  # 解码失败


# ---------------------------------------------------------------- 图片落盘

def test_save_session_images(tmp_path):
    settings = _settings(max_images_per_turn=2, max_image_bytes=1024)
    session = SimpleNamespace(images_dir=str(tmp_path))
    png = "data:image/png;base64," + base64.b64encode(b"png-bytes").decode()
    jpeg = "data:image/jpeg;base64," + base64.b64encode(b"jpg-bytes").decode()
    bad = "data:text/plain;base64,AAAA"
    huge = "data:image/webp;base64," + base64.b64encode(b"x" * 2048).decode()
    paths = save_session_images(session, [png, jpeg, bad, huge, png], settings)
    assert len(paths) == 2  # bad/huge 跳过；第 5 张超每轮上限
    assert all(Path(p).is_file() for p in paths)
    assert Path(paths[0]).read_bytes() == b"png-bytes"
    assert Path(paths[1]).read_bytes() == b"jpg-bytes"
    assert Path(paths[0]).suffix == ".png" and Path(paths[1]).suffix == ".jpg"
    # 上限截断告警路径（3 张有效 > 2）
    save_session_images(session, [png, jpeg, "data:image/webp;base64," + base64.b64encode(b"w").decode()], settings)


def test_save_session_images_skips_all_invalid(tmp_path):
    settings = _settings(max_images_per_turn=5, max_image_bytes=1024)
    session = SimpleNamespace(images_dir=str(tmp_path))
    assert save_session_images(session, ["not-a-uri"], settings) == []


# ---------------------------------------------------------------- SessionManager

class _FakeLocator:
    def __init__(self, count: int = 1) -> None:
        self._count = count

    async def count(self) -> int:
        return self._count


class _FakePage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)

    def locator(self, selector: str):
        return _FakeLocator(1)


class _FakeContext:
    def __init__(self) -> None:
        self.closed = False
        self.page = _FakePage()

    async def new_page(self):
        return self.page

    async def close(self) -> None:
        self.closed = True


class _FakeEngine:
    def __init__(self) -> None:
        self.contexts: list[_FakeContext] = []
        self.cookie_log: list[list] = []

    async def new_context(self, cookies: list[dict]):
        self.cookie_log.append(cookies)
        ctx = _FakeContext()
        self.contexts.append(ctx)
        return ctx


def _manager(**kwargs) -> tuple[SessionManager, _FakeEngine]:
    settings = _settings(**kwargs)
    engine = _FakeEngine()
    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None
    manager = SessionManager(engine, settings, alerts, ShardBuffer(60), PendingCallRegistry(60))
    return manager, engine


def test_get_or_create_creates_and_reuses():
    async def scenario():
        manager, engine = _manager(max_contexts=2)
        session, created = await manager.get_or_create("u1", tools=[{"name": "t"}])
        assert created and session.code and session.session_key == "u1"
        assert session.images_dir and Path(session.images_dir).is_dir()
        assert session.page.goto_calls  # goto_home 已执行
        assert manager.get_by_code(session.code) is session
        assert manager.get_by_key("u1") is session
        # 同 key 复用：tools 更新、last_active 刷新、不新建上下文
        time.sleep(0.01)
        same, created2 = await manager.get_or_create("u1", tools=[{"name": "t2"}])
        assert not created2 and same is session
        assert session.tools == [{"name": "t2"}]
        assert len(engine.contexts) == 1
        # 空 tools 不清空已有 tools
        await manager.get_or_create("u1", tools=[])
        assert session.tools == [{"name": "t2"}]

    asyncio.run(scenario())


def test_get_or_create_capacity_error():
    async def scenario():
        manager, engine = _manager(max_contexts=1, acquire_timeout=0.2)
        await manager.get_or_create("u1", [])
        with pytest.raises(CapacityError, match="上限"):
            await manager.get_or_create("u2", [])
        assert len(engine.contexts) == 1

    asyncio.run(scenario())


def test_evict_releases_capacity_and_cleans_up():
    async def scenario():
        manager, engine = _manager(max_contexts=1, acquire_timeout=0.2)
        session, _ = await manager.get_or_create("u1", [])
        # 放一个文件验证 images_dir 被清理
        marker = Path(session.images_dir) / "a.png"
        marker.write_bytes(b"x")
        await manager.evict(session.code, "test")
        assert manager.get_by_code(session.code) is None
        assert manager.get_by_key("u1") is None
        assert engine.contexts[0].closed
        assert not Path(session.images_dir).exists()
        # 信号量已释放 → 可以再建
        session2, created2 = await manager.get_or_create("u2", [])
        assert created2 and session2.code != session.code

    asyncio.run(scenario())


def test_evict_unknown_code_is_noop():
    async def scenario():
        manager, _ = _manager()
        await manager.evict("no-such-code")  # 不抛异常

    asyncio.run(scenario())


def test_manager_stats():
    async def scenario():
        manager, _ = _manager(max_contexts=3)
        session, _ = await manager.get_or_create("u1", [{"name": "t"}])
        stats = manager.stats()
        assert stats["contexts"] == 1 and stats["max_contexts"] == 3
        assert stats["sessions"][0]["code"] == session.code
        assert stats["sessions"][0]["tools"] == 1

    asyncio.run(scenario())


def test_sweep_images_ttl(tmp_path):
    settings = _settings(data_dir=str(tmp_path), image_ttl=1)
    engine = _FakeEngine()
    alerts = AlertManager(settings)
    alerts._send_mail = lambda *a, **k: None
    manager = SessionManager(engine, settings, alerts, ShardBuffer(60), PendingCallRegistry(60))
    root = tmp_path / "images" / "c1"
    root.mkdir(parents=True)
    old = root / "old.png"
    old.write_bytes(b"x")
    fresh = root / "fresh.png"
    fresh.write_bytes(b"x")
    past = time.time() - 3600
    os.utime(old, (past, past))
    (tmp_path / "images" / "loose-file.txt").write_text("not-a-dir")  # 非目录跳过
    manager._sweep_images()
    assert not old.exists() and fresh.exists()
    # 根目录不存在时静默返回
    import shutil

    shutil.rmtree(tmp_path / "images")
    manager._sweep_images()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
