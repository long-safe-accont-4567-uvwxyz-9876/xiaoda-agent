"""微信 Bot 适配器骨架测试

TDD 红阶段：验证 wechat_bot_adapter.WeChatBotAdapter 骨架接口。
所有方法仅验证骨架行为（不抛异常 / 返回 False），不验证 iLink 协议细节。
"""
import asyncio
import os

import pytest


# ---------- 简单 mock 对象 ----------

class MockDB:
    pass


class MockRouter:
    pass


class MockAPI:
    pass


class MockCore:
    pass


class MockConfigService:
    pass


class MockPortraitManager:
    pass


# ---------- fixtures ----------

@pytest.fixture
def mock_deps():
    """返回一组 mock 依赖，用于构造 WeChatBotAdapter"""
    return {
        "db": MockDB(),
        "router": MockRouter(),
        "api": MockAPI(),
        "user_openid": "wx_openid_test_1234567890abcdef",
        "core": MockCore(),
        "config_service": MockConfigService(),
        "portrait_manager": MockPortraitManager(),
    }


@pytest.fixture
def adapter(mock_deps):
    """构造一个 WeChatBotAdapter 实例"""
    from wechat_bot_adapter import WeChatBotAdapter
    return WeChatBotAdapter(**mock_deps)


# ---------- 测试用例 ----------

def test_adapter_instantiable(mock_deps):
    """1. WeChatBotAdapter 可实例化"""
    from wechat_bot_adapter import WeChatBotAdapter
    bot = WeChatBotAdapter(**mock_deps)
    assert bot is not None
    # 验证关键属性被正确赋值
    assert bot._db is mock_deps["db"]
    assert bot._router is mock_deps["router"]
    assert bot._api is mock_deps["api"]
    assert bot._user_openid == mock_deps["user_openid"]
    assert bot._core is mock_deps["core"]
    assert bot._config_service is mock_deps["config_service"]
    assert bot._portrait_manager is mock_deps["portrait_manager"]


def test_start_does_not_raise(adapter):
    """2. start() 不抛异常（仅记录 warning）"""
    asyncio.run(adapter.start())
    assert adapter._running is True


def test_stop_does_not_raise(adapter):
    """3. stop() 不抛异常"""
    asyncio.run(adapter.stop())
    assert adapter._running is False


def test_send_message_returns_false(adapter):
    """4. send_message() 返回 False（骨架实现）"""
    result = asyncio.run(adapter.send_message("hello"))
    assert result is False


def test_send_sticker_returns_false(adapter):
    """5. send_sticker() 返回 False"""
    result = asyncio.run(adapter.send_sticker("/tmp/sticker.png"))
    assert result is False


def test_send_voice_returns_false(adapter):
    """6. send_voice() 返回 False"""
    result = asyncio.run(adapter.send_voice("/tmp/voice.wav"))
    assert result is False


def test_create_wechat_bot_factory(mock_deps):
    """7. create_wechat_bot() 工厂函数返回 WeChatBotAdapter 实例"""
    from wechat_bot_adapter import create_wechat_bot, WeChatBotAdapter
    bot = create_wechat_bot(
        db=mock_deps["db"],
        router=mock_deps["router"],
        api=mock_deps["api"],
        user_openid=mock_deps["user_openid"],
        core=mock_deps["core"],
        config_service=mock_deps["config_service"],
        portrait_manager=mock_deps["portrait_manager"],
    )
    assert isinstance(bot, WeChatBotAdapter)


def test_create_wechat_bot_without_env_enabled(mock_deps, monkeypatch):
    """8. WECHAT_ILINK_ENABLED 环境变量未设置时仍可创建实例"""
    monkeypatch.delenv("WECHAT_ILINK_ENABLED", raising=False)
    from wechat_bot_adapter import create_wechat_bot, WeChatBotAdapter
    bot = create_wechat_bot(
        db=mock_deps["db"],
        router=mock_deps["router"],
    )
    assert isinstance(bot, WeChatBotAdapter)
