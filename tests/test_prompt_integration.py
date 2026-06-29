"""测试 prompt 集成：新能力段落注入

覆盖:
- test_build_system_prompt_with_user_id: 带 user_id 时正确注入新段落 + 顺序正确
- test_build_system_prompt_without_user_id: 无 user_id 时不注入新段落
- test_mental_state_injected: L/M/S 段落正确注入
- test_permanent_memory_injected: 永久记忆段落正确注入
- test_emotional_memory_injected: 情感记忆段落正确注入
- test_xp_segment_injected: XP 段落正确注入
- test_exception_does_not_break_prompt: 异常时不破坏主 prompt
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _isolate_prompt_env(monkeypatch):
    """每个测试自动应用：隔离 prompt 构建环境（清空缓存 + mock 基础段）"""
    import prompt_builder

    # 清空缓存
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE", "")
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_TS", 0.0)
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_MTIMES", {})
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_ADDR_TERM", "")

    # Mock workspace sections 和 hardware context 以返回可控内容
    monkeypatch.setattr(prompt_builder, "_build_workspace_sections", lambda addr: ["BASE_PROMPT"])
    monkeypatch.setattr(prompt_builder, "_build_hardware_context", lambda d: "HW_CONTEXT")

    # 确保走非缓存路径
    monkeypatch.setattr("config.PROMPT_CACHING_ENABLED", False, raising=False)

    # 确保 XP 系统启用
    monkeypatch.setenv("XP_SYSTEM_ENABLED", "true")


@pytest.fixture
def xp_system(tmp_path, monkeypatch):
    """真实 XP 系统（使用 tmp_path 隔离数据）"""
    import core.xp_system as mod
    mod._reset_persona_cache()
    sys_obj = mod.XPSystem(data_dir=tmp_path)
    sys_obj.get_state("u1")  # 初始化用户 (LV1, 0 XP)
    monkeypatch.setattr("core.xp_system.get_xp_system", lambda: sys_obj)
    return sys_obj


def _patch_managers(monkeypatch, mental="", permanent="", emotional="",
                     mental_error=None, emotional_error=None):
    """辅助：patch 三个管理器单例"""
    mock_mental = MagicMock()
    if mental_error:
        mock_mental.get_prompt_segment.side_effect = mental_error
    else:
        mock_mental.get_prompt_segment.return_value = mental

    mock_permanent = MagicMock()
    mock_permanent.get_prompt_segment.return_value = permanent

    mock_emotional = MagicMock()
    if emotional_error:
        mock_emotional.recall_and_enact.side_effect = emotional_error
    else:
        mock_emotional.recall_and_enact.return_value = emotional

    monkeypatch.setattr("core.mental_state.get_mental_state_manager", lambda: mock_mental)
    monkeypatch.setattr("core.permanent_memory.get_permanent_memory_manager", lambda: mock_permanent)
    monkeypatch.setattr("memory.emotional_memory.get_emotional_memory_manager", lambda: mock_emotional)
    return mock_mental, mock_permanent, mock_emotional


# ============================================================
# test_build_system_prompt_with_user_id
# ============================================================

def test_build_system_prompt_with_user_id(xp_system, monkeypatch):
    """带 user_id 时正确注入新段落，且顺序正确"""
    from prompt_builder import build_system_prompt

    _patch_managers(
        monkeypatch,
        mental="[当前心理状态]\n长期身份：温柔",
        permanent="[永久记忆]\n用户偏好：称呼=爸爸",
        emotional="[情感记忆召回]\n记得刚才",
    )

    result = build_system_prompt(
        extra_context="EXTRA_CTX",
        user_id="u1", user_input="你好", address_term="爸爸"
    )

    # 所有段落都应出现
    assert "BASE_PROMPT" in result
    assert "[当前心理状态]" in result
    assert "[永久记忆]" in result
    assert "[情感记忆召回]" in result
    assert "[关系亲密度配置]" in result  # XP segment
    assert "EXTRA_CTX" in result

    # 验证顺序: base < mental < permanent < emotional < xp < extra_context
    pos = {
        "base": result.find("BASE_PROMPT"),
        "mental": result.find("[当前心理状态]"),
        "permanent": result.find("[永久记忆]"),
        "emotional": result.find("[情感记忆召回]"),
        "xp": result.find("[关系亲密度配置]"),
        "extra": result.find("EXTRA_CTX"),
    }
    assert pos["base"] < pos["mental"]
    assert pos["mental"] < pos["permanent"]
    assert pos["permanent"] < pos["emotional"]
    assert pos["emotional"] < pos["xp"]
    assert pos["xp"] < pos["extra"]


# ============================================================
# test_build_system_prompt_without_user_id
# ============================================================

def test_build_system_prompt_without_user_id():
    """无 user_id 时不注入新段落（向后兼容）"""
    from prompt_builder import build_system_prompt

    result = build_system_prompt(address_term="爸爸")

    assert "BASE_PROMPT" in result
    # 不应出现任何新段落
    assert "[当前心理状态]" not in result
    assert "[永久记忆]" not in result
    assert "[情感记忆召回]" not in result
    assert "[关系亲密度配置]" not in result


# ============================================================
# test_mental_state_injected
# ============================================================

def test_mental_state_injected(xp_system, monkeypatch):
    """L/M/S 心理状态段落正确注入"""
    from prompt_builder import build_system_prompt

    _patch_managers(
        monkeypatch,
        mental="[当前心理状态]\n长期身份：温柔、聪慧\n当前情绪：用户感到焦虑",
    )

    result = build_system_prompt(user_id="u1", user_input="你好")

    assert "[当前心理状态]" in result
    assert "温柔、聪慧" in result
    assert "焦虑" in result


# ============================================================
# test_permanent_memory_injected
# ============================================================

def test_permanent_memory_injected(xp_system, monkeypatch):
    """永久记忆段落正确注入"""
    from prompt_builder import build_system_prompt

    _patch_managers(
        monkeypatch,
        permanent="[永久记忆]\n用户偏好：\n  - preferred_name: 小纳",
    )

    result = build_system_prompt(user_id="u1", user_input="你好")

    assert "[永久记忆]" in result
    assert "preferred_name" in result
    assert "小纳" in result


# ============================================================
# test_emotional_memory_injected
# ============================================================

def test_emotional_memory_injected(xp_system, monkeypatch):
    """情感记忆段落正确注入"""
    from prompt_builder import build_system_prompt

    _, _, mock_emotional = _patch_managers(
        monkeypatch,
        emotional="[情感记忆召回]\n记得刚才，用户提到工作压力，你当时焦虑。",
    )

    result = build_system_prompt(user_id="u1", user_input="工作压力好大")

    assert "[情感记忆召回]" in result
    assert "工作压力" in result

    # 验证 recall_and_enact 被正确调用（xp_level=1 for LV1）
    mock_emotional.recall_and_enact.assert_called_once_with(
        "u1", "工作压力好大", 1
    )


# ============================================================
# test_xp_segment_injected
# ============================================================

def test_xp_segment_injected(xp_system, monkeypatch):
    """XP 等级段落正确注入"""
    from prompt_builder import build_system_prompt

    # 其他段落返回空，只测 XP
    _patch_managers(monkeypatch)

    result = build_system_prompt(user_id="u1", user_input="你好")

    assert "[关系亲密度配置]" in result
    assert "LV1" in result
    assert "陌生人" in result


# ============================================================
# test_exception_does_not_break_prompt
# ============================================================

def test_exception_does_not_break_prompt(xp_system, monkeypatch):
    """异常时不破坏主 prompt（零质量回退）"""
    from prompt_builder import build_system_prompt

    _patch_managers(
        monkeypatch,
        permanent="[永久记忆]\nOK",
        mental_error=RuntimeError("boom"),
        emotional_error=RuntimeError("boom"),
    )

    result = build_system_prompt(user_id="u1", user_input="你好")

    # 主 prompt 不被破坏
    assert "BASE_PROMPT" in result
    # permanent_memory 正常注入
    assert "[永久记忆]" in result
    assert "OK" in result
    # 异常的段落不出现
    assert "[当前心理状态]" not in result
    assert "[情感记忆召回]" not in result
    # XP 段落仍然出现（不受其他异常影响）
    assert "[关系亲密度配置]" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
