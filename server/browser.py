"""浏览器引擎与 DeepSeek 网页驱动。

纪律（见方案 §0 / §7 / §8）：
- DOM 仅用于生命周期（输入、上传、状态检测、停止、风控检测），不解析模型输出内容；
- Stealth 启动参数 + 人类打字延迟（逐段 30–120ms 随机）+ 随机停顿；
- 风控信号（验证码/访问频繁/登录墙）→ 截图 + 告警 + 挂起线程等人工；
- playwright 懒加载：未安装时其余模块（公网面/管理面）仍可导入。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path
from typing import Any

from .browser_kernel import ensure_browser_kernel
from .config import SELECTORS

logger = logging.getLogger("server.browser")


class ThreadSuspended(Exception):
    """线程被风控/登录墙挂起，需人工介入。"""


_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = {runtime: {}};
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
"""

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--window-size=1366,768",
    "--lang=zh-CN",
]

_LINUX_FIREFOX_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0"


class BrowserEngine:
    """共享一个 Chromium 实例，按会话创建 BrowserContext（cookies 已注入）。"""

    def __init__(self, settings, alerts) -> None:
        self._settings = settings
        self._alerts = alerts
        self._pw: Any = None
        self._browser: Any = None

    async def start(self) -> None:
        from playwright.async_api import async_playwright  # 懒加载

        # 内核装配（同步 IO，放线程）：优先 Chrome for Testing 自动下载，失败则回退
        executable = await asyncio.to_thread(ensure_browser_kernel, self._settings)

        self._pw = await async_playwright().start()
        kwargs: dict = {"headless": self._settings.headless, "args": _LAUNCH_ARGS}
        if executable:
            kwargs["executable_path"] = executable
        try:
            self._browser = await self._pw.chromium.launch(**kwargs)
            logger.info(
                "Chromium 已启动（headless=%s, 内核=%s）",
                self._settings.headless, executable or "playwright 自带",
            )
        except Exception as exc:  # noqa: BLE001
            if not executable:
                raise RuntimeError(
                    "浏览器启动失败。请执行 python scripts/install_browser.py "
                    "下载内核，或运行 playwright install chromium"
                ) from exc
            logger.warning("指定内核启动失败，回退 playwright 自带 chromium: %s", exc)
            self._browser = await self._pw.chromium.launch(
                headless=self._settings.headless, args=_LAUNCH_ARGS
            )
            logger.info("已回退至 playwright 自带 chromium")

    async def stop(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
            if self._pw is not None:
                await self._pw.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("关闭浏览器异常: %s", exc)

    async def new_context(self, cookies: list[dict]) -> Any:
        if self._browser is None:
            raise RuntimeError("浏览器引擎未启动")
        context = await self._browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=_LINUX_FIREFOX_UA,
        )
        await context.add_init_script(_STEALTH_JS)
        if cookies:
            await context.add_cookies(cookies)
        return context

    async def screenshot(self, page: Any, name: str) -> str:
        out_dir = Path(self._settings.data_dir) / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{name}-{int(time.time() * 1000)}.png"
        try:
            await page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("截图失败: %s", exc)
            return ""


class DeepSeekDriver:
    """单会话的页面操作（输入 / 提交 / 生命周期 / 风控 / 上传）。"""

    def __init__(self, engine: BrowserEngine, settings, page: Any) -> None:
        self._engine = engine
        self._settings = settings
        self.page = page

    # ---------------------------------------------------------------- 基础

    async def goto_home(self) -> None:
        await self.page.goto(
            self._settings.deepseek_url, wait_until="domcontentloaded", timeout=45_000
        )
        if await self._wait_selector("input", 30_000) is None:
            raise RuntimeError("DeepSeek 输入框未出现（页面结构变化或网络异常）")
        await asyncio.sleep(1.0)

    async def _first_selector(self, key: str) -> str | None:
        for sel in SELECTORS.get(key, []):
            try:
                if await self.page.locator(sel).count() > 0:
                    return sel
            except Exception:  # noqa: BLE001
                continue
        return None

    async def _wait_selector(self, key: str, timeout_ms: int) -> str | None:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            sel = await self._first_selector(key)
            if sel:
                return sel
            await asyncio.sleep(0.3)
        return None

    # ---------------------------------------------------------------- 输入

    async def input_text(self, text: str) -> None:
        sel = await self._wait_selector("input", 15_000)
        if not sel:
            raise RuntimeError("找不到输入框（选择器失效或页面结构变化）")
        await self.page.click(sel, timeout=5000)
        await asyncio.sleep(0.2)
        if len(text) <= self._settings.typing_max_chars:
            await self._type_human(text)
        else:
            # 超长文本走快速插入（insertText，不逐字符发按键，避免数分钟等待）
            await self.page.keyboard.insert_text(text)
            logger.info("长文本 %d 字符，已快速插入", len(text))
        await asyncio.sleep(0.3)

    async def _type_human(self, text: str) -> None:
        lo = self._settings.typing_delay_min_ms
        hi = self._settings.typing_delay_max_ms
        lines = text.split("\n")
        for idx, line in enumerate(lines):
            pos = 0
            while pos < len(line):
                step = random.randint(3, 10)
                seg = line[pos : pos + step]
                await self.page.keyboard.type(seg, delay=random.randint(lo, hi))
                pos += step
                if random.random() < self._settings.human_pause_probability:
                    await asyncio.sleep(random.uniform(0.2, 0.8))
            if idx != len(lines) - 1:
                await self.page.keyboard.press("Shift+Enter")  # 换行不提交

    async def submit(self) -> None:
        await self.page.keyboard.press("Enter")

    async def submit_with_fallback(self) -> None:
        """回车提交；若生成未启动则尝试点击发送按钮。"""
        await self.submit()
        await asyncio.sleep(1.0)
        if await self._first_selector("stop") is None:
            sel = await self._first_selector("send")
            if sel:
                logger.info("回车未触发生成，尝试点击发送按钮")
                try:
                    await self.page.click(sel, timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("点击发送按钮失败: %s", exc)

    # ---------------------------------------------------------------- 联网搜索

    async def ensure_web_search(self) -> bool:
        """保守策略：只有明确未开启时才点击；无法确认则保持原样并告警。"""
        for sel in SELECTORS.get("web_search", []):
            loc = self.page.locator(sel)
            if await loc.count() == 0:
                continue
            btn = loc.first
            state: bool | None = None
            for attr in ("aria-pressed", "aria-checked"):
                value = await btn.get_attribute(attr)
                if value is not None:
                    state = value.lower() in ("true", "1")
                    break
            if state is False:
                try:
                    await btn.click(timeout=5000)
                    logger.info("已点击开启联网搜索")
                    return True
                except Exception as exc:  # noqa: BLE001
                    logger.warning("点击联网搜索失败: %s", exc)
                    return False
            if state is True:
                return True
            cls = (await btn.get_attribute("class")) or ""
            if any(marker in cls for marker in ("active", "checked", "selected", "on")):
                return True
            logger.warning("无法确认联网搜索状态，保持原样")
            return False
        logger.warning("未找到联网搜索开关（选择器失效？）")
        return False

    # ---------------------------------------------------------------- 风控

    async def _check_risk(self, frequent_until: float | None = None, force: bool = False) -> None:
        for key in ("risk_captcha",):
            if await self._first_selector(key) is not None:
                shot = await self._engine.screenshot(self.page, "risk-captcha")
                await self._alerts.alert(
                    "risk-captcha", "检测到验证码弹窗", f"截图: {shot}"
                )
                raise ThreadSuspended("检测到验证码弹窗（Turnstile）")
        if frequent_until is None or time.monotonic() < frequent_until:
            if await self._first_selector("frequent") is not None:
                shot = await self._engine.screenshot(self.page, "risk-frequent")
                await self._alerts.alert(
                    "risk-frequent", "检测到访问频繁提示", f"截图: {shot}"
                )
                raise ThreadSuspended("检测到访问频繁提示")
        # 登录墙：仅在输入框消失或强制检查时判定（避免答案文本误报）
        if force or await self._first_selector("input") is None:
            if await self._first_selector("login_wall") is not None:
                shot = await self._engine.screenshot(self.page, "login-wall")
                await self._alerts.alert(
                    "login-wall", "疑似登录墙（cookies 过期）", f"截图: {shot}"
                )
                raise ThreadSuspended("疑似登录墙（cookies 过期）")

    # ---------------------------------------------------------------- 生命周期

    async def wait_generation(self) -> None:
        """确认生成开始 → 轮询至完成；期间监控风控。"""
        stop_sel = await self._wait_selector(
            "stop", int(self._settings.generation_start_timeout * 1000)
        )
        if stop_sel is None:
            if await self._first_selector("done") is not None:
                return  # 秒答：已完成
            await self._check_risk(force=True)
            raise RuntimeError("未能确认生成开始（输入或提交失败）")
        frequent_until = time.monotonic() + 15.0  # 访问频繁提示只在早期检测
        deadline = time.monotonic() + self._settings.generation_done_timeout
        while time.monotonic() < deadline:
            await self._check_risk(frequent_until=frequent_until)
            try:
                if await self.page.locator(stop_sel).count() == 0:
                    await asyncio.sleep(1.0)
                    return
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(1.0)
        raise RuntimeError("生成超时（超过 generation_done_timeout）")

    async def stop_generation(self) -> None:
        sel = await self._first_selector("stop")
        if not sel:
            return
        try:
            await self.page.click(sel, timeout=5000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("点击停止按钮失败: %s", exc)
        for _ in range(20):
            try:
                if await self.page.locator(sel).count() == 0:
                    return
            except Exception:  # noqa: BLE001
                return
            await asyncio.sleep(0.25)

    # ---------------------------------------------------------------- 图片

    async def upload_images(self, paths: list[str]) -> bool:
        if not paths:
            return True
        sel = await self._first_selector("file_input")
        if not sel:
            logger.warning("未找到文件输入框，跳过图片上传")
            return False
        try:
            await self.page.set_input_files(sel, paths)
        except Exception as exc:  # noqa: BLE001
            logger.warning("上传图片失败: %s", exc)
            return False
        thumb = await self._first_selector("upload_thumb")
        deadline = time.monotonic() + self._settings.upload_wait_timeout
        while time.monotonic() < deadline:
            await self._check_risk()
            if thumb:
                if await self.page.locator(thumb).count() >= len(paths):
                    await asyncio.sleep(0.5)
                    return True
            else:
                await asyncio.sleep(2.0)  # 无缩略图选择器时盲等
                return True
            await asyncio.sleep(0.5)
        logger.warning("图片上传完成标志未出现（仍继续输入文本）")
        return False

    # ---------------------------------------------------------------- 自检

    async def check_selectors(self) -> dict:
        report: dict[str, bool] = {}
        for key in SELECTORS:
            report[key] = await self._first_selector(key) is not None
        return report
