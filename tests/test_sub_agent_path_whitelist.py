"""子代理路径白名单机制测试

覆盖:
    - SubAgentConfig 新增 allowed_paths / forbidden_paths 字段
    - ToolCallHandler._check_path_whitelist 校验逻辑
    - 错误码 E_TOOL006 (Path forbidden by sub-agent whitelist)
    - 黑名单优先 / 白名单为空允许所有 / 白名单匹配规则
    - config/agents/*.json 配置文件加载后字段正确
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from agent_dispatcher import SubAgentConfig
from core.error_codes import ErrorCodeEnum
from tool_engine.tool_call_handler import ToolCallHandler, _extract_path_from_args


# ============================================================
# 工具函数：构造测试用 SubAgentConfig
# ============================================================
def _make_config(
    name: str = "test_agent",
    allowed_paths: list[str] | None = None,
    forbidden_paths: list[str] | None = None,
) -> SubAgentConfig:
    return SubAgentConfig(
        name=name,
        display_name=name,
        provider="mimo",
        model="mimo-v2.5-pro",
        allowed_paths=allowed_paths or [],
        forbidden_paths=forbidden_paths or [],
    )


def _make_handler(agent_config: SubAgentConfig | None = None) -> ToolCallHandler:
    """构造一个仅用于路径校验的 handler（不依赖完整运行时）。"""
    return ToolCallHandler(
        tool_executor=None,
        tool_repair=None,
        clean_reply_callback=lambda x: x,
        agent_config=agent_config,
    )


# ============================================================
# 1. SubAgentConfig 字段定义
# ============================================================
def test_sub_agent_config_has_path_fields():
    """SubAgentConfig 默认包含 allowed_paths / forbidden_paths 字段，默认为空列表"""
    cfg = SubAgentConfig(name="x", display_name="x", provider="mimo", model="m")
    assert cfg.allowed_paths == []
    assert cfg.forbidden_paths == []
    # 可独立设置
    cfg.allowed_paths = ["a/*"]
    cfg.forbidden_paths = ["*.env"]
    assert cfg.allowed_paths == ["a/*"]
    assert cfg.forbidden_paths == ["*.env"]


# ============================================================
# 2. 无 SubAgentConfig 时允许所有
# ============================================================
def test_no_config_allows_all():
    """无 SubAgentConfig 时允许所有路径（主体 Agent）"""
    handler = _make_handler(agent_config=None)
    allowed, reason = handler._check_path_whitelist("/etc/passwd", agent_config=None)
    assert allowed is True
    assert "main agent" in reason


# ============================================================
# 3. 白名单为空时允许所有
# ============================================================
def test_empty_whitelist_allows_all():
    """allowed_paths 和 forbidden_paths 均为空时允许所有路径"""
    cfg = _make_config(allowed_paths=[], forbidden_paths=[])
    handler = _make_handler(agent_config=cfg)
    allowed, reason = handler._check_path_whitelist("any/path/file.txt", cfg)
    assert allowed is True
    assert "no whitelist restriction" in reason


# ============================================================
# 4. 黑名单匹配时拒绝
# ============================================================
def test_forbidden_path_blocked():
    """黑名单匹配时拒绝，即使路径也在白名单中"""
    cfg = _make_config(
        allowed_paths=["assets/*"],
        forbidden_paths=["*.env", "secret/*"],
    )
    handler = _make_handler(agent_config=cfg)
    # .env 文件被黑名单拦截
    allowed, reason = handler._check_path_whitelist("config/prod.env", cfg)
    assert allowed is False
    assert "forbidden" in reason
    assert "*.env" in reason
    # secret/ 目录被黑名单拦截
    allowed2, reason2 = handler._check_path_whitelist("secret/keys.json", cfg)
    assert allowed2 is False
    assert "forbidden" in reason2


# ============================================================
# 5. 白名单匹配时允许
# ============================================================
def test_allowed_path_permitted():
    """路径匹配白名单模式时允许"""
    cfg = _make_config(
        allowed_paths=["assets/stickers/xiaoli/*", "config/agents/xiaoli_personality.md"],
        forbidden_paths=["*.env"],
    )
    handler = _make_handler(agent_config=cfg)
    # 匹配 glob 模式
    allowed, reason = handler._check_path_whitelist("assets/stickers/xiaoli/happy.png", cfg)
    assert allowed is True
    assert "allowed" in reason
    # 精确匹配
    allowed2, reason2 = handler._check_path_whitelist("config/agents/xiaoli_personality.md", cfg)
    assert allowed2 is True


# ============================================================
# 6. 不匹配白名单时拒绝
# ============================================================
def test_non_matching_path_blocked():
    """路径不在白名单中且未被黑名单匹配时拒绝"""
    cfg = _make_config(
        allowed_paths=["assets/stickers/xiaoli/*"],
        forbidden_paths=[],
    )
    handler = _make_handler(agent_config=cfg)
    # 不在白名单中的路径
    allowed, reason = handler._check_path_whitelist("config/agents/xiaoke_personality.md", cfg)
    assert allowed is False
    assert "not in whitelist" in reason
    # 其他目录
    allowed2, _ = handler._check_path_whitelist("tools/code_tools.py", cfg)
    assert allowed2 is False


# ============================================================
# 7. 错误码 E_TOOL006 正确定义
# ============================================================
def test_error_code_correct():
    """错误码 E_TOOL006 (Path forbidden by sub-agent whitelist) 正确定义"""
    ec = ErrorCodeEnum.E_TOOL006
    assert ec.code == "E_TOOL006"
    assert ec.http_status == 403
    assert "forbidden" in ec.message.lower() or "whitelist" in ec.message.lower()
    assert ec.retryable is False
    # to_dict 序列化正确
    d = ec.to_dict()
    assert d["error_code"] == "E_TOOL006"
    assert d["http_status"] == 403
    assert d["retryable"] is False


# ============================================================
# 8. 黑名单优先于白名单
# ============================================================
def test_blacklist_takes_precedence():
    """路径同时匹配白名单和黑名单时，黑名单优先（拒绝）"""
    cfg = _make_config(
        allowed_paths=["config/agents/*_personality.md"],
        forbidden_paths=["config/agents/*_personality.md"],
    )
    handler = _make_handler(agent_config=cfg)
    allowed, reason = handler._check_path_whitelist("config/agents/xiaoli_personality.md", cfg)
    assert allowed is False
    assert "forbidden" in reason


# ============================================================
# 9. 路径参数提取
# ============================================================
def test_extract_path_from_write_file_args():
    """write_file 工具参数 input_str="path|||content" 正确提取路径"""
    path = _extract_path_from_args("write_file", {"input_str": "assets/test.txt|||hello"})
    assert path == "assets/test.txt"
    # 无分隔符时返回整个 input_str
    path2 = _extract_path_from_args("write_file", {"input_str": "assets/test.txt"})
    assert path2 == "assets/test.txt"
    # 其他工具按 path/file_path 参数提取
    path3 = _extract_path_from_args("delete_file", {"path": "config/test.json"})
    assert path3 == "config/test.json"
    path4 = _extract_path_from_args("modify_config", {"file_path": "config/app.yaml"})
    assert path4 == "config/app.yaml"
    # 无路径参数时返回空字符串
    path5 = _extract_path_from_args("write_file", {})
    assert path5 == ""


# ============================================================
# 10. 配置文件加载后路径白名单字段正确
# ============================================================
def test_config_files_have_path_whitelist():
    """四个内置 Agent 配置文件包含 allowed_paths / forbidden_paths 字段"""
    import json
    config_dir = Path(__file__).parent.parent / "config" / "agents"
    expected = {
        "xiaoli.json": {
            "allowed": ["assets/stickers/xiaoli/*", "config/agents/xiaoli_personality.md"],
            "forbidden_count_min": 3,
        },
        "xiaoke.json": {
            "allowed": ["tools/code_tools_v2.py", "tools/file_tools_v2.py"],
            "forbidden_count_min": 3,
        },
        "xiaolian.json": {
            "allowed": ["tools/web_browse_tools.py", "tools/web_browse_enhanced.py"],
            "forbidden_count_min": 2,
        },
        "xiaolang.json": {
            "allowed": ["tools/hardware_tools.py", "tools/system_tools.py"],
            "forbidden_count_min": 2,
        },
    }
    for fname, spec in expected.items():
        fp = config_dir / fname
        assert fp.exists(), f"配置文件不存在: {fname}"
        data = json.loads(fp.read_text(encoding="utf-8"))
        assert "allowed_paths" in data, f"{fname} 缺少 allowed_paths"
        assert "forbidden_paths" in data, f"{fname} 缺少 forbidden_paths"
        assert data["allowed_paths"] == spec["allowed"], f"{fname} allowed_paths 不匹配"
        assert len(data["forbidden_paths"]) >= spec["forbidden_count_min"], \
            f"{fname} forbidden_paths 数量不足"


# ============================================================
# 11. SubAgentConfig 从 dict 加载路径白名单
# ============================================================
def test_sub_agent_config_loads_from_dict():
    """SubAgentConfig 能从 JSON dict 正确加载路径白名单字段"""
    cfg = SubAgentConfig(
        name="xiaoli",
        display_name="小莉",
        provider="agnes",
        model="agnes-2.0-flash",
        allowed_paths=["assets/stickers/xiaoli/*"],
        forbidden_paths=["*.env"],
    )
    assert cfg.allowed_paths == ["assets/stickers/xiaoli/*"]
    assert cfg.forbidden_paths == ["*.env"]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
