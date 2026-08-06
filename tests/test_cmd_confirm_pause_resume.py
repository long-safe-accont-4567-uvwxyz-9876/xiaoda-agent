"""多平台共用上下文后续修复的 TDD 测试：
- ① 权限模式（随心/绕过/全自动）下 is_command_allowed 放行非黑名单命令（不再弹确认）
- ② 命令确认流程改为“暂停-确认-继续”，而非“先失败”
- ③ 用户拒绝/超时的命令不计入熔断器工具失败
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from security.permission_manager import PermissionManager
from tool_engine.tool_executor import ToolExecutor
from tool_engine.tool_registry import ToolResult


@pytest.fixture(autouse=True)
def _ensure_tools_registered():
    from tool_engine.tool_registry import register_builtin_tools_lazy
    register_builtin_tools_lazy()
    yield


@pytest.fixture(autouse=True)
def _isolate_permission_state(tmp_path, monkeypatch):
    """隔离权限持久化状态，避免测试污染真实配置或互相继承权限档位。

    set_mode() 现在会落盘（permission_mode.json）。不隔离的话：
    - 测试会改写开发者真实的权限模式；
    - 新建 PermissionManager() 时从磁盘读到上次测试写入的档位（如 GOAT），
      导致期望 DEFAULT 行为的测试失败。
    此 fixture 把持久化文件指向临时目录，并把全局管理器重置为 DEFAULT。
    """
    import security.permission_manager as m
    monkeypatch.setattr(m, "_PERMISSION_FILE", str(tmp_path / "permission_mode.json"))
    pm = m.get_permission_manager()
    pm.set_mode(m.PermissionMode.DEFAULT)
    pm.clear_cwd()
    pm.set_whitelist([])
    pm.clear_audit_log()
    yield


@pytest.fixture
def pm():
    return PermissionManager()


@pytest.fixture
def global_pm():
    """execute() 走全局单例 get_permission_manager()，集成测试需用它并清理状态。"""
    from security.permission_manager import get_permission_manager
    pm = get_permission_manager()
    pm.clear_cwd()
    pm.set_whitelist([])
    pm.clear_audit_log()
    return pm


@pytest.fixture
def executor():
    return ToolExecutor()


# ── ① is_command_allowed 尊重权限模式 ──────────────────────────
class TestCommandAllowedRespectsMode:
    def test_goat_unknown_command_allowed(self, pm):
        pm.set_mode("goat")
        allowed, _, needs_conf = pm.is_command_allowed("wget http://example.com/x")
        assert allowed is True
        assert needs_conf is False

    def test_goat_dangerous_still_blocked(self, pm):
        pm.set_mode("goat")
        allowed, reason, needs_conf = pm.is_command_allowed("rm -rf /")
        assert allowed is False
        assert needs_conf is False
        assert "危险" in reason or "拦截" in reason

    @pytest.mark.parametrize("mode", ["bypass", "auto"])
    def test_bypass_auto_unknown_command_allowed(self, pm, mode):
        pm.set_mode(mode)
        allowed, _, needs_conf = pm.is_command_allowed("wget http://example.com/x")
        assert allowed is True
        assert needs_conf is False

    def test_default_still_needs_confirmation(self, pm):
        pm.set_mode("default")
        allowed, _, needs_conf = pm.is_command_allowed("wget http://example.com/x")
        assert allowed is False
        assert needs_conf is True


# ── ToolResult.user_decision 标记（fix ③ 依赖） ────────────────
class TestToolResultUserDecision:
    def test_fail_can_mark_user_decision(self):
        r = ToolResult.fail("命令已被用户拒绝执行", user_decision=True)
        assert r.success is False
        assert r.user_decision is True

    def test_fail_default_not_user_decision(self):
        r = ToolResult.fail("真实故障")
        assert r.user_decision is False


# ── ② 暂停-确认-继续：决策单元测试 ─────────────────────────────
class TestAwaitCmdConfirmation:
    @pytest.mark.asyncio
    async def test_returns_allow(self):
        ex = ToolExecutor(decision_provider=lambda rid: "allow")
        assert await ex._await_cmd_confirmation("echo hi", "shell_command") == "allow"

    @pytest.mark.asyncio
    async def test_returns_deny(self):
        ex = ToolExecutor(decision_provider=lambda rid: "deny")
        assert await ex._await_cmd_confirmation("echo hi", "shell_command") == "deny"

    @pytest.mark.asyncio
    async def test_timeout_when_no_decision(self):
        ex = ToolExecutor(decision_provider=lambda rid: None, cmd_confirm_timeout=0.2)
        assert await ex._await_cmd_confirmation("echo hi", "shell_command") == "timeout"


# ── ② 暂停-确认-继续：execute 集成 ─────────────────────────────
class TestExecuteConfirmPauseResume:
    @pytest.mark.asyncio
    async def test_deny_returns_user_decision_failure(self, executor, global_pm):
        global_pm.set_cwd("/tmp")
        global_pm.set_whitelist([])
        executor._decision_provider = lambda rid: "deny"
        result = await executor.execute("shell_command", {"command": "cargo build"})
        assert not result.success
        assert result.user_decision is True
        assert "拒绝" in (result.error or "")

    @pytest.mark.asyncio
    async def test_timeout_returns_user_decision_failure(self, executor, global_pm):
        global_pm.set_cwd("/tmp")
        global_pm.set_whitelist([])
        executor._decision_provider = lambda rid: None
        executor._cmd_confirm_timeout = 0.2
        result = await executor.execute("shell_command", {"command": "cargo build"})
        assert not result.success
        assert result.user_decision is True
        assert "超时" in (result.error or "")

    @pytest.mark.asyncio
    async def test_allow_proceeds_past_confirmation(self, executor, global_pm, tmp_path):
        """确认放行后，命令不再以“需确认”失败，而是继续执行并返回真实输出。"""
        global_pm.set_cwd(str(tmp_path))
        global_pm.set_whitelist([])
        executor._decision_provider = lambda rid: "allow"
        result = await executor.execute("shell_command", {"command": "echo confirm-proceed"})
        # 核心行为：放行后命令真实执行成功，且输出包含预期内容
        assert result.success, f"确认放行后命令应成功执行: {result.error}"
        assert "confirm-proceed" in str(result.data)
        # 不应是“需确认/拒绝/超时”的用户决策失败
        assert not getattr(result, "user_decision", False), f"不应标记用户决策失败: {result.error}"
        assert "确认" not in (result.error or "")


# ── ④ 权限模式持久化 ───────────────────────────────────────
class TestPermissionModePersistence:
    def test_set_mode_persists_and_reloads(self, monkeypatch, tmp_path):
        import security.permission_manager as m
        persist_file = tmp_path / "perm.json"
        monkeypatch.setattr(m, "_PERMISSION_FILE", str(persist_file))
        # 触发持久化
        m._persist_mode(m.PermissionMode.GOAT)
        assert persist_file.exists()
        # 模拟重启：新实例从磁盘加载持久化模式
        loaded = m._load_persisted_mode()
        assert loaded == m.PermissionMode.GOAT

    def test_load_missing_file_returns_none(self, monkeypatch, tmp_path):
        import security.permission_manager as m
        monkeypatch.setattr(m, "_PERMISSION_FILE", str(tmp_path / "nope.json"))
        assert m._load_persisted_mode() is None

    def test_set_mode_writes_file(self, monkeypatch, tmp_path):
        import security.permission_manager as m
        persist_file = tmp_path / "perm.json"
        monkeypatch.setattr(m, "_PERMISSION_FILE", str(persist_file))
        pm = PermissionManager()
        pm.set_mode("goat")
        assert persist_file.exists()
        assert m._load_persisted_mode() == m.PermissionMode.GOAT


# ── ⑤ 随心模式跳过"未授权工作目录"墙 ────────────────────────
class TestGoatSkipsCwdWall:
    @pytest.mark.asyncio
    async def test_goat_executes_shell_without_authorized_cwd(self, executor, global_pm):
        """随心模式下，未授权工作目录也能执行非黑名单命令，不再被 cwd 墙拦截。"""
        global_pm.clear_cwd()          # 未授权任何工作目录
        global_pm.set_mode("goat")     # 随心最高权限
        global_pm.set_whitelist([])
        executor._decision_provider = lambda rid: "allow"
        result = await executor.execute("shell_command", {"command": "echo goat-skip-cwd"})
        assert not getattr(result, "user_decision", False), f"不应需要确认: {result.error}"
        assert "未授权工作目录" not in (result.error or ""), f"不应被 cwd 墙拦截: {result.error}"

    @pytest.mark.asyncio
    async def test_default_still_requires_cwd(self, executor, global_pm):
        """默认模式下，未授权工作目录仍被 cwd 墙拦截。"""
        global_pm.clear_cwd()
        global_pm.set_mode("default")
        global_pm.set_whitelist([])
        result = await executor.execute("shell_command", {"command": "echo need-cwd"})
        assert "未授权工作目录" in (result.error or ""), f"默认模式应被 cwd 墙拦截: {result.error}"


# ── ③ 熔断器不把用户决策失败计入工具故障 ───────────────────────
class TestCircuitBreakerSkipsUserDecision:
    def test_real_failure_counts(self):
        from agent_core._shared import _current_request_ctx
        from core.circuit_breaker import CircuitBreaker, CognitiveState
        cb = CircuitBreaker()
        st = CognitiveState()
        # 真实失败 → consecutive_fails +1
        cb.on_failure(st, is_tool=True)
        assert st.consecutive_fails == 1
        assert st.tool_fail_rate == 1.0

    def test_decision_helper_marks_real_failure(self):
        """真实失败（非用户决策）应计入熔断。"""
        from core.circuit_breaker import CircuitBreaker, CognitiveState
        cb = CircuitBreaker()
        st = CognitiveState()
        cb.on_failure(st, is_tool=True)
        assert st.consecutive_fails == 1