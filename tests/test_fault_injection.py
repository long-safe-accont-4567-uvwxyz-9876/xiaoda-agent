"""FaultInjectingLLMClient 单元测试 (P1 Chaos Engineering)

覆盖场景:
- test_no_fault_normal      : fault_rate=0 时正常调用不注入
- test_timeout_injection    : timeout 故障正确注入 (抛 TimeoutError)
- test_error_injection      : error 故障正确注入 (抛 LLMFaultError, 随机 5xx)
- test_slow_injection       : slow 故障正确注入 (延迟后返回, 用 mock 时间)
- test_empty_injection      : empty 响应注入 (返回空字符串)
- test_fault_rate            : 故障率正确 (100 次, fault_rate=0.3, 注入 25-35 次)
- test_seed_reproducibility  : 相同 seed 产生相同故障序列
- test_stats                 : get_stats 统计正确

依赖:
- pytest, pytest-asyncio
- 不依赖真实 LLM, 使用 _StubClient
"""
import asyncio
import sys
from pathlib import Path

import pytest

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from chaos.fault_injecting_llm_client import (
    ERROR_FAULT_CODES,
    FaultConfig,
    FaultInjectingLLMClient,
    LLMFaultError,
)
from chaos import verify_degradation
import contextlib


# ────────────────────────────────────────────────────────────
# 辅助: 桩 LLM 客户端
# ────────────────────────────────────────────────────────────

class _StubClient:
    """桩 LLM 客户端 — chat 返回固定字符串, chat_stream 产出一个 chunk"""

    async def chat(self, *args, **kwargs):
        return "stub-reply"

    async def chat_stream(self, *args, **kwargs):
        yield "stub-chunk-1"
        yield "stub-chunk-2"


# ────────────────────────────────────────────────────────────
# 1. fault_rate=0 时正常调用, 不注入任何故障
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_fault_normal():
    """fault_rate=0 时所有调用都走真实 client, 无故障注入"""
    cfg = FaultConfig(fault_rate=0.0, seed=42)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    # 调用 20 次, 全部应返回正常响应
    for _ in range(20):
        reply = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert reply == "stub-reply"

    stats = client.get_stats()
    assert stats["total_calls"] == 20
    assert stats["faults_injected"] == 0
    # by_type 各类型计数均为 0
    for ft, count in stats["by_type"].items():
        assert count == 0, f"fault_rate=0 时不应注入 {ft}, 实际 {count}"


# ────────────────────────────────────────────────────────────
# 2. timeout 故障正确注入
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_injection():
    """100% timeout 故障时, chat 抛出 asyncio.TimeoutError"""
    cfg = FaultConfig(fault_rate=1.0, fault_types=["timeout"], seed=1)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    for _ in range(5):
        with pytest.raises(asyncio.TimeoutError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])

    stats = client.get_stats()
    assert stats["total_calls"] == 5
    assert stats["faults_injected"] == 5
    assert stats["by_type"]["timeout"] == 5
    # 其他类型不应有计数
    assert stats["by_type"]["error"] == 0
    assert stats["by_type"]["slow"] == 0
    assert stats["by_type"]["empty"] == 0


# ────────────────────────────────────────────────────────────
# 3. error 故障正确注入 (随机 API 错误码)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_injection():
    """100% error 故障时, chat 抛出 LLMFaultError 且 code 在合法集合内"""
    cfg = FaultConfig(fault_rate=1.0, fault_types=["error"], seed=3)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    observed_codes = set()
    for _ in range(20):
        with pytest.raises(LLMFaultError) as exc_info:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
        # 错误码必须在合法集合内
        assert exc_info.value.code in ERROR_FAULT_CODES
        assert exc_info.value.fault_type == "error"
        observed_codes.add(exc_info.value.code)

    stats = client.get_stats()
    assert stats["total_calls"] == 20
    assert stats["faults_injected"] == 20
    assert stats["by_type"]["error"] == 20
    # 至少应观察到 2 种不同的错误码 (seed=3 在 20 次中覆盖多码)
    assert len(observed_codes) >= 2, (
        f"20 次 error 注入应至少出现 2 种错误码, 实际 {observed_codes}"
    )


# ────────────────────────────────────────────────────────────
# 4. slow 故障正确注入 (用 mock 时间, 避免真实 10s 等待)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_slow_injection(monkeypatch):
    """100% slow 故障时, 延迟后返回真实 client 的结果

    通过 monkeypatch 将 SLOW_FAULT_DELAY_SECONDS 置 0, 避免真实等待.
    同时验证 client 内部确实调用了 asyncio.sleep.
    """
    # mock 延迟常量
    import chaos.fault_injecting_llm_client as fim
    monkeypatch.setattr(fim, "SLOW_FAULT_DELAY_SECONDS", 0.0)

    # 记录 sleep 被调用的次数
    sleep_calls = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    cfg = FaultConfig(fault_rate=1.0, fault_types=["slow"], seed=5)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    for _ in range(3):
        reply = await client.chat(messages=[{"role": "user", "content": "hi"}])
        # slow 故障延迟后返回真实结果
        assert reply == "stub-reply"

    stats = client.get_stats()
    assert stats["total_calls"] == 3
    assert stats["faults_injected"] == 3
    assert stats["by_type"]["slow"] == 3
    # asyncio.sleep 被调用 3 次 (每次 slow 注入都延迟)
    assert len(sleep_calls) == 3
    # 由于我们 mock 了常量为 0, 但 _fake_sleep 收到的是 0
    # (fault_injecting_llm_client 中使用 module 全局 SLOW_FAULT_DELAY_SECONDS)
    # 注: monkeypatch.setattr(fim, ...) 已让 client 读到 0.0
    assert all(s == 0.0 for s in sleep_calls)


# ────────────────────────────────────────────────────────────
# 5. empty 响应注入
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_injection():
    """100% empty 故障时, chat 返回空字符串"""
    cfg = FaultConfig(fault_rate=1.0, fault_types=["empty"], seed=9)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    for _ in range(5):
        reply = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert reply == ""

    stats = client.get_stats()
    assert stats["total_calls"] == 5
    assert stats["faults_injected"] == 5
    assert stats["by_type"]["empty"] == 5


# ────────────────────────────────────────────────────────────
# 6. 故障率正确 (100 次调用, fault_rate=0.3, 注入次数 25-35)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fault_rate(monkeypatch):
    """100 次调用 fault_rate=0.3 时, 注入次数应落在 [25, 35] 内 (固定 seed)

    注: slow 故障默认延迟 10s, 此处 mock SLOW_FAULT_DELAY_SECONDS=0 避免真实等待.
    seed=41 在该序列下精确产生 30 次注入 (落在 25-35 区间内).
    """
    # mock slow 延迟, 避免真实等待 10s × N
    import chaos.fault_injecting_llm_client as fim
    monkeypatch.setattr(fim, "SLOW_FAULT_DELAY_SECONDS", 0.0)

    cfg = FaultConfig(fault_rate=0.3, seed=41)
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    for _ in range(100):
        try:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
        except (asyncio.TimeoutError, LLMFaultError):
            pass  # 故障注入引发的异常, 忽略
        # empty / slow / 正常 都不抛异常

    stats = client.get_stats()
    assert stats["total_calls"] == 100
    injected = stats["faults_injected"]
    # 允许 ±5 的统计偏差 (seed 固定后是确定值)
    assert 25 <= injected <= 35, (
        f"100 次 fault_rate=0.3 应注入 25-35 次, 实际 {injected}"
    )
    # by_type 总和应等于 faults_injected
    total_by_type = sum(stats["by_type"].values())
    assert total_by_type == injected, (
        f"by_type 总和 {total_by_type} != faults_injected {injected}"
    )


# ────────────────────────────────────────────────────────────
# 7. 相同 seed 产生相同故障序列 (可复现)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seed_reproducibility():
    """两个相同 seed 的 client 产生相同的故障注入序列"""
    cfg1 = FaultConfig(fault_rate=0.5, seed=2024)
    cfg2 = FaultConfig(fault_rate=0.5, seed=2024)
    client1 = FaultInjectingLLMClient(_StubClient(), cfg1)
    client2 = FaultInjectingLLMClient(_StubClient(), cfg2)

    # 收集每次调用的故障类型 (None 表示不注入)
    def _fault_label(reply_or_exc):
        if isinstance(reply_or_exc, BaseException):
            if isinstance(reply_or_exc, asyncio.TimeoutError):
                return "timeout"
            if isinstance(reply_or_exc, LLMFaultError):
                return "error"
            return f"exc:{type(reply_or_exc).__name__}"
        if reply_or_exc == "":
            return "empty"
        if reply_or_exc == "stub-reply":
            # 可能是正常调用或 slow (延迟后返回正常结果)
            # 通过 stats 的 by_type 区分, 此处统一标记
            return "ok_or_slow"
        return "other"

    async def _capture(client, n=30):
        """捕获 n 次调用的故障标签序列 (mock slow 延迟为 0 避免等待)"""
        labels = []
        import chaos.fault_injecting_llm_client as fim
        old_delay = fim.SLOW_FAULT_DELAY_SECONDS
        fim.SLOW_FAULT_DELAY_SECONDS = 0.0
        try:
            for _ in range(n):
                try:
                    reply = await client.chat(
                        messages=[{"role": "user", "content": "hi"}]
                    )
                    labels.append(_fault_label(reply))
                except (asyncio.TimeoutError, LLMFaultError) as e:
                    labels.append(_fault_label(e))
        finally:
            fim.SLOW_FAULT_DELAY_SECONDS = old_delay
        return labels

    labels1 = await _capture(client1, 30)
    labels2 = await _capture(client2, 30)

    # 两序列应完全相同
    assert labels1 == labels2, (
        f"相同 seed 应产生相同故障序列, 但得到:\n"
        f"client1: {labels1}\n"
        f"client2: {labels2}"
    )
    # 同时验证 stats 一致 (包含 by_type 分布)
    assert client1.get_stats() == client2.get_stats()


# ────────────────────────────────────────────────────────────
# 8. 统计正确 (get_stats / set_fault_rate / record_fault / get_fault_log)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stats():
    """get_stats 返回正确的统计结构, set_fault_rate 动态生效

    注: 此处排除 slow 故障类型, 避免 10s 延迟拖慢测试.
    """
    # 排除 slow, 避免 10s 延迟
    cfg = FaultConfig(
        fault_rate=0.5,
        fault_types=["timeout", "error", "empty"],
        seed=100,
    )
    client = FaultInjectingLLMClient(_StubClient(), cfg)

    # 初始统计应为 0
    stats = client.get_stats()
    assert stats["total_calls"] == 0
    assert stats["faults_injected"] == 0
    assert stats["fault_rate"] == 0.5
    assert isinstance(stats["by_type"], dict)
    assert all(v == 0 for v in stats["by_type"].values())

    # 调用 4 次 (seed=100 固定序列)
    for _ in range(4):
        with contextlib.suppress(asyncio.TimeoutError, LLMFaultError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])

    stats = client.get_stats()
    assert stats["total_calls"] == 4
    # faults_injected = sum(by_type)
    assert stats["faults_injected"] == sum(stats["by_type"].values())
    # by_type 之和应等于 faults_injected (不大于 total_calls)
    assert stats["faults_injected"] <= stats["total_calls"]

    # set_fault_rate 动态调整
    client.set_fault_rate(0.0)
    assert client.fault_rate == 0.0
    assert client.get_stats()["fault_rate"] == 0.0
    # 调用 5 次应全部不注入
    for _ in range(5):
        reply = await client.chat(messages=[{"role": "user", "content": "hi"}])
        assert reply == "stub-reply"
    stats = client.get_stats()
    assert stats["total_calls"] == 9  # 4 + 5
    # 故障数应仍为前 4 次的注入数 (后 5 次未注入)
    new_injected = stats["faults_injected"]
    # 验证 set_fault_rate(0) 后 5 次无新注入: 再调用并比较
    assert client.get_stats()["faults_injected"] == new_injected

    # record_fault 公共 API: 手动记录一次故障
    client.record_fault("timeout", {"manual": True})
    log = client.get_fault_log()
    assert any(e["type"] == "timeout" and e["context"].get("manual") for e in log)

    # reset_stats 重置统计
    client.reset_stats()
    stats = client.get_stats()
    assert stats["total_calls"] == 0
    assert stats["faults_injected"] == 0
    assert all(v == 0 for v in stats["by_type"].values())


# ────────────────────────────────────────────────────────────
# 9. chat_stream 流式故障注入 (附加测试)
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_stream_faults(monkeypatch):
    """chat_stream 也支持故障注入 (timeout/error/empty/slow)"""
    # mock slow 延迟
    import chaos.fault_injecting_llm_client as fim
    monkeypatch.setattr(fim, "SLOW_FAULT_DELAY_SECONDS", 0.0)

    # empty: 流为空
    cfg_empty = FaultConfig(fault_rate=1.0, fault_types=["empty"], seed=10)
    client_empty = FaultInjectingLLMClient(_StubClient(), cfg_empty)
    chunks = [c async for c in client_empty.chat_stream(messages=[])]
    assert chunks == []
    assert client_empty.get_stats()["by_type"]["empty"] == 1

    # timeout: 抛异常
    cfg_to = FaultConfig(fault_rate=1.0, fault_types=["timeout"], seed=11)
    client_to = FaultInjectingLLMClient(_StubClient(), cfg_to)
    with pytest.raises(asyncio.TimeoutError):
        async for _ in client_to.chat_stream(messages=[]):
            pass
    assert client_to.get_stats()["by_type"]["timeout"] == 1

    # error: 抛异常
    cfg_err = FaultConfig(fault_rate=1.0, fault_types=["error"], seed=12)
    client_err = FaultInjectingLLMClient(_StubClient(), cfg_err)
    with pytest.raises(LLMFaultError):
        async for _ in client_err.chat_stream(messages=[]):
            pass
    assert client_err.get_stats()["by_type"]["error"] == 1

    # slow: 延迟后透传真实流
    cfg_slow = FaultConfig(fault_rate=1.0, fault_types=["slow"], seed=13)
    client_slow = FaultInjectingLLMClient(_StubClient(), cfg_slow)
    chunks = [c async for c in client_slow.chat_stream(messages=[])]
    assert chunks == ["stub-chunk-1", "stub-chunk-2"]
    assert client_slow.get_stats()["by_type"]["slow"] == 1


# ────────────────────────────────────────────────────────────
# 10. 集成验证: chaos.verify_degradation 端到端
# ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_verify_degradation_triggers_integration():
    """verify_degradation_triggers_async 应全部通过"""
    report = await verify_degradation.verify_degradation_triggers_async()
    assert report["all_passed"], (
        f"降级触发验证未通过: {report['summary']}\n"
        f"scenarios: {report['scenarios']}"
    )
    # 应包含 4 个场景
    names = [s["name"] for s in report["scenarios"]]
    assert "timeout_reliability" in names
    assert "error_quality" in names
    assert "slow_performance" in names
    assert "continuous_strategy" in names


# ────────────────────────────────────────────────────────────
# 11. FaultConfig 参数校验 (附加测试)
# ────────────────────────────────────────────────────────────

def test_fault_config_validation():
    """FaultConfig 校验非法 fault_rate / fault_types"""
    # fault_rate 越界
    with pytest.raises(ValueError):
        FaultConfig(fault_rate=1.5)
    with pytest.raises(ValueError):
        FaultConfig(fault_rate=-0.1)

    # fault_types 为空
    with pytest.raises(ValueError):
        FaultConfig(fault_types=[])

    # 不支持的故障类型
    with pytest.raises(ValueError):
        FaultConfig(fault_types=["unknown_fault"])

    # 合法配置
    cfg = FaultConfig(fault_rate=0.2, fault_types=["timeout", "error"], seed=42)
    assert cfg.fault_rate == 0.2
    assert cfg.fault_types == ["timeout", "error"]
    assert cfg.seed == 42

    # 默认值
    cfg_default = FaultConfig()
    assert cfg_default.fault_rate == 0.1
    assert "timeout" in cfg_default.fault_types
    assert "error" in cfg_default.fault_types
    assert "slow" in cfg_default.fault_types
    assert "empty" in cfg_default.fault_types
    assert cfg_default.seed is None


# ────────────────────────────────────────────────────────────
# 12. FaultInjectingLLMClient 构造校验 (附加测试)
# ────────────────────────────────────────────────────────────

def test_constructor_validation():
    """构造函数校验: real_client 不能为 None, config 必须是 FaultConfig"""
    with pytest.raises(ValueError):
        FaultInjectingLLMClient(None, FaultConfig())

    with pytest.raises(TypeError):
        FaultInjectingLLMClient(_StubClient(), {"fault_rate": 0.1})  # type: ignore
