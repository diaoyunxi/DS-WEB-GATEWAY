"""浏览器内核（Chrome for Testing）自动下载与装配。

纪律：
- 只在 server（①）侧使用，playwright 仍保持懒加载（未安装时其余模块可导入）；
- 幂等：已存在可用内核则直接复用，不重复下载；
- 失败可降级：下载/解压/校验任一环节失败 → 返回 None，由 BrowserEngine 回退到
  playwright 自带 chromium（不阻断服务启动，只告警）。

内核来源：Chrome for Testing 官方归档
https://storage.googleapis.com/chrome-for-testing-public/<version>/<platform>/<archive>
"""
from __future__ import annotations

import logging
import os
import platform as platform_mod
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("server.browser_kernel")

DEFAULT_VERSION = "152.0.7977.82"
URL_TEMPLATE = (
    "https://storage.googleapis.com/chrome-for-testing-public/"
    "{version}/{platform}/{archive}"
)

# 平台 → (归档名, 归档内可执行文件路径)
_PLATFORMS: dict[str, tuple[str, str]] = {
    "linux64": ("chrome-linux64.zip", "chrome-linux64/chrome"),
    "mac-x64": ("chrome-mac-x64.zip", "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    "mac-arm64": ("chrome-mac-arm64.zip", "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
    "win64": ("chrome-win64.zip", "chrome-win64/chrome.exe"),
}

# zipfile 不保留可执行位时，按文件名兜底赋权
_EXEC_NAMES = {
    "chrome", "chrome.exe", "chrome_crashpad_handler", "chrome_sandbox",
    "chrome-management-service", "setup.exe",
}

_CHUNK = 1 << 20  # 1MiB
_DEFAULT_TIMEOUT = 600.0


def detect_platform() -> str | None:
    """返回 Chrome for Testing 的平台标识；未知平台返回 None。"""
    system = sys.platform
    machine = platform_mod.machine().lower()
    if system.startswith("linux"):
        return "linux64"
    if system == "darwin":
        return "mac-arm64" if machine in ("arm64", "aarch64") else "mac-x64"
    if system in ("win32", "cygwin", "msys"):
        return "win64"
    return None


def archive_name(platform_key: str) -> str:
    return _PLATFORMS[platform_key][0]


def binary_relpath(platform_key: str) -> str:
    return _PLATFORMS[platform_key][1]


def default_kernel_url(version: str = DEFAULT_VERSION, platform_key: str | None = None) -> str:
    """按版本 + 当前平台拼出官方归档 URL。"""
    plat = platform_key or detect_platform()
    if plat is None or plat not in _PLATFORMS:
        raise RuntimeError(f"不支持的浏览器内核平台: {platform_key or sys.platform}")
    return URL_TEMPLATE.format(version=version, platform=plat, archive=archive_name(plat))


def resolve_kernel_dir(settings: Any) -> Path:
    """内核根目录：BROWSER_KERNEL_DIR，否则 <data_dir>/browser。"""
    configured = (getattr(settings, "browser_kernel_dir", "") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(getattr(settings, "data_dir", "data")).expanduser() / "browser"


def _is_usable(path: Path) -> bool:
    return path.is_file() and os.access(str(path), os.X_OK)


def _chmod_executables(root: Path) -> None:
    """zip 不保存权限位时按文件名兜底赋可执行位。"""
    for dirpath, _dirnames, filenames in os.walk(str(root)):
        for name in filenames:
            if name in _EXEC_NAMES:
                path = Path(dirpath) / name
                try:
                    path.chmod(0o755)
                except OSError:  # noqa: PERF203
                    logger.debug("赋可执行位失败: %s", path)


def _safe_extract(zip_path: Path, dest: Path) -> None:
    """解压并保留 unix 权限位；阻断 zip-slip 路径穿越。"""
    dest_root = dest.resolve()
    with zipfile.ZipFile(str(zip_path)) as zf:
        for info in zf.infolist():
            target = (dest_root / info.filename).resolve()
            if target != dest_root and not str(target).startswith(str(dest_root) + os.sep):
                raise RuntimeError(f"归档含非法路径，已中止解压: {info.filename}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=_CHUNK)
            mode = info.external_attr >> 16
            if mode:
                os.chmod(target, mode & 0o7777)
    _chmod_executables(dest_root)


def _download(url: str, dest: Path, timeout: float = _DEFAULT_TIMEOUT) -> Path:
    """流式下载到临时文件，返回归档路径。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "DS-Web-Gateway/browser_kernel"})
    downloaded = 0
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        with open(tmp, "wb") as out:
            while True:
                chunk = resp.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)
                if downloaded % (16 * _CHUNK) < _CHUNK:  # 每约 16MiB 打一次
                    if total:
                        logger.info("浏览器内核下载 %d/%d MiB", downloaded >> 20, total >> 20)
                    else:
                        logger.info("浏览器内核下载 %d MiB", downloaded >> 20)
    tmp.replace(dest)
    logger.info("浏览器内核下载完成: %s (%.1f MiB)", dest, downloaded / 1048576)
    return dest


def _verify(binary: Path) -> bool:
    """执行 `<chrome> --version` 验证内核可用（失败只告警，不阻断）。"""
    try:
        proc = subprocess.run(  # noqa: S603
            [str(binary), "--version"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("浏览器内核校验失败（%s）: %s", binary, exc)
        return False
    if proc.returncode != 0:
        logger.warning("浏览器内核校验失败（%s）: %s", binary, (proc.stderr or "").strip()[:200])
        return False
    logger.info("浏览器内核可用: %s → %s", binary, (proc.stdout or "").strip())
    return True


def ensure_browser_kernel(
    settings: Any,
    force: bool = False,
    timeout: float = _DEFAULT_TIMEOUT,
) -> str | None:
    """确保存在可用浏览器内核，返回可执行文件路径；不可用时返回 None（降级）。

    优先级：
      1. BROWSER_EXECUTABLE 显式指定（不下载）；
      2. 内核目录下已解压的 chrome（不下载，force=True 除外）；
      3. 自动下载官方归档（BROWSER_AUTO_DOWNLOAD=false 时跳过）。
    """
    explicit = (getattr(settings, "browser_executable", "") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if _is_usable(path):
            logger.info("使用显式指定的浏览器内核: %s", path)
            return str(path)
        logger.warning("BROWSER_EXECUTABLE 指向的文件不可用: %s", path)

    plat = detect_platform()
    if plat is None:
        logger.warning("未知平台 %s，跳过内核自动下载，回退 playwright 自带 chromium", sys.platform)
        return None

    root = resolve_kernel_dir(settings)
    rel = binary_relpath(plat)
    binary = root / rel
    if not force and _is_usable(binary):
        logger.info("复用已安装的浏览器内核: %s", binary)
        return str(binary)

    url = (getattr(settings, "browser_kernel_url", "") or "").strip() or default_kernel_url(
        getattr(settings, "browser_kernel_version", DEFAULT_VERSION) or DEFAULT_VERSION, plat
    )

    if not getattr(settings, "browser_auto_download", True):
        logger.warning(
            "未找到浏览器内核且已关闭自动下载（BROWSER_AUTO_DOWNLOAD=false）。"
            "可手动执行 python scripts/install_browser.py"
        )
        return None

    root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix="chrome-staging-", dir=str(root)))
    try:
        archive = staging / archive_name(plat)
        logger.info("开始下载浏览器内核: %s", url)
        _download(url, archive, timeout=timeout)
        _safe_extract(archive, staging / "extracted")
        extracted = staging / "extracted" / rel
        if not extracted.is_file():
            raise RuntimeError(f"归档解压后缺少可执行文件: {rel}")

        target_parent = (root / rel).parent
        if target_parent.exists():
            shutil.rmtree(str(target_parent))  # 覆盖旧版本
        target_parent.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extracted.parent), str(target_parent))  # 原子替换整个内核目录
        binary = root / rel
        os.chmod(str(binary), 0o755)
        if not _verify(binary):
            return None
        logger.info("浏览器内核已安装: %s", binary)
        return str(binary)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, zipfile.BadZipFile,
            RuntimeError) as exc:
        logger.error(
            "浏览器内核自动下载失败: %s（将回退 playwright 自带 chromium；"
            "可手动执行 python scripts/install_browser.py）", exc,
        )
        return None
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)


def kernel_status(settings: Any) -> dict[str, Any]:
    """内核状态（供管理面/自检展示）。"""
    explicit = (getattr(settings, "browser_executable", "") or "").strip()
    plat = detect_platform()
    root = resolve_kernel_dir(settings)
    binary = root / binary_relpath(plat) if plat else None
    return {
        "platform": plat,
        "kernel_dir": str(root),
        "version": getattr(settings, "browser_kernel_version", DEFAULT_VERSION) or DEFAULT_VERSION,
        "url": (getattr(settings, "browser_kernel_url", "") or "").strip()
        or (default_kernel_url(DEFAULT_VERSION, plat) if plat else ""),
        "auto_download": bool(getattr(settings, "browser_auto_download", True)),
        "explicit_executable": explicit,
        "installed": bool(binary and _is_usable(binary)),
        "executable": str(binary) if binary and _is_usable(binary) else "",
    }
