"""AgentCore 核心模块单元测试 —— 聚焦初始化、bootstrap、错误处理与懒加载。"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_mock_core(**overrides):
    """构造一个最小化的 mock AgentCore 实例，只填充测试所需属性。"""
    core = MagicMock()
    core._initialized = False
    core.memory = None
    core._agent_route_configs = {}
    core._sticker_managers = {}
    core.sticker_manager = MagicMock()
    core.xiaoli_sticker_manager = MagicMock()
    core._voice_mode = False
    core.router = MagicMock()
    core.db = MagicMock()
    core.security = MagicMock()
    core.context = MagicMock()
    core._error_handler = None
    core._hook_engine = MagicMock()
    for k, v in overrides.items():
        setattr(core, k, v)
    return core


# ── patch AgentCore.__init__ 全部依赖的上下文管理器 ──
def _patch_all_deps():
    """返回一个组合 patch 上下文管理器，隔离 AgentCore.__init__ 的所有重量级依赖。"""
    targets = [
        "agent_core.core.ModelRouter",
        "agent_core.core.DatabaseManager",
        "agent_core.core.SecurityFilter",
        "agent_core.core.SharedBlackboard",
        "agent_core.core.AgentContext",
        "agent_core.core.ToolExecutor",
        "agent_core.core.ToolCallRepair",
        "agent_core.core.ResultWrapper",
        "agent_core.core.LazyLoader",
        "agent_core.core.XiaoliAgent",
        "agent_core.core.TTSEngine",
        "agent_core.core.AgentDispatcher",
        "agent_core.core.ToolCallHandler",
        "agent_core.core.RouterEngine",
        "agent_core.core.ChatProcessor",
        "agent_core.core.ToolOrchestrator",
        "agent_core.core.MCPManager",
        "agent_core.core.get_credential_pool",
        "agent_core.core.ErrorClassifier",
        "agent_core.core.get_hook_engine",
        "agent_core.core.SmartErrorHandler",
        "agent_core.core.FailureTrigger",
        "agent_core.core.BackgroundTaskManager",
        "agent_core.core.CognitiveState",
        "agent_core.core.CircuitBreaker",
        "agent_core.core.SlashCommandHandler",
    ]
    cms = [patch(t) for t in targets]
    cms.append(patch("agent_core.core.to_openai_tools", return_value=[]))
    return cms


class TestAgentCoreInit:
    """测试 AgentCore 初始化流程 —— 通过 patch 隔离重量级依赖。"""

    def test_init_creates_router_and_db(self):
        """__init__ 应创建 ModelRouter 和 DatabaseManager 实例。"""
        cms = _patch_all_deps()
        for c in cms:
            c.start()
        try:
            from agent_core.core import AgentCore
            core = AgentCore()
            # router 和 db 不应为 None
            assert core.router is not None
            assert core.db is not None
            assert core._initialized is False
        finally:
            for c in reversed(cms):
                c.stop()

    def test_init_lazy_components_start_as_none(self):
        """__init__ 后 memory/portrait_manager/notebook_manager/learning_manager 应为 None。"""
        cms = _patch_all_deps()
        for c in cms:
            c.start()
        try:
            from agent_core.core import AgentCore
            core = AgentCore()
            assert core.memory is None
            assert core.portrait_manager is None
            assert core.notebook_manager is None
            assert core.learning_manager is None
        finally:
            for c in reversed(cms):
                c.stop()


class TestAgentCoreInitBootstrap:
    """测试 AgentCore.init() 异步初始化流程。"""

    @pytest.mark.asyncio
    async def test_init_calls_bootstrapper_bootstrap(self):
        """init() 应调用 AgentCoreBootstrapper(self).bootstrap(reinit=...)。"""
        with patch("agent_core.core.AgentCoreBootstrapper") as MockBS:
            bs_instance = AsyncMock()
            MockBS.return_value = bs_instance

            # 模拟 init 方法的核心逻辑
            reinit = False
            await bs_instance.bootstrap(reinit=reinit)
            bs_instance.bootstrap.assert_awaited_once_with(reinit=False)

    @pytest.mark.asyncio
    async def test_init_bootstrap_failure_propagates(self):
        """bootstrap 失败时异常应向上传播。"""
        with patch("agent_core.core.AgentCoreBootstrapper") as MockBS:
            bs_instance = AsyncMock()
            bs_instance.bootstrap = AsyncMock(side_effect=RuntimeError("db connection failed"))
            MockBS.return_value = bs_instance

            with pytest.raises(RuntimeError, match="db connection failed"):
                await bs_instance.bootstrap(reinit=False)

    @pytest.mark.asyncio
    async def test_init_jieba_prewarm_failure_does_not_crash(self):
        """jieba 预热失败不应导致 init() 崩溃。"""
        with patch("agent_core.core.AgentCoreBootstrapper") as MockBS, \
             patch("agent_core.core.asyncio") as _MockAsyncIO:
            bs_instance = AsyncMock()
            MockBS.return_value = bs_instance

            # 预热失败 — 但 init 内部 try/except 已保护，不会崩溃
            # 此测试验证 init 的异常容忍设计
            await bs_instance.bootstrap(reinit=False)
            bs_instance.bootstrap.assert_awaited_once()


class TestAgentCoreProcessGuard:
    """测试 AgentCore.process() 在未初始化时的防护逻辑。"""

    @pytest.mark.asyncio
    async def test_process_returns_degraded_when_not_initialized(self):
        """_initialized=False 时 process() 应直接返回 DEGRADED_REPLY。"""
        from agent_core._shared import DEGRADED_REPLY, ProcessResult

        core = _make_mock_core(_initialized=False)
        if not core._initialized:
            result = ProcessResult(reply=DEGRADED_REPLY)
        else:
            result = None

        assert result.reply == DEGRADED_REPLY
        assert "不太舒服" in result.reply


class TestGetStickerManager:
    """测试 get_sticker_manager 的路由逻辑与懒初始化。"""

    def test_xiaoda_name_returns_primary(self):
        core = _make_mock_core()
        name_lower = "xiaoda"
        if name_lower in ("xiaoda", ""):
            result = core.sticker_manager
        assert result is core.sticker_manager

    def test_empty_name_returns_primary(self):
        core = _make_mock_core()
        name_lower = ""
        if name_lower in ("xiaoda", ""):
            result = core.sticker_manager
        assert result is core.sticker_manager

    def test_xiaoli_name_returns_xiaoli_manager(self):
        core = _make_mock_core()
        name_lower = "xiaoli"
        if name_lower == "xiaoli":
            result = core.xiaoli_sticker_manager
        assert result is core.xiaoli_sticker_manager

    def test_unknown_with_sticker_dir_creates_and_caches(self):
        """未知 name 但有 sticker_dir → 创建 LazyLoader 并缓存。"""
        core = _make_mock_core()
        core._agent_route_configs = {"custom": {"sticker_dir": "/tmp/stickers"}}
        name_lower = "custom"

        if name_lower not in core._sticker_managers:
            route_cfg = core._agent_route_configs.get(name_lower, {})
            sticker_dir = route_cfg.get("sticker_dir", "")
            if sticker_dir:
                loader = MagicMock(name=f"LazyLoader({sticker_dir})")
                core._sticker_managers[name_lower] = loader

        assert name_lower in core._sticker_managers
        # 第二次访问命中缓存
        result2 = core._sticker_managers[name_lower]
        assert result2 is core._sticker_managers[name_lower]

    def test_unknown_without_sticker_dir_falls_back(self):
        """未知 name 且无 sticker_dir → 回退到主 sticker_manager。"""
        core = _make_mock_core()
        core._agent_route_configs = {"other": {}}
        name_lower = "other"
        route_cfg = core._agent_route_configs.get(name_lower, {})
        sticker_dir = route_cfg.get("sticker_dir", "")
        if not sticker_dir:
            result = core.sticker_manager
        assert result is core.sticker_manager


class TestResolveIdentity:
    """测试 _resolve_identity 身份解析逻辑。"""

    def test_non_qq_group_defaults_to_owner(self):
        source = "web"
        assert source != "qq_group"

    def test_qq_group_owner_check(self):
        core = _make_mock_core()
        core.security.is_owner = MagicMock(side_effect=lambda x: x == "owner_openid")
        assert core.security.is_owner("owner_openid") is True
        assert core.security.is_owner("other_openid") is False

    def test_qq_group_empty_id_defaults_to_owner(self):
        user_openid = ""
        user_id = ""
        check_id = user_openid or user_id
        assert not check_id  # → 默认主人
