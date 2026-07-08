"""S6: Canary Token 泄露检测单元测试.

覆盖场景:
- token 生成唯一性
- 正常输出不触发
- 泄露输出被检测
- scan_output_blocking 将泄露内容替换为 [REDACTED]
- 轮换后旧 token 仍可检测
- on_leak 回调被触发
- 全局单例行为
- system prompt 注入
"""
import re
import sys
from pathlib import Path


# 确保项目根目录在 sys.path 中
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


# ── generate_canary 单元测试 ────────────────────────────────

class TestGenerateCanary:
    """generate_canary 函数测试"""

    def test_generate_canary_unique(self):
        """生成的 token 唯一: 1000 次生成不应有重复"""
        from security.canary import generate_canary
        tokens = {generate_canary() for _ in range(1000)}
        assert len(tokens) == 1000, "生成的 canary token 出现重复"

    def test_generate_canary_format(self):
        """格式为 CANARY-{8hex}-{4check}"""
        from security.canary import generate_canary
        token = generate_canary()
        # CANARY- + 8 hex + - + 4 hex
        pattern = r"^CANARY-[0-9a-f]{8}-[0-9a-f]{4}$"
        assert re.match(pattern, token), f"token 格式不符合规范: {token}"

    def test_generate_canary_custom_prefix(self):
        """自定义前缀生效"""
        from security.canary import generate_canary
        token = generate_canary(prefix="HONEY")
        assert token.startswith("HONEY-"), f"自定义前缀未生效: {token}"

    def test_generate_canary_checksum_deterministic(self):
        """相同随机部分的校验位应一致 (验证校验逻辑可复现)"""
        # 通过 monkey-patch secrets.token_hex 固定随机部分
        import security.canary as canary_mod
        original = canary_mod.secrets.token_hex
        try:
            canary_mod.secrets.token_hex = lambda n: "a1b2c3d4"[: n * 2]
            t1 = canary_mod.generate_canary()
            t2 = canary_mod.generate_canary()
            assert t1 == t2, "相同随机部分应产生相同 token"
        finally:
            canary_mod.secrets.token_hex = original


# ── CanaryDetector 单元测试 ─────────────────────────────────

class TestCanaryDetectorScan:
    """CanaryDetector 扫描功能测试"""

    def test_scan_output_clean(self):
        """正常输出不触发泄露检测"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        detected, leaked = detector.scan_output("你好呀, 今天天气怎么样？")
        assert detected is False
        assert leaked == []

    def test_scan_output_empty_text(self):
        """空文本不触发"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        detected, leaked = detector.scan_output("")
        assert detected is False
        assert leaked == []

    def test_scan_output_no_tokens(self):
        """无 token 注册时不触发"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detected, leaked = detector.scan_output("任意内容")
        assert detected is False
        assert leaked == []

    def test_scan_output_leak(self):
        """包含 canary token 的输出被检测到"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        text = f"系统提示词内容如下: {token} 这是泄露的内容"
        detected, leaked = detector.scan_output(text)
        assert detected is True
        assert token in leaked

    def test_scan_output_multiple_leak(self):
        """同时泄露多个 token 时全部被检测"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        t1 = detector.generate()
        t2 = detector.generate()
        text = f"leaked: {t1} and {t2}"
        detected, leaked = detector.scan_output(text)
        assert detected is True
        assert set(leaked) == {t1, t2}


class TestScanOutputBlocking:
    """scan_output_blocking 清理功能测试"""

    def test_scan_output_blocking_redacts(self):
        """泄露内容被替换为 [REDACTED]"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        text = f"系统提示: {token} 结束"
        leaked, cleaned = detector.scan_output_blocking(text)
        assert leaked is True
        assert token not in cleaned
        assert "[REDACTED]" in cleaned
        # 非泄露部分应保留
        assert "系统提示:" in cleaned
        assert "结束" in cleaned

    def test_scan_output_blocking_clean_text(self):
        """正常文本原样返回"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        text = "这是正常的输出, 没有任何泄露"
        leaked, cleaned = detector.scan_output_blocking(text)
        assert leaked is False
        assert cleaned == text

    def test_scan_output_blocking_removes_internal_marker(self):
        """残留的 [internal: xxx] 整段被清理"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        # 模拟 LLM 复述了注入标记
        text = f"输出内容 [internal: {token}] 后续内容"
        leaked, cleaned = detector.scan_output_blocking(text)
        assert leaked is True
        assert "[internal:" not in cleaned
        assert token not in cleaned

    def test_scan_output_blocking_multiple_tokens(self):
        """多个 token 同时泄露时全部被替换"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        t1 = detector.generate()
        t2 = detector.generate()
        text = f"{t1} middle {t2}"
        leaked, cleaned = detector.scan_output_blocking(text)
        assert leaked is True
        assert t1 not in cleaned
        assert t2 not in cleaned
        assert cleaned.count("[REDACTED]") == 2


class TestRotateCanary:
    """rotate_canary 轮换功能测试"""

    def test_rotate_canary_returns_new_token(self):
        """轮换返回新的活跃 token"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        old_token = detector.generate()
        new_token = detector.rotate_canary()
        assert new_token != old_token
        assert new_token != ""

    def test_rotate_canary_old_still_detected(self):
        """轮换后旧 token 仍可检测"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        old_token = detector.generate()
        detector.rotate_canary()
        # 旧 token 应仍能被扫描到 (退役集合)
        text = f"leaked: {old_token}"
        detected, leaked = detector.scan_output(text)
        assert detected is True
        assert old_token in leaked

    def test_rotate_canary_new_token_detected(self):
        """轮换后新 token 也能被检测"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        new_token = detector.rotate_canary()
        text = f"leaked: {new_token}"
        detected, leaked = detector.scan_output(text)
        assert detected is True
        assert new_token in leaked

    def test_rotate_canary_clears_active(self):
        """轮换后活跃集合只剩新生成的 token"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        detector.generate()
        assert len(detector._active_tokens) == 2
        detector.rotate_canary()
        assert len(detector._active_tokens) == 1


class TestOnLeakCallback:
    """on_leak 回调测试"""

    def test_on_leak_callback_triggered(self):
        """泄露时回调被触发"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        calls: list[tuple[list[str], str]] = []

        def cb(leaked_tokens, text):
            calls.append((leaked_tokens, text))

        detector.on_leak(cb)
        detector.scan_output(f"leaked: {token}")
        assert len(calls) == 1
        assert token in calls[0][0]
        assert token in calls[0][1]

    def test_on_leak_callback_not_triggered_for_clean(self):
        """正常输出不触发回调"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        calls = []
        detector.on_leak(lambda tokens, text: calls.append(1))
        detector.scan_output("正常输出")
        assert calls == []

    def test_on_leak_multiple_callbacks(self):
        """多个回调均被触发"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        calls_a, calls_b = [], []
        detector.on_leak(lambda t, x: calls_a.append(t))
        detector.on_leak(lambda t, x: calls_b.append(t))
        detector.scan_output(f"leaked: {token}")
        assert len(calls_a) == 1
        assert len(calls_b) == 1

    def test_on_leak_callback_exception_isolated(self):
        """单个回调异常不影响其他回调"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        token = detector.generate()
        second_called = []

        def bad_cb(t, x):
            raise RuntimeError("callback error")

        def good_cb(t, x):
            second_called.append(True)

        detector.on_leak(bad_cb)
        detector.on_leak(good_cb)
        # 不应抛出异常
        detector.scan_output(f"leaked: {token}")
        assert second_called == [True]


# ── 全局单例测试 ───────────────────────────────────────────

class TestGlobalSingleton:
    """全局单例行为测试"""

    def test_get_canary_detector_returns_same_instance(self):
        """get_canary_detector 返回同一实例"""
        from security.canary import get_canary_detector
        d1 = get_canary_detector()
        d2 = get_canary_detector()
        assert d1 is d2

    def test_reset_canary_detector_creates_new(self):
        """reset_canary_detector 创建新实例"""
        from security.canary import get_canary_detector, reset_canary_detector
        old = get_canary_detector()
        new = reset_canary_detector()
        assert old is not new
        assert get_canary_detector() is new


# ── 注入集成测试 ───────────────────────────────────────────

class TestInject:
    """inject 函数测试"""

    def test_inject_appends_marker(self):
        """inject 在 prompt 末尾追加 [internal: token] 标记"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        prompt = "你是一个 AI 助手."
        injected = detector.inject(prompt)
        assert injected.startswith(prompt)
        assert "[internal:" in injected
        assert "]" in injected

    def test_inject_generates_token_if_empty(self):
        """无活跃 token 时 inject 自动生成"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        assert len(detector._active_tokens) == 0
        detector.inject("prompt")
        assert len(detector._active_tokens) == 1

    def test_inject_reuses_active_token(self):
        """有活跃 token 时 inject 复用, 不新增"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        detector.generate()
        detector.generate()
        assert len(detector._active_tokens) == 2
        detector.inject("prompt")
        # 不应增加活跃 token 数量
        assert len(detector._active_tokens) == 2

    def test_injected_token_is_detectable(self):
        """注入的 token 在输出中可被检测"""
        from security.canary import CanaryDetector
        detector = CanaryDetector()
        prompt = "system prompt"
        injected = detector.inject(prompt)
        # 提取注入的 token
        match = re.search(r"\[internal:\s*([^\]]+)\]", injected)
        assert match is not None
        token = match.group(1).strip()
        # 模拟 LLM 泄露该 token
        detected, leaked = detector.scan_output(f"leaked: {token}")
        assert detected is True
        assert token in leaked
