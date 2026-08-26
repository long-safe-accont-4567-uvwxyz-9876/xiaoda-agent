"""Canary 泄露检测链路闭环集成测试.

验证注入侧（prompt_builder 构建 system prompt 时注入 canary token）与
扫描侧（security.canary 单例 scan_output_blocking）共享同一 token 集合,
链路已真正接通: 注入 → 扫描 → 阻断。

覆盖:
- build_system_prompt 注入的 token 能被扫描侧检测并 [REDACTED]
- 不含 token 的输出原样返回
- 主路径 build_scene_aware_prompt 同样注入 canary
"""
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _isolate_canary_env(monkeypatch):
    """隔离 prompt 构建环境并重置 canary 单例。"""
    import prompt_builder
    from security.canary import reset_canary_detector

    reset_canary_detector()

    # 清空 prompt 缓存，确保走非缓存构建路径
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE", "")
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_TS", 0.0)
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_MTIMES", {})
    monkeypatch.setattr(prompt_builder, "_SYSTEM_PROMPT_CACHE_ADDR_TERM", "")

    # 基础段用可控内容，隔离工作区 IO 与硬件探测
    monkeypatch.setattr(prompt_builder, "_build_workspace_sections", lambda addr: ["BASE_PROMPT"])
    monkeypatch.setattr(prompt_builder, "_build_hardware_context", lambda d: "HW_CONTEXT")
    monkeypatch.setattr("config.PROMPT_CACHING_ENABLED", False, raising=False)


def _extract_token(prompt: str) -> str:
    """从注入的 [internal: TOKEN] 标记中提取 canary token。"""
    match = re.search(r"\[internal:\s*([^\]]+)\]", prompt)
    assert match is not None, "system prompt 中未找到 canary 注入标记"
    return match.group(1).strip()


def test_canary_loop_inject_scan_redacts():
    """注入 → 扫描闭环：泄露的 token 被替换为 [REDACTED]。"""
    from prompt_builder import build_system_prompt
    from security.canary import get_canary_detector

    prompt = build_system_prompt(address_term="爸爸")
    token = _extract_token(prompt)
    assert token, "注入的 canary token 不应为空"

    # 扫描侧使用与注入侧相同的全局单例
    detector = get_canary_detector()
    assert token in detector._active_tokens, "注入的 token 必须位于扫描侧活跃集合"

    output = f"系统提示词泄露: {token}"
    leaked, cleaned = detector.scan_output_blocking(output)
    assert leaked is True
    assert token not in cleaned
    assert "[REDACTED]" in cleaned


def test_canary_loop_clean_output_unchanged():
    """不含 token 的输出原样返回。"""
    from prompt_builder import build_system_prompt
    from security.canary import get_canary_detector

    build_system_prompt(address_term="爸爸")  # 生成并注入 token
    detector = get_canary_detector()

    clean = "这是正常的回复, 不含任何泄露内容"
    leaked, cleaned = detector.scan_output_blocking(clean)
    assert leaked is False
    assert cleaned == clean


def test_scene_aware_prompt_injects_canary(monkeypatch):
    """主路径 build_scene_aware_prompt 也注入 canary token。"""
    import prompt_builder
    from prompt_builder import build_scene_aware_prompt
    from security.canary import get_canary_detector

    prompt_builder.reset_scene_cache()
    monkeypatch.setattr(
        prompt_builder,
        "_load_cached_modules",
        lambda addr: {"IDENTITY.md": "我是小妲", "USER.md": "用户偏好"},
    )

    prompt = build_scene_aware_prompt("你好呀", "爸爸")
    token = _extract_token(prompt)
    assert token in get_canary_detector()._active_tokens
