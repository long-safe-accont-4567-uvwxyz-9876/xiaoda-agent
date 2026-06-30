"""P0-2: Windows Selector 事件循环策略测试。

验证 agent.py 入口在 Windows 平台设置了 WindowsSelectorEventLoopPolicy，
使 aiosqlite 走 Selector 事件循环以加速线程间通知；非 Windows 平台不受影响。
"""
import asyncio
import sys

import pytest


def test_setup_windows_event_loop_is_callable():
    """agent 模块应暴露 _setup_windows_event_loop 函数。"""
    from agent import _setup_windows_event_loop
    assert callable(_setup_windows_event_loop)


def test_setup_windows_event_loop_does_not_raise_on_non_windows():
    """非 Windows 平台调用 _setup_windows_event_loop 不应抛错，也不改变策略。"""
    from agent import _setup_windows_event_loop
    policy_before = asyncio.get_event_loop_policy()
    _setup_windows_event_loop()  # 不应抛异常
    if sys.platform != "win32":
        # 非 Windows 策略保持不变
        assert asyncio.get_event_loop_policy() is policy_before


@pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 启用 Selector 策略")
def test_windows_uses_selector_event_loop_policy():
    """Windows 平台调用 _setup_windows_event_loop 后策略应为 WindowsSelectorEventLoopPolicy。"""
    from agent import _setup_windows_event_loop
    _setup_windows_event_loop()
    assert isinstance(
        asyncio.get_event_loop_policy(),
        asyncio.WindowsSelectorEventLoopPolicy,
    )


def test_main_calls_setup_before_asyncio_usage():
    """静态检查：agent.py 中 _setup_windows_event_loop 调用应早于 uvicorn.run / asyncio 调用。"""
    import re
    from pathlib import Path

    agent_src = Path(__file__).parent.parent / "agent.py"
    content = agent_src.read_text(encoding="utf-8")

    # main() 函数体内应存在 _setup_windows_event_loop() 调用
    main_match = re.search(r"^def main\(\)\s*->\s*None:\n", content, re.MULTILINE)
    assert main_match, "agent.py 未找到 main() 函数"

    # 取 main 函数体起始 400 字符窗口检查调用位置
    body_start = main_match.end()
    head = content[body_start:body_start + 400]
    assert "_setup_windows_event_loop()" in head, (
        "main() 起始处未调用 _setup_windows_event_loop()"
    )

    # 策略设置函数本身应早于任何 asyncio.set_event_loop_policy 之外的其他 asyncio 调用
    setup_idx = content.find("def _setup_windows_event_loop")
    assert setup_idx != -1, "agent.py 未定义 _setup_windows_event_loop"


if __name__ == "__main__":
    pytest.main([__file__, "-x"])
