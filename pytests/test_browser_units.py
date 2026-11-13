"""①浏览器驱动单测：DeepSeekDriver 生命周期/输入/风控/上传 + BrowserEngine 桩路径。

全部用假 page/engine/alerts，不启动真实 Chromium。

运行：pytest pytests/test_browser_units.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from common.config import Settings
from server.browser import BrowserEngine, DeepSeekDriver, ThreadSuspended
from server.config import SELECTORS


class _FakeLocator:
    def __init__(self, page: "_FakePage", selector: str) -> None:
        self._page = page
        self._selector = selector

    @property
    def first(self):
        return self

    async def count(self) -> int:
        return self._page.count_for(self._selector)

    async def click(self, **kwargs) -> None:
        self._page.clicks.append(self._selector)

    async def get_attribute(self, name: str):
        return self._page.attrs.get((self._selector, name))


class _FakePage:
    """counts: selector -> 计数；attrs: (selector, attr) -> 值。"""

    def __init__(self, counts: dict | None = None, attrs: dict | None = None) -> None:
        self.counts = dict(counts or {})
        self.attrs = dict(attrs or {})
        self.clicks: list[str] = []
        self.typed: list[str] = []
        self.inserted: list[str] = []
        self.pressed: list[str] = []
        self.goto_calls: list[str] = []
        self.files_set: list[list] = []
        self.keyboard = SimpleNamespace(
            type=self._type, insert_text=self._insert, press=self._press
        )

    def count_for(self, selector: str) -> int:
        return self.counts.get(selector, 0)

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    async def goto(self, url: str, **kwargs) -> None:
        self.goto_calls.append(url)

    async def click(self, selector: str, **kwargs) -> None:
        self.clicks.append(selector)

    async def set_input_files(self, selector: str, paths) -> None:
        self.files_set.append(list(paths))

    async def screenshot(self, **kwargs) -> None:
        return None

    async def _type(self, text: str, delay: int = 0) -> None:
        self.typed.append(text)

    async def _insert(self, text: str) -> None:
        self.inserted.append(text)

    async def _press(self, key: str) -> None:
        self.pressed.append(key)


class _FakeAlerts:
    def __init__(self) -> None:
        self.alerts: list[tuple[str, str]] = []

    async def alert(self, kind: str, subject: str, body: str) -> bool:
        self.alerts.append((kind, subject))
        return True


class _FakeEngine:
    def __init__(self) -> None:
        self.screenshots: list[str] = []

    async def screenshot(self, page, name: str) -> str:
        self.screenshots.append(name)
        return f"/tmp/{name}.png"


def _settings(**kwargs) -> Settings:
    base = dict(path_token="tok", ws_secret="s", data_dir="data-test")
    base.update(kwargs)
    return Settings(**base)


def _driver(page: _FakePage, **kwargs):
    settings = _settings(**kwargs)
    engine = _FakeEngine()
    alerts = _FakeAlerts()
    return DeepSeekDriver(engine, settings, page), engine, alerts


INPUT_SEL = SELECTORS["input"][0]
STOP_SEL = SELECTORS["stop"][0]
DONE_SEL = SELECTORS["done"][0]
SEND_SEL = SELECTORS["send"][0]


# ---------------------------------------------------------------- goto_home

def test_goto_home_success():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1})
        driver, _, _ = _driver(page)
        await driver.goto_home()
        assert page.goto_calls and "deepseek.com" in page.goto_calls[0]

    asyncio.run(scenario())


def test_goto_home_missing_input_raises():
    async def scenario():
        page = _FakePage({})  # 输入框始终不出现
        driver, _, _ = _driver(generation_start_timeout=0.2)
        with pytest.raises(RuntimeError, match="输入框未出现"):
            await driver.goto_home()

    asyncio.run(scenario())


# ---------------------------------------------------------------- 输入

def test_input_text_short_types_human():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1})
        driver, _, _ = _driver(typing_max_chars=100, human_pause_probability=0.0,
                               typing_delay_min_ms=0, typing_delay_max_ms=0)
        await driver.input_text("你好")
        assert page.clicks == [INPUT_SEL]
        assert page.typed == ["你好"]
        assert not page.inserted

    asyncio.run(scenario())


def test_input_text_long_uses_insert():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1})
        driver, _, _ = _driver(typing_max_chars=10)
        await driver.input_text("x" * 50)
        assert page.inserted == ["x" * 50]
        assert not page.typed

    asyncio.run(scenario())


def test_input_text_no_input_raises():
    async def scenario():
        page = _FakePage({})
        driver, _, _ = _driver(generation_start_timeout=0.2)
        with pytest.raises(RuntimeError, match="找不到输入框"):
            await driver.input_text("hi")

    asyncio.run(scenario())


def test_type_human_multiline_uses_shift_enter():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1})
        driver, _, _ = _driver(typing_max_chars=1000, human_pause_probability=0.0,
                               typing_delay_min_ms=0, typing_delay_max_ms=0)
        await driver._type_human("第一行\n第二行")
        assert "".join(page.typed) == "第一行第二行"
        assert page.pressed == ["Shift+Enter"]  # 换行不提交


def test_submit_and_fallback_click():
    async def scenario():
        page = _FakePage({STOP_SEL: 1})
        driver, _, _ = _driver()
        await driver.submit()
        assert page.pressed == ["Enter"]

        page2 = _FakePage({SEND_SEL: 1})  # 无 stop → 回车未生效 → 点击发送
        driver2, _, _ = _driver2 = _driver(page2)
        await driver2.submit_with_fallback()
        assert page2.clicks == [SEND_SEL]

    asyncio.run(scenario())


def test_submit_fallback_click_failure_logged():
    async def scenario():
        page = _FakePage({SEND_SEL: 1})

        async def bad_click(**kwargs):
            raise RuntimeError("click failed")

        page.click = bad_click  # type: ignore[assignment]
        driver, _, _ = _driver(page)
        await driver.submit_with_fallback()  # 异常被吞掉只记日志

    asyncio.run(scenario())


# ---------------------------------------------------------------- 联网搜索

def test_ensure_web_search_aria_pressed_true():
    async def scenario():
        page = _FakePage({SELECTORS["web_search"][0]: 1},
                         {(SELECTORS["web_search"][0], "aria-pressed"): "true"})
        driver, _, _ = _driver(page)
        assert await driver.ensure_web_search() is True
        assert not page.clicks

    asyncio.run(scenario())


def test_ensure_web_search_clicks_when_off():
    async def scenario():
        page = _FakePage({SELECTORS["web_search"][0]: 1},
                         {(SELECTORS["web_search"][0], "aria-checked"): "false"})
        driver, _, _ = _driver(page)
        assert await driver.ensure_web_search() is True
        assert page.clicks == [SELECTORS["web_search"][0]]

    asyncio.run(scenario())


def test_ensure_web_search_class_marker():
    async def scenario():
        sel = SELECTORS["web_search"][0]
        page = _FakePage({sel: 1}, {(sel, "class"): "btn active"})
        driver, _, _ = _driver(page)
        assert await driver.ensure_web_search() is True
        assert not page.clicks

    asyncio.run(scenario())


def test_ensure_web_search_unknown_state_and_missing():
    async def scenario():
        sel = SELECTORS["web_search"][0]
        page = _FakePage({sel: 1})  # 无 aria/class 线索 → 无法确认
        driver, _, _ = _driver(page)
        assert await driver.ensure_web_search() is False

        page2 = _FakePage({})  # 选择器失效
        driver2, _, _ = _driver(page2)
        assert await driver2.ensure_web_search() is False

    asyncio.run(scenario())


def test_ensure_web_search_click_failure():
    async def scenario():
        sel = SELECTORS["web_search"][0]
        page = _FakePage({sel: 1}, {(sel, "aria-pressed"): "false"})

        class _Loc(_FakeLocator):
            async def click(self, **kwargs):
                raise RuntimeError("nope")

        page.locator = lambda s: _Loc(page, s)  # type: ignore[assignment]
        driver, _, _ = _driver(page)
        assert await driver.ensure_web_search() is False

    asyncio.run(scenario())


# ---------------------------------------------------------------- 风控

def test_check_risk_captcha_suspends():
    async def scenario():
        page = _FakePage({SELECTORS["risk_captcha"][0]: 1})
        driver, engine, alerts = _driver(page)
        with pytest.raises(ThreadSuspended, match="验证码"):
            await driver._check_risk()
        assert alerts.alerts[0][0] == "risk-captcha"
        assert engine.screenshots == ["risk-captcha"]

    asyncio.run(scenario())


def test_check_risk_frequent_window():
    async def scenario():
        page = _FakePage({SELECTORS["frequent"][0]: 1})
        driver, _, alerts = _driver(page)
        with pytest.raises(ThreadSuspended, match="访问频繁"):
            await driver._check_risk(frequent_until=time.monotonic() + 10)
        assert alerts.alerts[0][0] == "risk-frequent"

    asyncio.run(scenario())


def test_check_risk_frequent_after_window_ignored():
    async def scenario():
        page = _FakePage({SELECTORS["frequent"][0]: 1})
        driver, _, _ = _driver(page)
        await driver._check_risk(frequent_until=time.monotonic() - 1)  # 窗口外不检测

    asyncio.run(scenario())


def test_check_risk_login_wall_when_input_gone():
    async def scenario():
        page = _FakePage({SELECTORS["login_wall"][0]: 1})  # input 消失 + 登录墙出现
        driver, _, alerts = _driver(page)
        with pytest.raises(ThreadSuspended, match="登录墙"):
            await driver._check_risk(force=True)
        assert alerts.alerts[0][0] == "login-wall"

    asyncio.run(scenario())


def test_check_risk_clean_page():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1})
        driver, _, _ = _driver(page)
        await driver._check_risk(force=True)  # 无风控信号

    asyncio.run(scenario())


# ---------------------------------------------------------------- 生命周期

def test_wait_generation_fast_answer():
    async def scenario():
        # 无 stop 但有 done → 秒答直接返回
        page = _FakePage({DONE_SEL: 1})
        driver, _, _ = _driver(generation_start_timeout=0.3)
        await driver.wait_generation()

    asyncio.run(scenario())


def test_wait_generation_start_confirmed_then_done():
    async def scenario():
        counts = {STOP_SEL: 1, DONE_SEL: 1}

        class _Page(_FakePage):
            async def count_for(self, selector: str) -> int:
                # stop 前两次在、之后消失
                if selector == STOP_SEL:
                    self._stop_hits = getattr(self, "_stop_hits", 0) + 1
                    return 1 if self._stop_hits <= 2 else 0
                return counts.get(selector, 0)

        page = _Page()
        driver, _, _ = _driver(generation_done_timeout=10)
        await driver.wait_generation()  # 轮询到 stop 消失 → 返回

    asyncio.run(scenario())


def test_wait_generation_start_timeout_raises():
    async def scenario():
        page = _FakePage({})  # stop 不出现、done 也没有、无风控
        driver, _, _ = _driver(generation_start_timeout=0.3)
        with pytest.raises(RuntimeError, match="未能确认生成开始"):
            await driver.wait_generation()

    asyncio.run(scenario())


def test_wait_generation_done_timeout_raises():
    async def scenario():
        page = _FakePage({STOP_SEL: 1})  # stop 永远在 → 超时
        driver, _, _ = _driver(generation_start_timeout=0.3, generation_done_timeout=0.5)
        with pytest.raises(RuntimeError, match="生成超时"):
            await driver.wait_generation()

    asyncio.run(scenario())


def test_stop_generation_paths():
    async def scenario():
        # 无 stop 选择器 → 直接返回
        page = _FakePage({})
        driver, _, _ = _driver(page)
        await driver.stop_generation()

        # 有 stop → 点击后消失
        page2 = _FakePage({STOP_SEL: 1})

        class _Page2(_FakePage):
            async def count_for(self, selector: str) -> int:
                self._hits = getattr(self, "_hits", 0) + 1
                return 1 if self._hits == 1 else 0

        page2.__class__ = _Page2
        driver2, _, _ = _driver(page2)
        await driver2.stop_generation()
        assert page2.clicks == [STOP_SEL]

    asyncio.run(scenario())


def test_stop_generation_click_failure_logged():
    async def scenario():
        page = _FakePage({STOP_SEL: 1})

        async def bad_click(**kwargs):
            raise RuntimeError("nope")

        page.click = bad_click  # type: ignore[assignment]
        driver, _, _ = _driver(page)
        await driver.stop_generation()

    asyncio.run(scenario())


# ---------------------------------------------------------------- 图片上传

def test_upload_images_empty_returns_true():
    async def scenario():
        page = _FakePage({})
        driver, _, _ = _driver(page)
        assert await driver.upload_images([]) is True

    asyncio.run(scenario())


def test_upload_images_no_file_input():
    async def scenario():
        page = _FakePage({})
        driver, _, _ = _driver(page)
        assert await driver.upload_images(["/tmp/a.png"]) is False

    asyncio.run(scenario())


def test_upload_images_set_files_failure():
    async def scenario():
        page = _FakePage({SELECTORS["file_input"][0]: 1})

        async def bad_set(selector, paths):
            raise RuntimeError("nope")

        page.set_input_files = bad_set  # type: ignore[assignment]
        driver, _, _ = _driver(page)
        assert await driver.upload_images(["/tmp/a.png"]) is False

    asyncio.run(scenario())


def test_upload_images_thumb_confirms():
    async def scenario():
        thumb = SELECTORS["upload_thumb"][0]
        page = _FakePage({SELECTORS["file_input"][0]: 1, thumb: 2})
        driver, _, _ = _driver(upload_wait_timeout=5)
        assert await driver.upload_images(["/tmp/a.png", "/tmp/b.png"]) is True
        assert len(page.files_set[0]) == 2

    asyncio.run(scenario())


def test_upload_images_thumb_never_enough():
    async def scenario():
        thumb = SELECTORS["upload_thumb"][0]
        page = _FakePage({SELECTORS["file_input"][0]: 1, thumb: 1})
        driver, _, _ = _driver(upload_wait_timeout=0.6)
        assert await driver.upload_images(["/tmp/a.png", "/tmp/b.png"]) is False

    asyncio.run(scenario())


def test_upload_images_no_thumb_selector_blind_wait():
    async def scenario():
        page = _FakePage({SELECTORS["file_input"][0]: 1})
        driver, _, _ = _driver(upload_wait_timeout=5)
        assert await driver.upload_images(["/tmp/a.png"]) is True

    asyncio.run(scenario())


# ---------------------------------------------------------------- 自检

def test_check_selectors_report():
    async def scenario():
        page = _FakePage({INPUT_SEL: 1, DONE_SEL: 1})
        driver, _, _ = _driver(page)
        report = await driver.check_selectors()
        assert report["input"] is True and report["done"] is True
        assert report["send"] is False
        assert set(report) == set(SELECTORS)

    asyncio.run(scenario())


# ---------------------------------------------------------------- BrowserEngine

def test_engine_new_context_requires_start():
    async def scenario():
        engine = BrowserEngine(_settings(), _FakeAlerts())
        with pytest.raises(RuntimeError, match="未启动"):
            await engine.new_context([])

    asyncio.run(scenario())


def test_engine_new_context_passes_cookies_and_ua():
    async def scenario():
        engine = BrowserEngine(_settings(), _FakeAlerts())

        class _Ctx:
            def __init__(self):
                self.init_scripts: list[str] = []
                self.cookies: list = []

            async def add_init_script(self, script: str) -> None:
                self.init_scripts.append(script)

            async def add_cookies(self, cookies: list) -> None:
                self.cookies = cookies

        created: list[_Ctx] = []

        class _Browser:
            async def new_context(self, **kwargs):
                ctx = _Ctx()
                created.append(ctx)
                return ctx

        engine._browser = _Browser()
        await engine.new_context([{"name": "a", "value": "1"}])
        assert created[0].init_scripts and created[0].cookies == [{"name": "a", "value": "1"}]

    asyncio.run(scenario())


def test_engine_screenshot_success_and_failure(tmp_path):
    async def scenario():
        engine = BrowserEngine(_settings(data_dir=str(tmp_path)), _FakeAlerts())

        class _Page:
            async def screenshot(self, **kwargs):
                Path(kwargs["path"]).write_bytes(b"png")

        shot = await engine.screenshot(_Page(), "test")
        assert shot and Path(shot).is_file()

        class _BadPage:
            async def screenshot(self, **kwargs):
                raise RuntimeError("boom")

        assert await engine.screenshot(_BadPage(), "bad") == ""

    asyncio.run(scenario())


def test_engine_stop_swallows_errors():
    async def scenario():
        engine = BrowserEngine(_settings(), _FakeAlerts())

        class _Bad:
            async def close(self):
                raise RuntimeError("boom")

            async def stop(self):
                raise RuntimeError("boom")

        engine._browser = _Bad()
        engine._pw = _Bad()
        await engine.stop()  # 异常被吞

    asyncio.run(scenario())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
