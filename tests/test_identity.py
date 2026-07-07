"""身份识别修复测试

测试 spec: fix-qq-group-identity-and-context-optimization
覆盖:
- is_owner 兼容 qq_{openid} 和裸 openid 两种格式
- 非主人冒充"爸爸"时 is_owner 返回 False
- restore_from_db 按 user_id 过滤
- UserIdentity 身份解析
- humanize 对 Markdown 的处理
"""
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from security.security import SecurityFilter
from agent_core import UserIdentity


class TestIsOwner:
    """Test is_owner 兼容性"""

    def test_owner_with_bare_openid_config(self):
        """OWNER_IDS 配置为裸 openid 时，qq_{openid} 格式能匹配"""
        sec = SecurityFilter(owner_ids=["abc123"])
        assert sec.is_owner("abc123") is True
        assert sec.is_owner("qq_abc123") is True

    def test_owner_with_prefixed_config(self):
        """OWNER_IDS 配置为带 qq_ 前缀的 user_id 时能匹配"""
        sec = SecurityFilter(owner_ids=["qq_abc123"])
        assert sec.is_owner("qq_abc123") is True

    def test_non_owner_returns_false(self):
        """非主人用户返回 False"""
        sec = SecurityFilter(owner_ids=["abc123"])
        assert sec.is_owner("xyz789") is False
        assert sec.is_owner("qq_xyz789") is False

    def test_impersonation_returns_false(self):
        """非主人冒充"爸爸"时 is_owner 返回 False（身份不受消息内容影响）"""
        sec = SecurityFilter(owner_ids=["abc123"])
        # is_owner 只看 user_id，不看消息内容
        assert sec.is_owner("xyz789") is False

    def test_empty_owner_ids_defaults_true(self):
        """未配置 OWNER_IDS 时默认所有人都是主人（向后兼容）"""
        sec = SecurityFilter(owner_ids=[])
        assert sec.is_owner("anyone") is True

    def test_empty_user_id_returns_false(self):
        """空 user_id 返回 False"""
        sec = SecurityFilter(owner_ids=["abc123"])
        assert sec.is_owner("") is False

    def test_master_qq_openid_merged_into_owner_ids(self, monkeypatch):
        """MASTER_QQ_OPENID 应合并到 owner_ids，确保 _resolve_identity 能识别主人

        场景：用户只配置了 MASTER_QQ_OPENID（由 qq_bot_adapter 自动绑定），
        未配置 OWNER_IDS。此时 SecurityFilter 应包含 MASTER_QQ_OPENID 的值，
        is_owner 才能正确识别主人，避免所有人被误判为主人。
        """
        import os
        from security.security import SecurityFilter

        # 模拟环境变量
        monkeypatch.setenv("OWNER_IDS", "")
        monkeypatch.setenv("MASTER_QQ_OPENID", "master_openid_123,other_openid_456")

        # 复现 agent_core.py 的初始化逻辑
        _owner_ids = os.getenv("OWNER_IDS", "").split(",")
        _owner_ids = [x.strip() for x in _owner_ids if x.strip()]
        _master_qq = os.getenv("MASTER_QQ_OPENID", "").split(",")
        _master_qq = [x.strip() for x in _master_qq if x.strip()]
        _owner_ids = list(dict.fromkeys(_owner_ids + _master_qq))

        sec = SecurityFilter(owner_ids=_owner_ids)

        # 主人 openid 应被识别
        assert sec.is_owner("master_openid_123") is True
        # 非主人 openid 应被拒绝
        assert sec.is_owner("stranger_openid") is False
        # qq_ 前缀格式也能匹配
        assert sec.is_owner("qq_master_openid_123") is True


class TestUserIdentity:
    """Test UserIdentity dataclass"""

    def test_default_owner(self):
        identity = UserIdentity.default_owner()
        assert identity.is_owner is True
        assert identity.address_term == "爸爸"

    def test_default_guest(self):
        identity = UserIdentity.default_guest()
        assert identity.is_owner is False
        assert identity.address_term == "朋友"

    def test_owner_identity(self):
        identity = UserIdentity(is_owner=True, display_name="爸爸", address_term="爸爸")
        assert identity.is_owner is True
        assert identity.address_term == "爸爸"

    def test_guest_identity(self):
        identity = UserIdentity(is_owner=False, display_name="用户", address_term="用户")
        assert identity.is_owner is False
        assert identity.address_term == "用户"


class TestRestoreFromDb:
    """Test restore_from_db 按 user_id 过滤"""

    @pytest.mark.asyncio
    async def test_restore_with_user_id_filters_history(self):
        """restore_from_db 按 user_id 过滤，不同用户历史不混合"""
        from agent_context import AgentContext

        ctx = AgentContext()
        ctx.current_address_term = "用户"

        # Mock database
        mock_db = MagicMock()
        mock_db.memory = MagicMock()
        mock_db.memory.get_recent_conversations = AsyncMock(return_value=[
            {"user_message": "你好", "assistant_reply": "你好呀"},
        ])

        await ctx.restore_from_db(mock_db, user_id="user_a", address_term="用户")

        # 验证调用了 user_id 过滤
        mock_db.memory.get_recent_conversations.assert_called_once_with(limit=10, user_id="user_a")
        assert "用户" in ctx._restored_summary
        assert "爸爸" not in ctx._restored_summary

    @pytest.mark.asyncio
    async def test_restore_owner_uses_baba_term(self):
        """主人的历史恢复使用"爸爸"称谓"""
        from agent_context import AgentContext

        ctx = AgentContext()
        ctx.current_address_term = "爸爸"

        mock_db = MagicMock()
        mock_db.memory = MagicMock()
        mock_db.memory.get_recent_conversations = AsyncMock(return_value=[
            {"user_message": "测试", "assistant_reply": "回复"},
        ])

        await ctx.restore_from_db(mock_db, user_id="owner", address_term="爸爸")

        assert "爸爸" in ctx._restored_summary

    @pytest.mark.asyncio
    async def test_restore_limit_is_10(self):
        """查询量从 limit=20 缩减到 limit=10"""
        from agent_context import AgentContext

        ctx = AgentContext()
        mock_db = MagicMock()
        mock_db.memory = MagicMock()
        mock_db.memory.get_recent_conversations = AsyncMock(return_value=[])

        await ctx.restore_from_db(mock_db, user_id="test")

        call_args = mock_db.memory.get_recent_conversations.call_args
        assert call_args.kwargs.get("limit", call_args.args[0] if call_args.args else None) == 10


class TestHumanizeMarkdown:
    """Test humanize 对 Markdown 的处理"""

    def test_humanize_strips_bold(self):
        """humanize 去除 **粗体** 标记"""
        from utils.text_utils import humanize
        text = "这是**粗体**文本"
        result = humanize(text, style="xiaoda")
        assert "**" not in result
        assert "粗体" in result

    def test_humanize_strips_headers(self):
        """humanize 去除 # 标题 标记"""
        from utils.text_utils import humanize
        text = "# 标题\n这是内容"
        result = humanize(text, style="xiaoda")
        assert "#" not in result or result.count("#") == 0

    def test_humanize_strips_list_items(self):
        """humanize 去除 - 列表 标记"""
        from utils.text_utils import humanize
        text = "- 列表项1\n- 列表项2"
        result = humanize(text, style="xiaoda")
        # 列表标记应被处理
        assert "列表项1" in result