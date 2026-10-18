"""部分② Gateway 配置（复用 common.Settings，此处仅做网关侧派生）。"""
from __future__ import annotations

from functools import lru_cache

from common.config import Settings, get_settings


@lru_cache
def get_gateway_settings() -> Settings:
    settings = get_settings()
    return settings
