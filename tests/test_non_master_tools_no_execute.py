"""TDD 测试：非主人 QQ 消息工具白名单不含 EXECUTE 类工具（VULN-26）。

    验证 ALLOWED_NON_MASTER_TOOLS 只包含只读/纯对话工具，
    不含 shell_command/python_executor/write_file/edit_file 等 EXECUTE 类工具。
    """

from agent_core.message_processor import MessageProcessorMixin

# EXECUTE 类工具（不应出现在非主人白名单中）
_EXECUTE_TOOLS = frozenset({
    "shell_command",
    "execute_code",
    "python_executor",
    "write_file",
    "edit_file",
    "create_file",
    "delete_file",
    "file_manager",
})


def test_non_master_whitelist_excludes_execute_tools():
    """ALLOWED_NON_MASTER_TOOLS 不应含任何 EXECUTE 类工具"""
    whitelist = MessageProcessorMixin.ALLOWED_NON_MASTER_TOOLS
    forbidden = whitelist & _EXECUTE_TOOLS
    assert not forbidden, (
        f"非主人工具白名单不应含 EXECUTE 工具，但发现: {forbidden}"
    )


def test_non_master_whitelist_is_readonly_or_chat_only():
    """非主人白名单应仅含只读查询/纯对话工具"""
    allowed = MessageProcessorMixin.ALLOWED_NON_MASTER_TOOLS
    # 至少应保留一些只读/对话工具
    assert len(allowed) > 0, "非主人白名单不应为空"
    for tool in allowed:
        assert tool not in _EXECUTE_TOOLS, f"非主人白名单不应含 {tool}"
