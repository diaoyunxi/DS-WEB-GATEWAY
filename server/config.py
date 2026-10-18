"""①服务端配置：DeepSeek 网页选择器（多层候选，网页改版时只改这里）。

选择器原则：
- 每个用途给多个候选，运行时取第一个可解析的；
- 全部失效时由告警驱动人工修复（不用启发式乱猜）；
- DOM 只用于生命周期检测，绝不解析模型输出内容。
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("server.config")

DEFAULT_SELECTORS: dict[str, list[str]] = {
    # 输入框（DeepSeek 网页版为 textarea）
    "input": [
        "textarea#chat-input",
        "textarea",
        "div[contenteditable='true']",
    ],
    # 发送按钮（回车失败时的兜底）
    "send": [
        "div[role='button'][aria-label*='发送']",
        "[class*='send']",
        "button[type='submit']",
    ],
    # 生成中标志（停止按钮可见）
    "stop": [
        "div[role='button'][aria-label*='停止']",
        "[class*='stop']",
    ],
    # 生成完成标志（重新生成按钮出现）
    "done": [
        "[class*='regenerate']",
        "[aria-label*='重新生成']",
        "text=重新生成",
    ],
    # 联网搜索开关（保守策略：只有明确未开启时才点击）
    "web_search": [
        "button:has-text('联网搜索')",
        "[class*='search'] button",
        "text=联网搜索",
    ],
    # 隐藏文件输入框（Playwright 可对隐藏 input set_input_files）
    "file_input": [
        "input[type='file']",
    ],
    # 上传完成标志（缩略图）
    "upload_thumb": [
        "[class*='thumb'] img",
        "[class*='upload'] img",
        "[class*='attach'] img",
        "img[src^='blob:']",
    ],
    # 风控：验证码弹窗
    "risk_captcha": [
        "iframe[src*='challenges.cloudflare.com']",
        "iframe[src*='turnstile']",
    ],
    # 风控：访问频繁提示（仅在提交后短时间内检测，避免答案文本误报）
    "frequent": [
        "text=访问频繁",
        "text=操作过于频繁",
        "text=Too Many Requests",
    ],
    # 登录墙（cookies 过期）
    "login_wall": [
        "text=登录 / 注册",
        "[class*='login-modal']",
        "[class*='login'] button",
    ],
}


def load_selectors(selectors_json: str = "") -> dict[str, list[str]]:
    """合并环境变量覆盖（SELECTORS_JSON：{"input": [...], ...}）。"""
    selectors = {k: list(v) for k, v in DEFAULT_SELECTORS.items()}
    if selectors_json:
        try:
            override: dict[str, Any] = json.loads(selectors_json)
            for key, value in override.items():
                if isinstance(value, list) and value:
                    selectors[key] = [str(v) for v in value]
        except Exception as exc:  # noqa: BLE001
            logger.warning("SELECTORS_JSON 解析失败，使用默认选择器: %s", exc)
    return selectors


SELECTORS = load_selectors()
