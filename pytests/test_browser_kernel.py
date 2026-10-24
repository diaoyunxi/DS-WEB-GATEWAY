"""浏览器内核自动装配单测（不联网：用本地构造的归档替代下载）。

运行：pytest pytests/test_browser_kernel.py -q
"""
from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from server import browser_kernel as bk


def make_settings(tmp_path: Path, **over) -> SimpleNamespace:
    base = dict(
        browser_executable="",
        browser_auto_download=True,
        browser_kernel_version="152.0.7977.82",
        browser_kernel_url="",
        browser_kernel_dir=str(tmp_path / "browser"),
        data_dir=str(tmp_path),
    )
    base.update(over)
    return SimpleNamespace(**base)


def fake_zip(tmp_path: Path) -> Path:
    """构造一个最小 chrome 归档（含可执行 chmod 位）。"""
    zpath = tmp_path / "chrome-linux64.zip"
    with zipfile.ZipFile(str(zpath), "w") as zf:
        info = zipfile.ZipInfo("chrome-linux64/chrome")
        info.external_attr = 0o755 << 16
        zf.writestr(info, "#!/bin/sh\necho 'Google Chrome for Testing 152.0.7977.82'\n")
        zf.writestr("chrome-linux64/version.txt", "152.0.7977.82")
    return zpath


@pytest.fixture
def no_verify(monkeypatch):
    """跳过 `<chrome> --version` 子进程校验（测试环境无真实 chrome）。"""
    monkeypatch.setattr(bk, "_verify", lambda path: True)


# ---------------------------------------------------------------- URL / 平台

def test_default_kernel_url_for_current_platform():
    plat = bk.detect_platform()
    if plat is None:
        pytest.skip("当前平台不在支持列表")
    url = bk.default_kernel_url("152.0.7977.82")
    assert url == (
        f"https://storage.googleapis.com/chrome-for-testing-public/"
        f"152.0.7977.82/{plat}/{bk.archive_name(plat)}"
    )


def test_default_kernel_url_explicit_platform():
    assert bk.default_kernel_url("152.0.7977.82", "linux64") == (
        "https://storage.googleapis.com/chrome-for-testing-public/"
        "152.0.7977.82/linux64/chrome-linux64.zip"
    )


def test_unknown_platform_rejected():
    with pytest.raises(RuntimeError):
        bk.default_kernel_url("1.0", "solaris")


# ---------------------------------------------------------------- 下载装配

def test_download_and_install(tmp_path, monkeypatch, no_verify):
    zpath = fake_zip(tmp_path)
    calls = []

    def fake_download(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zpath.read_bytes())
        return dest

    monkeypatch.setattr(bk, "_download", fake_download)
    settings = make_settings(tmp_path)

    exe = bk.ensure_browser_kernel(settings)

    assert exe and Path(exe).name == "chrome"
    assert Path(exe).is_file() and os.access(exe, os.X_OK)
    assert len(calls) == 1 and "152.0.7977.82" in calls[0]
    # 解压目录被移动到内核根，staging 目录已清理
    assert not list((tmp_path / "browser").glob("chrome-staging-*"))
    assert (tmp_path / "browser" / "chrome-linux64" / "version.txt").is_file()


def test_idempotent_no_redownload(tmp_path, monkeypatch, no_verify):
    zpath = fake_zip(tmp_path)
    calls = []

    def fake_download(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zpath.read_bytes())
        return dest

    monkeypatch.setattr(bk, "_download", fake_download)
    settings = make_settings(tmp_path)

    first = bk.ensure_browser_kernel(settings)
    second = bk.ensure_browser_kernel(settings)

    assert first == second
    assert len(calls) == 1, "已安装时不应重复下载"


def test_force_redownloads(tmp_path, monkeypatch, no_verify):
    zpath = fake_zip(tmp_path)
    calls = []

    def fake_download(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        calls.append(url)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zpath.read_bytes())
        return dest

    monkeypatch.setattr(bk, "_download", fake_download)
    settings = make_settings(tmp_path)

    bk.ensure_browser_kernel(settings)
    bk.ensure_browser_kernel(settings, force=True)
    assert len(calls) == 2


def test_custom_url_used(tmp_path, monkeypatch, no_verify):
    zpath = fake_zip(tmp_path)
    seen: dict = {}

    def fake_download(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        seen["url"] = url
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zpath.read_bytes())
        return dest

    monkeypatch.setattr(bk, "_download", fake_download)
    settings = make_settings(tmp_path, browser_kernel_url="https://mirror.example/chrome.zip")

    bk.ensure_browser_kernel(settings)
    assert seen["url"] == "https://mirror.example/chrome.zip"


def test_explicit_executable_skips_download(tmp_path, monkeypatch, no_verify):
    def boom(*a, **k):  # 不应被调用
        raise AssertionError("显式指定内核时不应下载")

    monkeypatch.setattr(bk, "_download", boom)
    chrome = tmp_path / "my-chrome"
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)

    settings = make_settings(tmp_path, browser_executable=str(chrome))
    assert bk.ensure_browser_kernel(settings) == str(chrome)


def test_auto_download_disabled_returns_none(tmp_path, monkeypatch, no_verify):
    monkeypatch.setattr(bk, "_download", lambda *a, **k: pytest.fail("不应下载"))
    settings = make_settings(tmp_path, browser_auto_download=False)
    assert bk.ensure_browser_kernel(settings) is None


def test_download_failure_returns_none(tmp_path, monkeypatch, no_verify):
    import urllib.error

    def fail(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(bk, "_download", fail)
    settings = make_settings(tmp_path)
    assert bk.ensure_browser_kernel(settings) is None
    assert not list((tmp_path / "browser").glob("chrome-staging-*")), "失败应清理 staging"


def test_bad_zip_returns_none(tmp_path, monkeypatch, no_verify):
    def bad(url, dest, timeout=bk._DEFAULT_TIMEOUT):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"not a zip")
        return dest

    monkeypatch.setattr(bk, "_download", bad)
    settings = make_settings(tmp_path)
    assert bk.ensure_browser_kernel(settings) is None


def test_verify_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(bk, "_verify", lambda path: False)
    zpath = fake_zip(tmp_path)
    monkeypatch.setattr(
        bk, "_download",
        lambda url, dest, timeout=bk._DEFAULT_TIMEOUT: (
            dest.parent.mkdir(parents=True, exist_ok=True),
            dest.write_bytes(zpath.read_bytes()),
            dest,
        )[-1],
    )
    settings = make_settings(tmp_path)
    assert bk.ensure_browser_kernel(settings) is None


# ---------------------------------------------------------------- zip-slip

def test_zip_slip_rejected(tmp_path, monkeypatch, no_verify):
    zpath = tmp_path / "evil.zip"
    with zipfile.ZipFile(str(zpath), "w") as zf:
        zf.writestr("../../../../tmp/evil-chrome", "x")

    monkeypatch.setattr(
        bk, "_download",
        lambda url, dest, timeout=bk._DEFAULT_TIMEOUT: (
            dest.parent.mkdir(parents=True, exist_ok=True),
            dest.write_bytes(zpath.read_bytes()),
            dest,
        )[-1],
    )
    settings = make_settings(tmp_path)
    assert bk.ensure_browser_kernel(settings) is None


# ---------------------------------------------------------------- 状态

def test_kernel_status(tmp_path, no_verify):
    settings = make_settings(tmp_path, browser_auto_download=False)
    status = bk.kernel_status(settings)
    assert status["installed"] is False
    assert status["executable"] == ""
    assert status["platform"] == bk.detect_platform()
    assert status["version"] == "152.0.7977.82"


def test_kernel_dir_defaults_to_data_dir(tmp_path):
    settings = make_settings(tmp_path, browser_kernel_dir="")
    assert bk.resolve_kernel_dir(settings) == Path(str(tmp_path)) / "browser"


# ---------------------------------------------------------------- BrowserEngine 集成

class _FakeBrowser:
    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def install_fake_playwright(monkeypatch, calls: list, fail_with_executable: bool = False,
                            fail_always: bool = False):
    """注入假的 playwright.async_api，记录 launch 的 kwargs。"""
    import types

    class _Launcher:
        async def launch(self, **kwargs):
            calls.append(kwargs)
            if fail_always or (fail_with_executable and "executable_path" in kwargs):
                raise RuntimeError("launch failed")
            return _FakeBrowser()

    class _Chromium:
        launch = _Launcher().launch

    class _PW:
        chromium = _Chromium()

        async def start(self):
            return self

        async def stop(self):
            return None

    mod = types.ModuleType("playwright.async_api")

    def async_playwright():
        return _PW()

    mod.async_playwright = async_playwright
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)


def test_engine_start_uses_kernel(tmp_path, monkeypatch):
    import asyncio

    from server.browser import BrowserEngine

    calls: list = []
    install_fake_playwright(monkeypatch, calls)
    chrome = tmp_path / "chrome"
    chrome.write_text("#!/bin/sh\n")
    chrome.chmod(0o755)
    monkeypatch.setattr("server.browser.ensure_browser_kernel", lambda s: str(chrome))

    settings = SimpleNamespace(headless=True, data_dir=str(tmp_path))
    asyncio.run(BrowserEngine(settings, None).start())

    assert len(calls) == 1
    assert calls[0]["executable_path"] == str(chrome)
    assert calls[0]["headless"] is True


def test_engine_falls_back_when_kernel_launch_fails(tmp_path, monkeypatch):
    import asyncio

    from server.browser import BrowserEngine

    calls: list = []
    install_fake_playwright(monkeypatch, calls, fail_with_executable=True)
    monkeypatch.setattr("server.browser.ensure_browser_kernel", lambda s: "/opt/chrome/chrome")

    settings = SimpleNamespace(headless=True, data_dir=str(tmp_path))
    asyncio.run(BrowserEngine(settings, None).start())

    assert len(calls) == 2
    assert "executable_path" in calls[0]
    assert "executable_path" not in calls[1], "回退启动不应再带指定内核"


def test_engine_raises_with_hint_when_no_kernel(tmp_path, monkeypatch):
    import asyncio

    import pytest

    from server.browser import BrowserEngine

    calls: list = []
    install_fake_playwright(monkeypatch, calls, fail_always=True)
    monkeypatch.setattr("server.browser.ensure_browser_kernel", lambda s: None)

    settings = SimpleNamespace(headless=True, data_dir=str(tmp_path))
    with pytest.raises(RuntimeError, match="install_browser.py"):
        asyncio.run(BrowserEngine(settings, None).start())
