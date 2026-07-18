"""Phase 1-5 新增模块的单元测试"""
import pytest
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Phase 1: 安全加固 ──────────────────────────────────────

class TestEncryptedCredential:
    """S1: API Key 加密存储"""

    def test_encrypt_decrypt_roundtrip(self):
        from utils.encrypted_credential import EncryptedCredential
        if not EncryptedCredential.is_available():
            pytest.skip("cryptography not installed")
        cred = EncryptedCredential.from_plaintext("sk-test-12345")
        assert cred.decrypt() == "sk-test-12345"

    def test_str_masking(self):
        from utils.encrypted_credential import EncryptedCredential
        if not EncryptedCredential.is_available():
            pytest.skip("cryptography not installed")
        cred = EncryptedCredential.from_plaintext("sk-secret-key")
        s = str(cred)
        assert "sk-secret-key" not in s
        assert "***" in s

    def test_protect_credential_fallback(self):
        from utils.encrypted_credential import protect_credential, reveal_credential
        result = protect_credential("test-key")
        assert reveal_credential(result) == "test-key"

    def test_empty_credential(self):
        from utils.encrypted_credential import protect_credential, reveal_credential
        assert protect_credential("") == ""
        assert reveal_credential("") == ""


class TestCanaryGuard:
    """S6: Canary Token 泄露检测"""

    def test_inject_creates_canary(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        prompt = mgr.inject("You are an agent.")
        assert "SECRET_CANARY" in prompt
        assert len(mgr._canaries) == 1

    def test_check_safe_output(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        mgr.inject("system prompt")
        assert mgr.check("normal output") is True

    def test_check_leaked_output(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        _prompt = mgr.inject("system prompt")
        canary = next(iter(mgr._canaries.keys()))
        assert mgr.check(f"leaked: {canary}") is False

    def test_sanitize_blocks_leak(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        mgr.inject("system prompt")
        canary = next(iter(mgr._canaries.keys()))
        result = mgr.sanitize(f"bad output {canary}")
        assert "拦截" in result

    def test_sanitize_cleans_residual(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        mgr.inject("prompt")
        cleaned = mgr.sanitize("normal [SECRET_CANARY: abc123] text")
        assert "SECRET_CANARY" not in cleaned

    def test_clear(self):
        from utils.canary_guard import CanaryManager
        mgr = CanaryManager()
        mgr.inject("prompt")
        mgr.clear()
        assert len(mgr._canaries) == 0


class TestInstructionHierarchy:
    """S7: 指令层级与内容边界标记"""

    def test_levels_ordered(self):
        from utils.instruction_hierarchy import InstructionLevel
        assert InstructionLevel.SYSTEM > InstructionLevel.APPLICATION
        assert InstructionLevel.APPLICATION > InstructionLevel.USER
        assert InstructionLevel.USER > InstructionLevel.EXTERNAL

    def test_build_contains_anti_injection(self):
        from utils.instruction_hierarchy import InstructionBuilder, InstructionLevel
        builder = InstructionBuilder()
        builder.add(InstructionLevel.SYSTEM, "Be helpful.")
        builder.add(InstructionLevel.EXTERNAL, "untrusted data")
        result = builder.build()
        assert "CRITICAL" in result
        assert "SYSTEM_INSTRUCTIONS" in result
        assert "UNTRUSTED DATA" in result

    def test_build_empty(self):
        from utils.instruction_hierarchy import InstructionBuilder
        builder = InstructionBuilder()
        result = builder.build()
        assert "CRITICAL" in result

    def test_reset(self):
        from utils.instruction_hierarchy import InstructionBuilder, InstructionLevel
        builder = InstructionBuilder()
        builder.add(InstructionLevel.SYSTEM, "test")
        builder.reset()
        result = builder.build()
        assert "test" not in result


class TestSSRFGuard:
    """S5: SSRF 防护"""

    def test_private_ip_blocked(self):
        from utils.ssrf_guard import SSRFGuardV2
        guard = SSRFGuardV2()
        assert not guard.is_safe("http://127.0.0.1/admin")
        assert not guard.is_safe("http://10.0.0.1/internal")
        assert not guard.is_safe("http://192.168.1.1/config")

    def test_normal_url_allowed(self):
        from utils.ssrf_guard import SSRFGuardV2
        guard = SSRFGuardV2()
        # duckduckgo.com 在白名单
        assert guard.is_safe("https://duckduckgo.com/search")

    def test_normalize_url(self):
        from utils.ssrf_guard import SSRFGuardV2
        guard = SSRFGuardV2()
        normalized = guard._normalize_url("https://example.com%2Fpath")
        assert "%2F" not in normalized

    def test_embedded_credentials_removed(self):
        from utils.ssrf_guard import SSRFGuardV2
        guard = SSRFGuardV2()
        normalized = guard._normalize_url("https://user:pass@example.com/path")
        assert "user:pass" not in normalized


class TestRateLimit:
    """S4: 速率限制"""

    @pytest.mark.asyncio
    async def test_token_bucket_allows_burst(self):
        from web.middleware.rate_limit import TokenBucketLimiter
        limiter = TokenBucketLimiter(rate=60, capacity=5)
        # 前5个请求应该通过
        for _ in range(5):
            assert await limiter.acquire("test-client")

    @pytest.mark.asyncio
    async def test_token_bucket_blocks_overflow(self):
        from web.middleware.rate_limit import TokenBucketLimiter
        limiter = TokenBucketLimiter(rate=1, capacity=2)
        await limiter.acquire("client1")
        await limiter.acquire("client1")
        assert not await limiter.acquire("client1")

    @pytest.mark.asyncio
    async def test_token_bucket_refills(self):
        from web.middleware.rate_limit import TokenBucketLimiter
        limiter = TokenBucketLimiter(rate=120, capacity=2)
        await limiter.acquire("c1")
        await limiter.acquire("c1")
        assert not await limiter.acquire("c1")
        await asyncio.sleep(1.0)
        assert await limiter.acquire("c1")


# ── Phase 2: 性能+服务质量 ────────────────────────────────

class TestErrorCodes:
    """Q1: 全局错误码体系"""

    def test_code_format(self):
        from core.error_codes import ErrorCode
        for attr in dir(ErrorCode):
            if attr.startswith("_"):
                continue
            val = getattr(ErrorCode, attr)
            if isinstance(val, str):
                parts = val.split("_")
                assert len(parts) == 3, f"{attr}={val} not 3-part"

    def test_make_error(self):
        from core.error_codes import make_error, ErrorCode
        err = make_error(ErrorCode.LLM_TIMEOUT, "LLM call timed out", provider="mimo")
        assert err.code == "01_2_001"
        assert err.recoverable is True
        assert err.context["provider"] == "mimo"

    def test_critical_not_recoverable(self):
        from core.error_codes import make_error, ErrorCode
        err = make_error(ErrorCode.LLM_CONTENT_FILTER, "blocked")
        assert err.recoverable is False


class TestTripleAxisDegradation:
    """Q2: 三轴退化模型"""

    def test_healthy_state(self):
        from quality.triple_axis_degradation import TripleAxisState, QualityProxy
        state = TripleAxisState(
            availability=True,
            latency_p95=500,
            quality=QualityProxy(),
        )
        assert state.overall_health > 0.8

    def test_unavailable_state(self):
        from quality.triple_axis_degradation import TripleAxisState
        state = TripleAxisState(availability=False)
        assert state.overall_health == 0.0

    def test_quality_degradation_detected(self):
        from quality.triple_axis_degradation import (
            TripleAxisState, QualityProxy, SilentDegradationDetector
        )
        baseline = TripleAxisState(
            availability=True,
            latency_p95=500,
            quality=QualityProxy(schema_violation_rate=0, refusal_rate=0),
        )
        current = TripleAxisState(
            availability=True,
            latency_p95=500,
            quality=QualityProxy(schema_violation_rate=0.5, refusal_rate=0.3),
        )
        detector = SilentDegradationDetector(baseline)
        alerts = detector.check(current)
        assert any("静默退化" in a for a in alerts)

    def test_latency_degradation(self):
        from quality.triple_axis_degradation import (
            TripleAxisState, SilentDegradationDetector
        )
        baseline = TripleAxisState(availability=True, latency_p95=500)
        current = TripleAxisState(availability=True, latency_p95=2000)
        detector = SilentDegradationDetector(baseline)
        alerts = detector.check(current)
        assert any("延迟退化" in a for a in alerts)


class TestDegradationManager:
    """Q4: 降级策略"""

    def setup_method(self):
        from core.degradation_strategy import reset_degradation_strategy
        import core.degradation
        reset_degradation_strategy()
        core.degradation._degradation_manager = None

    def test_initial_full(self):
        from core.degradation import DegradationManager, DegradationLevel
        mgr = DegradationManager()
        assert mgr.level == DegradationLevel.L0_NORMAL
        assert mgr.is_feature_available("tools")

    def test_escalate(self):
        from core.degradation import DegradationManager, DegradationLevel
        mgr = DegradationManager()
        mgr.escalate("LLM down")
        assert mgr.level == DegradationLevel.L1_DEGRADED
        assert not mgr.is_feature_available("image")

    def test_recover(self):
        from core.degradation import DegradationManager, DegradationLevel
        mgr = DegradationManager()
        mgr.escalate("test")
        mgr.escalate("test2")
        mgr.recover()
        assert mgr.level == DegradationLevel.L1_DEGRADED

    def test_emergency_disables_all(self):
        from core.degradation import DegradationManager, DegradationLevel
        mgr = DegradationManager()
        mgr.set_level(DegradationLevel.L3_EMERGENCY, "critical")
        assert not mgr.is_feature_available("tools")
        assert not mgr.is_feature_available("memory")


class TestLazyLoader:
    """P1: 懒加载"""

    def test_deferred_loading(self):
        from core.lazy_loader import LazyLoader
        loader = LazyLoader("collections.OrderedDict")
        assert not loader.is_loaded
        loader.preload()
        assert loader.is_loaded

    def test_preload(self):
        from core.lazy_loader import LazyLoader
        loader = LazyLoader("collections.deque")
        loader.preload()
        assert loader.is_loaded


# ── Phase 3: 自我意识+Doctor ──────────────────────────────

class TestMetaCognition:
    """A1: 元认知引擎"""

    def test_initial_state(self):
        from core.meta_cognition import MetaCognition
        mc = MetaCognition()
        report = mc.get_status_report()
        assert report["health_score"] > 0.5
        assert report["diagnosis"] == "状态良好"

    def test_record_success(self):
        from core.meta_cognition import MetaCognition
        mc = MetaCognition()
        mc.record_success(500.0, 0.9)
        mc.record_success(600.0, 0.95)
        report = mc.get_status_report()
        assert report["total_turns"] == 2
        assert report["avg_response_ms"] > 0

    def test_record_failure(self):
        from core.meta_cognition import MetaCognition
        mc = MetaCognition()
        mc.record_success(500.0)
        mc.record_failure(5000.0)
        report = mc.get_status_report()
        assert report["error_rate"] > 0

    def test_fatigue_increases(self):
        from core.meta_cognition import MetaCognition
        mc = MetaCognition()
        for _ in range(100):
            mc.record_success(100.0)
        report = mc.get_status_report()
        assert report["fatigue"] > 0.3


class TestLearningLoop:
    """A4: 学习反馈闭环"""

    @pytest.mark.asyncio
    async def test_extract_constraint(self, tmp_path):
        from core.learning_loop import LearningLoop
        loop = LearningLoop(persist_path=tmp_path / "ac.json")
        constraint = await loop.process_correction("不要总是说好的", "好的")
        assert constraint is not None
        assert "不要" in constraint

    @pytest.mark.asyncio
    async def test_no_constraint(self, tmp_path):
        from core.learning_loop import LearningLoop
        loop = LearningLoop(persist_path=tmp_path / "ac.json")
        constraint = await loop.process_correction("你好", "你好呀")
        assert constraint is None

    def test_active_constraints(self, tmp_path):
        from core.learning_loop import LearningLoop
        loop = LearningLoop(persist_path=tmp_path / "ac.json")
        loop._active_constraints.extend(["c1", "c2", "c3"])
        assert len(loop.get_active_constraints()) == 3


class TestBehavioralHealth:
    """Dr2: 行为健康评分"""

    def test_healthy_metrics(self):
        from doctor.behavioral_health import BehavioralMetrics
        m = BehavioralMetrics()
        assert m.behavioral_health_score == 1.0
        assert m.health_status == "Optimal"

    def test_degraded_metrics(self):
        from doctor.behavioral_health import BehavioralMetrics
        m = BehavioralMetrics(
            goal_completion_rate=0.6,
            failure_repeat_rate=0.2,
        )
        assert m.behavioral_health_score < 0.9
        assert "Degraded" in m.health_status or "Healthy" in m.health_status

    def test_zombie_detection(self):
        from doctor.behavioral_health import BehavioralMetrics, ZombieDetector
        m = BehavioralMetrics(goal_completion_rate=0.05)
        detector = ZombieDetector()
        alerts = detector.detect(m)
        assert any("Zombie" in a for a in alerts)

    def test_loop_detection(self):
        from doctor.behavioral_health import BehavioralMetrics, ZombieDetector
        m = BehavioralMetrics(loop_signal=0.6)
        detector = ZombieDetector()
        alerts = detector.detect(m)
        assert any("循环" in a for a in alerts)


class TestDoctor:
    """Dr1: Doctor 自检"""

    def test_run_all_checks(self):
        from core.doctor import _create_default_doctor
        doc = _create_default_doctor()
        report = doc.run()
        assert report["total"] > 0
        assert report["passed"] > 0
        assert "results" in report

    def test_format_text(self):
        from core.doctor import _create_default_doctor
        doc = _create_default_doctor()
        report = doc.run()
        text = doc.format_text(report)
        assert "Doctor" in text
        assert "Result" in text


# ── Phase 5: Chaos Engineering ─────────────────────────────

class TestFaultInjection:
    """Ch1: 故障注入"""

    @pytest.mark.asyncio
    async def test_normal_call_passes_through(self):
        from tests.fault_injection import FaultInjectingLLMClient

        class MockClient:
            async def complete(self, messages, **kwargs):
                return {"choices": [{"message": {"content": "ok"}}]}

        client = FaultInjectingLLMClient(MockClient())
        result = await client.complete([{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == "ok"

    @pytest.mark.asyncio
    async def test_timeout_injection(self):
        from tests.fault_injection import FaultInjectingLLMClient, FaultConfig, FaultType

        class MockClient:
            async def complete(self, messages, **kwargs):
                return {"choices": [{"message": {"content": "ok"}}]}

        client = FaultInjectingLLMClient(MockClient(), [
            FaultConfig(FaultType.TIMEOUT, probability=1.0)
        ])
        with pytest.raises(TimeoutError):
            await client.complete([{"role": "user", "content": "hi"}])

    @pytest.mark.asyncio
    async def test_empty_response_injection(self):
        from tests.fault_injection import FaultInjectingLLMClient, FaultConfig, FaultType

        class MockClient:
            async def complete(self, messages, **kwargs):
                return {"choices": [{"message": {"content": "ok"}}]}

        client = FaultInjectingLLMClient(MockClient(), [
            FaultConfig(FaultType.EMPTY_RESPONSE, probability=1.0)
        ])
        result = await client.complete([{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == ""


class TestTNRSelfHeal:
    """Ch3: TNR 安全自愈"""

    def test_test_step(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        needs = tnr.test(lambda: 0.5)
        assert needs is True
        needs_ok = tnr.test(lambda: 0.9)
        assert needs_ok is False

    def test_negotiate(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        result = tnr.negotiate(["restart", "degrade"])
        assert result == "restart"

    def test_recover_success(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        tnr.test(lambda: 0.5)
        tnr.negotiate(["fix"])
        assert tnr.recover(lambda: None) is True

    def test_recover_with_rollback(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        tnr.test(lambda: 0.5)
        tnr.negotiate(["fix"])
        rolled_back = []

        def fail():
            raise RuntimeError("fix failed")

        def rollback():
            rolled_back.append(True)

        assert tnr.recover(fail, rollback_func=rollback) is False
        assert len(rolled_back) == 1

    def test_verify_health_maintained(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        tnr.test(lambda: 0.5)
        tnr.recover(lambda: None)
        assert tnr.verify(lambda: 0.8) is True

    def test_verify_health_dropped(self):
        from core.tnr_self_heal import TNRSelfHeal
        tnr = TNRSelfHeal()
        tnr.test(lambda: 0.8)
        tnr.recover(lambda: None)
        assert tnr.verify(lambda: 0.3) is False
