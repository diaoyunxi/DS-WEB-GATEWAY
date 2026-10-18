#!/usr/bin/env python
"""阶段 0 PoC 门禁测试：内容复述测试（方案 §11 第 1 项，唯一核心未知项）。

前提：
  1. ① server 已启动并配置好 cookies（python -m server.main）；
  2. 命名 Cloudflare Tunnel 已把 SERVER_BIND_PORT 暴露到 SERVER_EXTERNAL_URL；
  3. ② gateway 已启动（python -m gateway.main）；
  4. 已知一个有效的 path_token（①启动日志会打印；或已写入 .env）。

用法：
  python scripts/poc_echo_test.py --token <path_token> --api-key <调用方key>

流程：生成唯一 nonce → 让模型访问 /probe?nonce=... 并原样复述 → 判定。
判定为 PASS 才值得继续投入阶段 1 之后的开发。
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gateway", default="http://127.0.0.1:8080")
    parser.add_argument("--token", required=True, help="①启动日志打印的 path_token")
    parser.add_argument("--api-key", default="", help="调用方 API Key（②配置了鉴权时必填）")
    parser.add_argument("--probe-base", default="", help="①外部基址；空则从 --token 拼（不可用，仅占位）")
    args = parser.parse_args()

    settings_base = args.probe_base.rstrip("/")
    if not settings_base:
        print("请用 --probe-base 指定①的外部基址（如 https://dsgw.example.com）")
        return 2

    nonce = f"北极星-{secrets.token_hex(3).upper()}"
    probe_url = f"{settings_base}/{args.token}/probe?nonce={nonce}"
    print(f"nonce      = {nonce}")
    print(f"probe_url  = {probe_url}")

    headers = {"Content-Type": "application/json"}
    if args.api_key:
        headers["Authorization"] = f"Bearer {args.api_key}"

    payload = {
        "model": "deepseek-web",
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    "请先用你的网页浏览/联网搜索能力访问下面这个 URL，"
                    "然后把页面中出现的那一行 nonce 原样复述给我（只复述 nonce 本身）：\n"
                    f"{probe_url}"
                ),
            }
        ],
    }

    async with httpx.AsyncClient(timeout=600.0) as client:
        resp = await client.post(
            f"{args.gateway}/v1/chat/completions", json=payload, headers=headers
        )
    if resp.status_code != 200:
        print(f"网关返回 {resp.status_code}: {resp.text[:500]}")
        return 1
    data = resp.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    print("---- 模型回答 ----")
    print(content)
    print("------------------")
    if nonce in content:
        print(f"PASS：模型复述了 nonce（{nonce}）——页面内容确认进入模型上下文。")
        return 0
    print("FAIL：模型未复述 nonce。可能原因：未真正抓取 / 抓取内容未进上下文 / 模型跑题。")
    print("请查看①的抓取日志（GET /admin/logs）确认 probe 是否被抓取过。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
