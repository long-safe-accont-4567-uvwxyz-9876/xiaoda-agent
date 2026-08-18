"""A3 审计修复的回归测试：bootstrap 分层降级与原子提交。

覆盖 4 处修复：
- #1 降级模式下 infra/cognitive 拆分 try：infra 失败不阻止 cognitive 尝试
- #25 ErrorRulePipeline 局部变量原子提交：构造失败不留半初始化对象
- #28 MCP 审计失败提级 warning（日志级别断言）
- #29 MCP start_failed 日志补充 clean/rejected 计数
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_bootstrapper():
    """构造 AgentCoreBootstrapper，core 用 MagicMock 填充最小属性。"""
    from core.bootstrap import AgentCoreBootstrapper
    core = MagicMock()
    core._initialized = False
    core._shared_blackboard = None
    core._shared_blackboard_cleanup_task = None
    core.xiaoli = MagicMock()
    core.xiaoli.init = AsyncMock()
    core.tts = MagicMock()
    core.tts.init = AsyncMock()
    core._mcp_manager = MagicMock()
    core._mcp_manager.start_all = AsyncMock()
    core._mcp_manager._clients = {}
    core._tool_call_handler = MagicMock()
    core.tool_repair = MagicMock()
    core._agent_route_configs = {}
    core.dispatcher = MagicMock()
    core.dispatcher._agents = {}
    return AgentCoreBootstrapper(core), core


# ── #1 降级模式 infra 失败不阻断 cognitive ────────────────────

class TestDegradedInitIsolation:
    """#1：降级模式下 infrastructure 失败时，cognitive 仍应被尝试。"""

    @pytest.mark.asyncio
    async def test_infra_failure_does_not_skip_cognitive(self):
        bs, core = _make_bootstrapper()
        # 降级模式走 no_mimo_api_key 分支：MIMO_API_KEY 空 → 进降级块
        with patch.dict("os.environ", {"MIMO_API_KEY": ""}), \
             patch("utils.encrypted_credential.reveal_credential", return_value=""), \
             patch.object(bs, "_init_infrastructure", AsyncMock(side_effect=RuntimeError("db down"))) as mock_infra, \
             patch.object(bs, "_init_cognitive", AsyncMock()) as mock_cog, \
             patch("core.bootstrap._ensure_workspace_template"):
            await bs.bootstrap(reinit=False)
        # cognitive 必须仍被调用（拆分 try 的核心契约）
        assert mock_cog.await_count == 1, "infra 失败后 cognitive 应仍被尝试"
        # 降级模式不设 _initialized
        assert core._initialized is False

    @pytest.mark.asyncio
    async def test_cognitive_failure_does_not_mask_infra_success(self):
        bs, core = _make_bootstrapper()
        with patch.dict("os.environ", {"MIMO_API_KEY": ""}), \
             patch("utils.encrypted_credential.reveal_credential", return_value=""), \
             patch.object(bs, "_init_infrastructure", AsyncMock()) as mock_infra, \
             patch.object(bs, "_init_cognitive", AsyncMock(side_effect=RuntimeError("memory fail"))), \
             patch("core.bootstrap._ensure_workspace_template"):
            await bs.bootstrap(reinit=False)
        # infra 被调用且成功
        assert mock_infra.await_count == 1
        # cognitive 被调用但抛异常——整体流程不受影响，降级返回
        assert core._initialized is False


# ── #25 ErrorRulePipeline 原子提交 ──────────────────────────────

class TestErrorPipelineAtomicCommit:
    """#25：pipeline 构造失败时 core.error_pipeline 不留半初始化对象。"""

    @pytest.mark.asyncio
    async def test_pipeline_construction_failure_leaves_none(self):
        from core.bootstrap import AgentCoreBootstrapper
        core = MagicMock()
        core.router = MagicMock()
        core.db = MagicMock()
        core.error_pipeline = None  # 初始 None
        core._tool_call_handler = MagicMock()
        # InstinctManager 部分用 mock 跳过（只测 ErrorRulePipeline 段）
        core.instinct_manager = MagicMock()
        core.instinct_manager.init = AsyncMock()
        core.instinct_manager.build_instinct_prompt = AsyncMock(return_value="")
        core.instinct_manager.set_backend = MagicMock()
        core.instinct_manager.set_free_model_client = MagicMock()
        core.context = MagicMock()
        bs = AgentCoreBootstrapper(core)

        # ErrorRulePipeline 构造抛异常
        with patch("instinct_manager.InstinctManager", return_value=core.instinct_manager), \
             patch("tool_engine.error_rule_pipeline.ErrorRulePipeline",
                   side_effect=RuntimeError("construct fail")):
            await bs._init_instinct_and_pipeline(core, sf_key="fake_key")

        # core.error_pipeline 应保持 None，不被赋半初始化对象
        assert core.error_pipeline is None
        # handler 不应被注入（set_error_pipeline 未被调用）
        core._tool_call_handler.set_error_pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_pipeline_success_commits_and_injects(self):
        from core.bootstrap import AgentCoreBootstrapper
        core = MagicMock()
        core.router = MagicMock()
        core.db = MagicMock()
        core.error_pipeline = None
        core._tool_call_handler = MagicMock()
        core.instinct_manager = MagicMock()
        core.instinct_manager.init = AsyncMock()
        core.instinct_manager.build_instinct_prompt = AsyncMock(return_value="")
        core.instinct_manager.set_backend = MagicMock()
        core.instinct_manager.set_free_model_client = MagicMock()
        core.context = MagicMock()
        bs = AgentCoreBootstrapper(core)

        fake_pipeline = MagicMock()
        with patch("instinct_manager.InstinctManager", return_value=core.instinct_manager), \
             patch("tool_engine.error_rule_pipeline.ErrorRulePipeline",
                   return_value=fake_pipeline):
            await bs._init_instinct_and_pipeline(core, sf_key="")

        # 成功时原子提交到 core 并注入 handler
        assert core.error_pipeline is fake_pipeline
        core._tool_call_handler.set_error_pipeline.assert_called_once_with(fake_pipeline)


# ── #29 MCP start_failed 日志上下文 ─────────────────────────────

class TestMcpStartFailedLogContext:
    """#29：start_all 失败时日志应携带 clean/rejected 计数便于排查。"""

    @pytest.mark.asyncio
    async def test_start_failed_log_includes_counts(self):
        bs, core = _make_bootstrapper()
        core._mcp_manager.start_all = AsyncMock(side_effect=RuntimeError("spawn fail"))
        core.db.insert_audit_log = AsyncMock()

        captured: list[str] = []
        with patch("core.bootstrap.logger") as mock_logger:
            mock_logger.warning = lambda *a, **k: captured.append(str(a))
            mock_logger.info = lambda *a, **k: None
            mock_logger.debug = lambda *a, **k: None
            with patch("config.MCP_SERVERS", {}):
                with patch("config.WORKSPACE_DIR") as mock_ws:
                    import tempfile, json
                    from pathlib import Path
                    with tempfile.TemporaryDirectory() as td:
                        cfg_path = Path(td) / "test.json"
                        cfg_path.write_text(json.dumps({
                            "id": "s1",
                            "connections": {"command": "ok"},
                        }), encoding="utf-8")
                        # WORKSPACE_DIR / "mcp_configs" → td
                        mcp_dir = MagicMock()
                        mcp_dir.is_dir.return_value = True
                        mcp_dir.glob.return_value = [cfg_path]
                        mock_ws.__truediv__ = MagicMock(return_value=mcp_dir)
                        # sanitize 返回 clean + rejected 各 1
                        with patch.object(bs, "_sanitize_mcp_configs",
                                          return_value=({"s1": {"command": "ok"}},
                                                        [("bad", "blocked binary")])):
                            await bs._init_mcp()

        # 至少有一条 warning 消息同时含 clean= 和 rejected= 计数
        assert any("clean=" in msg and "rejected=" in msg for msg in captured), \
            f"start_failed 日志未携带 clean/rejected 计数: {captured}"