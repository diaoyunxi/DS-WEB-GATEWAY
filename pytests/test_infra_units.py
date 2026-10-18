"""①基础设施单测：分片重组、pending_call、限流、审计日志、告警冷却。

运行：pytest pytests/test_infra_units.py -q
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

from common.schemas import AnswerEnvelope
from server.alerts import AlertManager
from server.audit import AuditLog
from server.envelopes import ShardBuffer
from server.pending import PendingCallRegistry
from server.ratelimit import RateLimiter, TokenBucket


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- ShardBuffer

def test_shard_buffer_partial_then_complete():
    async def scenario():
        buf = ShardBuffer(60)
        assert await buf.add("c1", AnswerEnvelope(content="甲", seq=1, total=2)) is None
        assert await buf.progress("c1") == (1, 2)
        content = await buf.add("c1", AnswerEnvelope(content="乙", seq=2, total=2))
        assert content == "甲乙"
        assert await buf.progress("c1") == (0, 0)  # 完成后分组清除

    run(scenario())


def test_shard_buffer_out_of_order():
    async def scenario():
        buf = ShardBuffer(60)
        assert await buf.add("c1", AnswerEnvelope(content="乙", seq=2, total=2)) is None
        # seq=1 晚到：不重启分组，直接补齐
        assert await buf.add("c1", AnswerEnvelope(content="甲", seq=1, total=2)) == "甲乙"

    run(scenario())


def test_shard_buffer_duplicate_seq_overwrite():
    async def scenario():
        buf = ShardBuffer(60)
        await buf.add("c1", AnswerEnvelope(content="旧", seq=1, total=2))
        await buf.add("c1", AnswerEnvelope(content="新", seq=1, total=2))  # 覆盖去重
        assert await buf.progress("c1") == (1, 2)
        assert await buf.add("c1", AnswerEnvelope(content="乙", seq=2, total=2)) == "新乙"

    run(scenario())


def test_shard_buffer_total_change_restarts_group():
    async def scenario():
        buf = ShardBuffer(60)
        await buf.add("c1", AnswerEnvelope(content="甲", seq=1, total=2))
        # total 变化 → 开启新组（此前分片作废）
        assert await buf.add("c1", AnswerEnvelope(content="乙", seq=2, total=3)) is None
        assert await buf.progress("c1") == (1, 3)
        # 同一 total 下重复 seq=1 → 重启新组
        await buf.add("c1", AnswerEnvelope(content="丙", seq=3, total=3))
        assert await buf.add("c1", AnswerEnvelope(content="新一组", seq=1, total=3)) is None
        assert await buf.progress("c1") == (1, 3)

    run(scenario())


def test_shard_buffer_sweep_and_stats():
    async def scenario():
        buf = ShardBuffer(ttl=0)  # 立即过期
        await buf.add("c1", AnswerEnvelope(content="x", seq=1, total=2))
        assert buf.sweep() == 1
        assert await buf.progress("c1") == (0, 0)

    run(scenario())
    assert ShardBuffer(42).stats() == {"codes": 0, "ttl": 42}


# ---------------------------------------------------------------- PendingCallRegistry

def test_pending_register_dedup():
    async def scenario():
        reg = PendingCallRegistry(60)
        call, is_new = await reg.register("c1", "get_weather", {"city": "北京"})
        assert is_new and call.call_id.startswith("call_")
        same, is_new2 = await reg.register("c1", "get_weather", {"city": "北京"})
        assert same.call_id == call.call_id and not is_new2  # 重复抓取去重
        other, is_new3 = await reg.register("c1", "get_weather", {"city": "上海"})
        assert other.call_id != call.call_id and is_new3
        # 不同 code 同一工具是不同调用
        diff_code, _ = await reg.register("c2", "get_weather", {"city": "北京"})
        assert diff_code.call_id != call.call_id

    run(scenario())


def test_pending_set_wait_consume_lifecycle():
    async def scenario():
        reg = PendingCallRegistry(60)
        call, _ = await reg.register("c1", "t", {})
        assert await reg.set_result("no-such", "x") is False
        assert await reg.set_result(call.call_id, "晴，20℃") is True
        assert await reg.wait_result(call.call_id, 0.1) == "晴，20℃"
        assert await reg.consume(call.call_id) == "晴，20℃"
        assert await reg.consume(call.call_id) is None  # 一次性消费
        assert await reg.set_result(call.call_id, "again") is False  # 已消费

    run(scenario())


def test_pending_wait_timeout_and_unknown():
    async def scenario():
        reg = PendingCallRegistry(60)
        assert await reg.wait_result("no-such", 0.05) is None
        call, _ = await reg.register("c1", "t", {})
        assert await reg.wait_result(call.call_id, 0.05) is None  # 超时

    run(scenario())


def test_pending_sweep_and_stats():
    async def scenario():
        reg = PendingCallRegistry(ttl=0)
        call, _ = await reg.register("c1", "t", {})
        assert reg.sweep() == 1
        assert reg.get(call.call_id) is None
        assert await reg.register("c1", "t2", {}) is not None

    run(scenario())


async def _stats_scenario():
    reg = PendingCallRegistry(60)
    c1, _ = await reg.register("c1", "t", {})
    c2, _ = await reg.register("c1", "t2", {})
    await reg.set_result(c1.call_id, "r")
    await reg.consume(c1.call_id)
    return reg.stats()


def test_pending_stats():
    stats = run(_stats_scenario())
    assert stats == {"total": 2, "ready": 1, "consumed": 1, "ttl": 60}


# ---------------------------------------------------------------- 限流

def test_token_bucket_burst_and_refill():
    bucket = TokenBucket(qps=100.0, burst=3)
    assert [bucket.try_consume() for _ in range(4)] == [True, True, True, False]
    time.sleep(0.05)  # 100qps → 5 tokens
    assert bucket.try_consume()


def test_token_bucket_min_floor():
    bucket = TokenBucket(qps=0.0, burst=1)
    assert bucket._rate >= 0.01 and bucket._capacity >= 1.0


def test_rate_limiter_global_and_per_code():
    limiter = RateLimiter(global_qps=1000.0, per_code_qps=1000.0, burst=5)
    assert all(limiter.check("c1") for _ in range(5))
    assert not limiter.check("c1")  # 全局 burst 耗尽
    assert not limiter.check("c2")  # 全局桶是共享的


def test_rate_limiter_per_code_isolation():
    limiter = RateLimiter(global_qps=1000.0, per_code_qps=1000.0, burst=50)
    for _ in range(3):
        assert limiter.check("c1")
    # 每个 code 独立桶：c2 不受 c1 消耗影响
    assert limiter.check("c2")
    assert limiter.stats()["codes"] == 2


# ---------------------------------------------------------------- 审计日志

def test_audit_log_record_recent_ip_stats():
    log = AuditLog(capacity=3)
    log.record("1.1.1.1", "UA", "/p", "c1", "push", "tool_call:x")
    log.record("2.2.2.2", "UA" * 300, "/p" * 300, "c2", "probe", "n" * 300)
    log.record("1.1.1.1", "UA", "/d", "c1", "docs")
    recent = log.recent(10)
    assert len(recent) == 3  # 容量 3
    assert recent[0]["ip"] == "1.1.1.1"
    assert len(recent[1]["ua"]) == 200  # 截断
    assert log.recent(2, code="c1")[-1]["kind"] == "docs"  # 按 code 过滤
    assert log.ip_stats() == {"1.1.1.1": 2, "2.2.2.2": 1}  # 按次数降序
    assert log.stats() == {"total": 3, "unique_ips": 2}


# ---------------------------------------------------------------- 告警

def test_alert_manager_without_smtp_only_logs():
    async def scenario():
        alerts = AlertManager(SimpleNamespace(smtp_host="", smtp_to="", alert_cooldown=300))
        assert not alerts.smtp_configured
        assert await alerts.alert("test", "主题", "正文") is False
        assert len(alerts.recent()) == 1
        assert alerts.recent()[0]["kind"] == "test"

    run(scenario())


def test_alert_manager_cooldown_and_send():
    sent: list[tuple[str, str]] = []

    async def scenario():
        alerts = AlertManager(
            SimpleNamespace(
                smtp_host="mail.example.com",
                smtp_port=587,
                smtp_user="u",
                smtp_password="p",
                smtp_from="f@example.com",
                smtp_to="t@example.com",
                smtp_starttls=True,
                alert_cooldown=300,
            )
        )
        alerts._send_mail = lambda subject, body: sent.append((subject, body))
        assert alerts.smtp_configured
        assert await alerts.alert("risk", "验证码", "详情") is True
        assert sent and sent[0][0] == "[DS-Web-Gateway][risk] 验证码"
        # 冷却期内同 kind 不再发信
        assert await alerts.alert("risk", "验证码2", "详情2") is False
        assert len(sent) == 1
        assert len(alerts.recent(10)) == 2  # 历史都记录

    run(scenario())


def test_alert_manager_history_capped():
    async def scenario():
        alerts = AlertManager(SimpleNamespace(smtp_host="", smtp_to="", alert_cooldown=0))
        for i in range(210):
            await alerts.alert("k", f"s{i}", "b")
        assert len(alerts.recent(1000)) == 200  # 环形上限 200

    run(scenario())


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
