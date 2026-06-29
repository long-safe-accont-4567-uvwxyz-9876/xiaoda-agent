"""测试 SOLO 模式任务→代理 1:1 绑定路由（参考 Trae SOLO 模式）。

覆盖：route_task 路由映射 / classify_task 关键词分类 / 不可用回退 / 配置加载 /
未知输入回退到 general。使用 pytest + unittest.mock.MagicMock 隔离子代理依赖。
"""
from unittest.mock import MagicMock

import pytest

from agent_dispatcher import AgentDispatcher


def _make_dispatcher(agents: dict[str, bool] | None = None) -> AgentDispatcher:
    """构造带 mock 子代理的 AgentDispatcher。

    :param agents: {agent_name: available} 字典；为 None 时不注册任何代理
    """
    dispatcher = AgentDispatcher(tts=MagicMock())
    if agents:
        for name, available in agents.items():
            mock_agent = MagicMock()
            mock_agent.available = available
            dispatcher._agents[name] = mock_agent
    return dispatcher


# 所有子代理可用的默认注册表
_ALL_AVAILABLE = {
    "keli": True,
    "nike": True,
    "xilian": True,
    "yinlang": True,
}


def test_route_frontend_to_nike():
    """前端任务应路由到 nike（编程助手）。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)
    target = dispatcher.route_task("frontend", "帮我写个 Vue 前端页面")
    assert target == "nike"


def test_route_emotional_to_keli():
    """情感陪伴任务应路由到 keli（萌系陪伴）。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)
    target = dispatcher.route_task("emotional", "今天好难过，求安慰")
    assert target == "keli"


def test_route_hardware_to_yinlang():
    """硬件任务应路由到 yinlang（系统管理）。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)
    target = dispatcher.route_task("hardware", "GPIO 引脚怎么配置")
    assert target == "yinlang"


def test_classify_task_keywords():
    """关键词分类应正确识别各种任务类型。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)

    cases = [
        ("帮我写个前端页面", "frontend"),
        ("后端 API 设计", "backend"),
        ("这个 bug 怎么调试", "debug"),
        ("检查系统安全漏洞", "security"),
        ("运行 pytest 单测", "test"),
        ("搜索一下天气", "info_search"),
        ("GPIO 传感器读取", "hardware"),
        ("今天好难过求陪伴", "emotional"),
    ]

    for user_input, expected in cases:
        result = dispatcher.classify_task(user_input)
        assert result == expected, f"classify_task({user_input!r}) = {result!r}, expected {expected!r}"


def test_route_fallback_when_unavailable():
    """目标代理不可用时应回退到默认代理（keli）。"""
    # nike 不可用，keli 可用
    agents = {"nike": False, "keli": True, "xilian": True, "yinlang": True}
    dispatcher = _make_dispatcher(agents)

    # frontend 本应路由到 nike，但 nike 不可用 → 回退到 keli
    target = dispatcher.route_task("frontend", "写个 React 组件")
    assert target == "keli"


def test_routing_config_load():
    """配置文件应正确加载，包含所有任务类型映射。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)
    config = dispatcher._load_routing_config()

    # 验证全部关键字段
    assert config["frontend"] == "nike"
    assert config["backend"] == "nike"
    assert config["debug"] == "nike"
    assert config["security"] == "yinlang"
    assert config["test"] == "nike"
    assert config["info_search"] == "xilian"
    assert config["hardware"] == "yinlang"
    assert config["emotional"] == "keli"
    assert config["general"] == "keli"


def test_classify_unknown_to_general():
    """未匹配任何关键词的输入应分类为 general。"""
    dispatcher = _make_dispatcher(_ALL_AVAILABLE)

    # 这些输入不含任何关键词
    for user_input in ["今天天气怎么样", "你好", "12345", "随便说点什么"]:
        result = dispatcher.classify_task(user_input)
        assert result == "general", f"classify_task({user_input!r}) = {result!r}, expected 'general'"
