#!/usr/bin/env python3
"""浏览器内核安装器：下载并装配 Chrome for Testing。

用法：
    python scripts/install_browser.py                 # 用默认版本/当前平台
    python scripts/install_browser.py --force         # 强制重新下载
    python scripts/install_browser.py --version 152.0.7977.82
    python scripts/install_browser.py --url <zip-url> # 指定归档（内网镜像等）
    python scripts/install_browser.py --dir /opt/chrome
    python scripts/install_browser.py --status        # 只看状态不下载

服务启动时（python -m server.main）会自动执行同一逻辑，此脚本用于预装/排障。
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.config import get_settings  # noqa: E402
from server.browser_kernel import (  # noqa: E402
    DEFAULT_VERSION,
    default_kernel_url,
    ensure_browser_kernel,
    kernel_status,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="下载并装配 Chrome for Testing 浏览器内核")
    parser.add_argument("--version", default="", help=f"内核版本（默认 {DEFAULT_VERSION}）")
    parser.add_argument("--url", default="", help="归档 URL（默认按版本+平台拼官方地址）")
    parser.add_argument("--dir", default="", help="安装目录（默认 <data_dir>/browser）")
    parser.add_argument("--executable", default="", help="直接使用已安装的 chrome 路径")
    parser.add_argument("--force", action="store_true", help="已存在也重新下载")
    parser.add_argument("--no-download", action="store_true", help="只检查，不下载")
    parser.add_argument("--status", action="store_true", help="打印内核状态后退出")
    parser.add_argument("--timeout", type=float, default=600.0, help="下载超时秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    settings = get_settings()
    if args.version:
        settings.browser_kernel_version = args.version
    if args.url:
        settings.browser_kernel_url = args.url
    if args.dir:
        settings.browser_kernel_dir = args.dir
    if args.executable:
        settings.browser_executable = args.executable
    if args.no_download or args.status:
        settings.browser_auto_download = False

    status = kernel_status(settings)
    print(f"平台          : {status['platform']}")
    print(f"版本          : {status['version']}")
    print(f"安装目录      : {status['kernel_dir']}")
    print(f"归档地址      : {status['url'] or default_kernel_url(status['version'], status['platform'])}")
    if args.status:
        print(f"已安装        : {status['installed']}")
        print(f"可执行路径    : {status['executable'] or '(未安装)'}")
        return 0 if status["installed"] else 2

    executable = ensure_browser_kernel(settings, force=args.force, timeout=args.timeout)
    if not executable:
        print("\n✗ 浏览器内核不可用。可手动下载后设置 BROWSER_EXECUTABLE，或运行 "
              "playwright install chromium 回退到自带内核。", file=sys.stderr)
        return 1
    print(f"\n✓ 浏览器内核就绪: {executable}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
