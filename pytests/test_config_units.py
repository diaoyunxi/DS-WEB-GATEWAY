"""共享配置单测：Settings 派生属性、密钥生成、选择器覆盖合并。

运行：pytest pytests/test_config_units.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # 允许直接运行

os.environ.setdefault("DATA_DIR", "data-test")

import pytest

from common.config import Settings, get_settings
from server.config import DEFAULT_SELECTORS, load_selectors


def _settings(**kwargs) -> Settings:
    base = dict(
        path_token="tok-abcdefghijklmnop",
        ws_secret="secret-xyz",
        data_dir="data-test",
    )
    base.update(kwargs)
    return Settings(**base)


# ---------------------------------------------------------------- 派生属性

def test_api_keys_parsing():
    s = _settings(gateway_api_keys=" a , b ,, c ")
    assert s.api_keys == ["a", "b", "c"]
    assert _settings(gateway_api_keys="").api_keys == []


def test_external_base_url_variants():
    assert _settings(server_port=443).external_base_url == "https://localhost"
    assert _settings(server_port=80).external_base_url == "https://localhost"
    assert _settings(server_port=8443).external_base_url == "https://localhost:8443"
    assert (
        _settings(server_external_url="https://tunnel.example.com/").external_base_url
        == "https://tunnel.example.com"
    )


def test_resolved_ws_url_variants():
    s = _settings(ws_url="wss://custom/ws")
    assert s.resolved_ws_url == "wss://custom/ws"
    s = _settings(server_scheme="https", server_bind_host="0.0.0.0", server_bind_port=8443)
    assert s.resolved_ws_url == "wss://0.0.0.0:8443/{token}/ws"
    s = _settings(server_scheme="http", server_bind_host="127.0.0.1", server_bind_port=9000)
    assert s.resolved_ws_url == "ws://127.0.0.1:9000/{token}/ws"


def test_valid_tokens_and_urls():
    s = _settings()
    assert s.valid_tokens == {"tok-abcdefghijklmnop"}
    assert _settings(path_token="").valid_tokens == set()
    assert s.docs_url("tok-abcdefghijklmnop", "c1").endswith(
        "/tok-abcdefghijklmnop/docs?code=c1"
    )
    assert s.push_url("tok-abcdefghijklmnop").endswith("/tok-abcdefghijklmnop/push")
    assert s.result_url("tok-abcdefghijklmnop").endswith("/tok-abcdefghijklmnop/result")


def test_ensure_secrets_generates_and_is_stable():
    s = _settings(path_token="", ws_secret="")
    token, secret = s.ensure_secrets()
    assert len(token) == 32 and len(secret) >= 32  # 128bit+
    assert s.ensure_secrets() == (token, secret)  # 幂等


def test_get_settings_singleton():
    assert get_settings() is get_settings()


# ---------------------------------------------------------------- 选择器

def test_load_selectors_default():
    selectors = load_selectors()
    assert selectors == {k: list(v) for k, v in DEFAULT_SELECTORS.items()}


def test_load_selectors_override():
    selectors = load_selectors('{"input": ["textarea#x"]}')
    assert selectors["input"] == ["textarea#x"]
    assert selectors["send"] == DEFAULT_SELECTORS["send"]  # 其余保持默认


def test_load_selectors_invalid_json_keeps_default():
    selectors = load_selectors("{不是JSON")
    assert selectors["input"] == DEFAULT_SELECTORS["input"]


def test_load_selectors_ignores_non_list_override():
    selectors = load_selectors('{"input": "not-a-list", "send": []}')
    assert selectors["input"] == DEFAULT_SELECTORS["input"]
    assert selectors["send"] == DEFAULT_SELECTORS["send"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
