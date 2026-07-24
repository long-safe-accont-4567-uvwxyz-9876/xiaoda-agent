"""测试时间上下文使用正确的时区 — 修复Windows本地部署时间乱报问题.

根因: agent_context._build_time_context() 使用 datetime.now() (无时区),
在Windows/Docker中系统时区非中国时区时, agent会报出错误的时间.
修复: 使用 ZoneInfo("Asia/Shanghai") 显式指定中国时区.
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo


def test_time_context_uses_shanghai_timezone():
    """_build_time_context 应使用 Asia/Shanghai 时区, 不依赖系统本地时区."""
    from agent_context import AgentContext

    ctx = AgentContext.__new__(AgentContext)
    result = ctx._build_time_context()

    shanghai_now = datetime.now(ZoneInfo("Asia/Shanghai"))

    assert str(shanghai_now.year) in result, \
        f"时间上下文应包含上海时间年份 {shanghai_now.year}, 实际: {result}"
    assert f"{shanghai_now.month}月" in result, \
        f"时间上下文应包含上海时间月份, 实际: {result}"
    assert f"{shanghai_now.day}日" in result, \
        f"时间上下文应包含上海时间日期, 实际: {result}"
    assert f"{shanghai_now.hour:02d}:" in result, \
        f"时间上下文应包含上海时间小时 {shanghai_now.hour:02d}, 实际: {result}"


def test_time_context_respects_env_timezone():
    """时间上下文应支持 NUDGE_TIMEZONE 环境变量覆盖."""
    from agent_context import AgentContext

    old_tz = os.environ.get("NUDGE_TIMEZONE")
    try:
        os.environ["NUDGE_TIMEZONE"] = "Asia/Tokyo"
        ctx = AgentContext.__new__(AgentContext)
        result = ctx._build_time_context()

        tokyo_now = datetime.now(ZoneInfo("Asia/Tokyo"))
        assert f"{tokyo_now.hour:02d}:" in result, \
            f"时间上下文应支持环境变量覆盖为东京时间, 实际: {result}"
    finally:
        if old_tz is not None:
            os.environ["NUDGE_TIMEZONE"] = old_tz
        else:
            os.environ.pop("NUDGE_TIMEZONE", None)


def test_greeting_scheduler_uses_explicit_timezone():
    """greeting_scheduler 应使用显式时区, 不依赖系统本地时区."""
    import inspect

    import web.greeting_scheduler as gs_mod
    source = inspect.getsource(gs_mod)
    assert "Asia/Shanghai" in source or "ZoneInfo" in source or "NUDGE_TIMEZONE" in source, \
        "greeting_scheduler 模块应使用显式时区"
    # 确保不再有裸 datetime.now() 调用（无时区参数）
    import re
    # 排除 _get_local_now 内部的 datetime.now(tz) 和注释
    bare_now = re.findall(r'datetime\.now\(\)', source)
    assert not bare_now, \
        f"greeting_scheduler 不应使用裸 datetime.now(), 发现 {len(bare_now)} 处"
