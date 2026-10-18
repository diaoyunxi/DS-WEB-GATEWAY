"""共享配置：能力 URL、密钥、超时与长度限制。

①（server）与 ②（gateway）都读同一份 Settings（环境变量 / .env）。
密钥类字段留空时启动时随机生成并打印到日志（仅首次）。
"""
from __future__ import annotations

import secrets
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- ①公网入口（能力 URL 基址）----
    server_scheme: str = "https"
    server_host: str = "localhost"
    server_port: int = 443
    server_external_url: str = ""          # 外部基址（命名 Cloudflare Tunnel 域名）；空则自动拼接
    path_token: str = ""                   # ≥128bit；空则启动时随机生成
    ws_secret: str = ""                    # ②拨入①的共享密钥；空则随机生成

    # ---- ①监听（公网面 / 管理面）----
    server_bind_host: str = "127.0.0.1"
    server_bind_port: int = 8443
    admin_bind_host: str = "127.0.0.1"
    admin_bind_port: int = 8444
    admin_token: str = ""                  # 管理面额外令牌；空则不校验（仅监听内网）
    trust_forwarded_for: bool = True       # 位于 Cloudflare/反代之后时取 X-Forwarded-For

    # ---- DeepSeek 网页 ----
    deepseek_url: str = "https://chat.deepseek.com/"
    cookies_json: str = ""                 # 内联 cookies JSON（Playwright cookie 数组）
    cookies_file: str = ""                 # cookies JSON 文件路径（优先于内联）
    headless: bool = True

    # ---- 会话与并发 ----
    max_contexts: int = 3
    context_idle_ttl: int = 1800
    skeleton_every_n_turns: int = 8
    acquire_timeout: float = 180.0

    # ---- 信封与分片 ----
    answer_shard_max_chars: int = 4000
    shard_buffer_ttl: int = 300

    # ---- 工具结果交付（/result 短挂起）----
    poll_timeout: float = 12.0
    result_max_bytes: int = 8192
    pending_call_ttl: int = 600

    # ---- 图片 ----
    image_ttl: int = 600
    max_images_per_turn: int = 5
    max_image_bytes: int = 10 * 1024 * 1024

    # ---- 限流（不按 IP）----
    global_qps: float = 5.0
    per_code_qps: float = 1.0
    rate_limit_burst: int = 10

    # ---- 反风控 ----
    typing_delay_min_ms: int = 30
    typing_delay_max_ms: int = 120
    typing_max_chars: int = 1200           # 超过则改用快速插入（不再逐字符）
    human_pause_probability: float = 0.15
    generation_start_timeout: float = 30.0
    generation_done_timeout: float = 600.0
    upload_wait_timeout: float = 30.0

    # ---- WS ----
    ws_heartbeat: int = 30

    # ---- 告警（SMTP，留空则只记日志）----
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_to: str = ""
    smtp_starttls: bool = True
    alert_cooldown: int = 300

    # ---- 数据目录 ----
    data_dir: str = "data"

    # ---- ②网关 ----
    gateway_bind_host: str = "127.0.0.1"
    gateway_bind_port: int = 8080
    gateway_api_keys: str = ""             # 逗号分隔；空 = 不鉴权
    gateway_model_name: str = "deepseek-web"
    ws_url: str = ""                       # ②拨入①的 WS 地址；空则按本机拼接
    request_timeout: float = 1800.0
    replay_max_chars: int = 24000

    # ---- 其他 ----
    log_level: str = "INFO"

    # ---------------------------------------------------------------- 派生属性

    @property
    def api_keys(self) -> list[str]:
        return [k.strip() for k in self.gateway_api_keys.split(",") if k.strip()]

    @property
    def external_base_url(self) -> str:
        if self.server_external_url:
            return self.server_external_url.rstrip("/")
        port = "" if self.server_port in (80, 443) else f":{self.server_port}"
        return f"{self.server_scheme}://{self.server_host}{port}"

    @property
    def resolved_ws_url(self) -> str:
        """②拨入①的 WS 地址（{token} 占位符由调用方替换）。"""
        if self.ws_url:
            return self.ws_url
        scheme = "wss" if self.server_scheme == "https" else "ws"
        return f"{scheme}://{self.server_bind_host}:{self.server_bind_port}/{{token}}/ws"

    @property
    def valid_tokens(self) -> set[str]:
        """支持轮换：当前 token 集合（暂存单 token，预留多 token）。"""
        return {self.path_token} if self.path_token else set()

    def docs_url(self, token: str, code: str) -> str:
        return f"{self.external_base_url}/{token}/docs?code={code}"

    def push_url(self, token: str) -> str:
        return f"{self.external_base_url}/{token}/push"

    def result_url(self, token: str) -> str:
        return f"{self.external_base_url}/{token}/result"

    def ensure_secrets(self) -> tuple[str, str]:
        """path_token / ws_secret 留空则随机生成（≥128bit）。"""
        if not self.path_token:
            self.path_token = secrets.token_hex(16)  # 128bit
        if not self.ws_secret:
            self.ws_secret = secrets.token_urlsafe(32)
        return self.path_token, self.ws_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
