"""斜杠命令别名映射、非法参数边界与动态模型切换测试。

覆盖：
1. 别名映射（COMMAND_ALIASES / resolve_command / is_owner_command / handle 分发）
2. /model 非法参数边界（空参数、provider/ 或 /model、未知预设、切换失败）
3. /model 对齐 WebUI 模型选择 button 的动态 provider 列表（已移除 MiMo 预设）
4. CLI 两级补全（命令名 + 参数，/model 走动态模型发现）
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slash_commands import (
    COMMAND_ALIASES,
    COMMAND_META,
    OWNER_ONLY_COMMANDS,
    SlashCommandHandler,
    get_argument_completions,
    resolve_command,
)


# ── 别名映射 ──────────────────────────────────────────────

def test_aliases_defined():
    """别名表应包含 /m /v /d /s /h 等快捷别名。"""
    assert COMMAND_ALIASES["/m"] == "/model"
    assert COMMAND_ALIASES["/v"] == "/voice"
    assert COMMAND_ALIASES["/d"] == "/doctor"
    assert COMMAND_ALIASES["/s"] == "/status"
    assert COMMAND_ALIASES["/h"] == "/help"


def test_resolve_command_maps_aliases():
    """resolve_command 应把别名归一化为规范命令。"""
    assert resolve_command("/m") == "/model"
    assert resolve_command("/v") == "/voice"
    assert resolve_command("/d") == "/doctor"
    assert resolve_command("/s") == "/status"
    assert resolve_command("/h") == "/help"


def test_resolve_command_unchanged_for_unknown():
    """未知/普通命令应原样返回。"""
    assert resolve_command("/status") == "/status"
    assert resolve_command("/help") == "/help"
    assert resolve_command("/nope") == "/nope"
    assert resolve_command("hello") == "hello"


def test_resolve_command_case_and_args():
    """resolve_command 应忽略大小写并丢弃参数。"""
    assert resolve_command("/M foo bar") == "/model"
    assert resolve_command("/MODEL") == "/model"


def test_is_owner_command_with_alias():
    """别名解析后应正确判定 owner 权限（/m → /model 是主人专属）。"""
    handler = SlashCommandHandler()
    assert handler.is_owner_command("/m") is True
    assert handler.is_owner_command("/model") is True
    assert handler.is_owner_command("/help") is False


def test_owner_alias_rejected_for_non_owner():
    """非主人通过 /m 别名调用 /model 应被拒绝（别名不绕过 owner 校验）。"""
    handler = SlashCommandHandler()
    handler._security = MagicMock()
    handler._security.is_owner.return_value = False
    result = asyncio.run(handler.handle("/m", "guest_user"))
    assert "只有主人才能用" in result


def test_handle_dispatches_alias_to_model():
    """handle() 应把 /m 别名分发到 /model 处理器。"""
    handler = SlashCommandHandler()
    handler._force_owner = True
    # mock router：/model agnes/xx 走切换分支
    router = MagicMock()
    router.list_models.return_value = {
        "current": "agnes/agnes-2.0-flash",
        "current_label": "Agnes Flash 2.0",
        "providers": [],
    }
    router.set_chat_model.return_value = {"provider": "agnes", "model_id": "agnes-2.0-flash"}
    router.set_model_preference.return_value = True
    handler._router = router
    result = asyncio.run(handler.handle("/m agnes/agnes-2.0-flash", "owner"))
    assert "已切换到" in result
    router.set_chat_model.assert_called_once_with("agnes", "agnes-2.0-flash")


# ── /model 非法参数边界 ───────────────────────────────────

def _mock_router_with_providers():
    """构造带动态 provider 列表的 mock router。"""
    router = MagicMock()
    router.list_models.return_value = {
        "current": "agnes/agnes-2.0-flash",
        "current_label": "Agnes Flash 2.0",
        "providers": [
            {"provider": "agnes", "label": "Agnes AI",
             "models": [{"id": "agnes-2.0-flash", "display_name": "Agnes Flash 2.0", "free": False}]},
            {"provider": "mimo", "label": "MiMo",
             "models": [{"id": "mimo-v2.5", "display_name": "MiMo V2.5", "free": True}]},
        ],
    }
    router.set_chat_model.return_value = {"provider": "agnes", "model_id": "agnes-2.0-flash"}
    router.set_model_preference.return_value = True
    return router


def _handler_with_router(router):
    handler = SlashCommandHandler(router=router)
    handler._force_owner = True
    return handler


@pytest.mark.asyncio
async def test_cmd_model_no_router():
    """router 未初始化时返回友好提示。"""
    handler = SlashCommandHandler(router=None)
    result = await handler._cmd_model("", "owner")
    assert "路由器还没准备好" in result


@pytest.mark.asyncio
async def test_cmd_model_no_args_lists_providers():
    """/model 无参数应显示当前模型 + 动态 provider 列表（对齐 button）。"""
    router = _mock_router_with_providers()
    handler = _handler_with_router(router)
    result = await handler._cmd_model("", "owner")
    assert "当前: Agnes Flash 2.0" in result
    assert "agnes" in result
    assert "Agnes Flash 2.0" in result
    assert "mimo-v2.5" in result
    # 未触发切换
    router.set_chat_model.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_model_switch_valid():
    """/model provider/model 应调用 set_chat_model 切换并更新偏好。"""
    router = _mock_router_with_providers()
    handler = _handler_with_router(router)
    result = await handler._cmd_model("agnes/agnes-2.0-flash", "owner")
    assert "已切换到 agnes-2.0-flash（agnes）" in result
    router.set_chat_model.assert_called_once_with("agnes", "agnes-2.0-flash")
    router.set_model_preference.assert_called_once_with("agnes/agnes-2.0-flash")


@pytest.mark.asyncio
async def test_cmd_model_empty_provider():
    """/model /xxx（provider 为空）应返回用法提示。"""
    router = _mock_router_with_providers()
    handler = _handler_with_router(router)
    result = await handler._cmd_model("/some-model", "owner")
    assert "用法: /model" in result
    router.set_chat_model.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_model_empty_model():
    """/model xxx/（model 为空）应返回用法提示。"""
    router = _mock_router_with_providers()
    handler = _handler_with_router(router)
    result = await handler._cmd_model("agnes/", "owner")
    assert "用法: /model" in result
    router.set_chat_model.assert_not_called()


@pytest.mark.asyncio
async def test_cmd_model_switch_failure():
    """set_chat_model 抛异常时返回失败信息。"""
    router = _mock_router_with_providers()
    router.set_chat_model.side_effect = RuntimeError("boom")
    handler = _handler_with_router(router)
    result = await handler._cmd_model("agnes/agnes-2.0-flash", "owner")
    assert "切换 agnes-2.0-flash 失败" in result
    assert "boom" in result


@pytest.mark.asyncio
async def test_cmd_model_dead_mimo_preset_not_switched():
    """/model mimo-pro（已废弃的 MiMo 预设）不应触发切换，而应显示模型列表。"""
    router = _mock_router_with_providers()
    handler = _handler_with_router(router)
    result = await handler._cmd_model("mimo-pro", "owner")
    # 无 "/" → 落入列表展示分支，不调用 set_chat_model
    router.set_chat_model.assert_not_called()
    assert "当前:" in result
    assert "可用模型" in result


@pytest.mark.asyncio
async def test_cmd_model_no_providers_loaded():
    """模型发现缓存为空时给出引导提示。"""
    router = MagicMock()
    router.list_models.return_value = {
        "current": "agnes/agnes-2.0-flash",
        "current_label": "Agnes Flash 2.0",
        "providers": [],
    }
    handler = _handler_with_router(router)
    result = await handler._cmd_model("", "owner")
    assert "当前:" in result
    assert "尚未加载" in result


@pytest.mark.asyncio
async def test_cmd_help_model_usage_aligns_button():
    """/help 中 /model 用法应对齐 WebUI button（provider/模型），不再出现已废弃的 MiMo 预设。"""
    handler = SlashCommandHandler()
    handler._force_owner = True
    result = await handler._cmd_help("", "owner")
    assert "/model [provider/模型]" in result
    for dead in ("mimo-pro", "mimo-flash", "mimo-mini", "[mimo|"):
        assert dead not in result


# ── 参数补全边界 ──────────────────────────────────────────

def test_get_argument_completions_unknown_command():
    """未知命令应返回空列表（不抛异常）。"""
    assert get_argument_completions("/nope", "x") == []


def test_get_argument_completions_partial_filter():
    """参数补全应按 partial 过滤。"""
    assert get_argument_completions("/voice", "o") == ["on", "off"]
    assert get_argument_completions("/voice", "on") == ["on"]
    assert get_argument_completions("/voice", "zz") == []


def test_get_argument_completions_model_is_dynamic():
    """/model 参数补全不再返回硬编码的 MiMo 预设（已废弃）。"""
    assert get_argument_completions("/model", "mimo") == []
    assert "mimo-pro" not in get_argument_completions("/model", "")


# ── 动态模型发现函数 ──────────────────────────────────────

def test_list_discovered_model_ids():
    """list_discovered_model_ids 应从发现缓存枚举 provider/模型。"""
    from model_router import list_discovered_model_ids
    fake_cache = {"data": [
        {"provider": "agnes", "models": [{"id": "agnes-2.0-flash"}]},
        {"provider": "mimo", "models": [{"id": "mimo-v2.5"}, {"id": "mimo-v2.5-pro"}]},
    ]}
    with patch("web.routers.model_discovery._cache", fake_cache):
        ids = list_discovered_model_ids()
    assert ids == ["agnes/agnes-2.0-flash", "mimo/mimo-v2.5", "mimo/mimo-v2.5-pro"]


def test_list_discovered_model_ids_empty_cache():
    """发现缓存为空时返回空列表。"""
    from model_router import list_discovered_model_ids
    with patch("web.routers.model_discovery._cache", {"data": None}):
        assert list_discovered_model_ids() == []


# ── CLI 两级补全 ──────────────────────────────────────────

def _complete(line: str, text: str) -> list[str]:
    """用假 readline buffer 调用 cli._cli_completer 收集所有候选。"""
    import readline as _rl
    import cli
    orig = _rl.get_line_buffer
    _rl.get_line_buffer = lambda: line
    try:
        out = []
        i = 0
        while True:
            r = cli._cli_completer(text, i)
            if r is None:
                break
            out.append(r)
            i += 1
        return out
    finally:
        _rl.get_line_buffer = orig


def test_completer_level1_command_name():
    """第一级：命令名补全。"""
    matches = _complete("/mo", "/mo")
    assert "/model" in matches


def test_completer_level2_argument():
    """第二级：参数补全（/voice o → on/off）。"""
    matches = _complete("/voice o", "o")
    assert matches == ["on", "off"]


def test_completer_level2_model_dynamic():
    """/model 参数补全应走动态模型发现。"""
    from model_router import list_discovered_model_ids
    fake_cache = {"data": [
        {"provider": "agnes", "models": [{"id": "agnes-2.0-flash"}]},
        {"provider": "mimo", "models": [{"id": "mimo-v2.5"}]},
    ]}
    with patch("web.routers.model_discovery._cache", fake_cache):
        matches = _complete("/model agn", "agn")
    assert matches == ["agnes/agnes-2.0-flash"]


def test_completer_ignores_plain_text():
    """非斜杠输入不触发补全。"""
    assert _complete("hello ", "hello") == []