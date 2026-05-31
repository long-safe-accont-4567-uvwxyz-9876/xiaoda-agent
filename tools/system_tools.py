import os
import asyncio
import subprocess
from tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


@register_tool(
    name="system_info",
    description="获取系统信息（CPU、内存、磁盘）",
    schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="system",
    max_frequency=5,
)
async def system_info() -> ToolResult:
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return ToolResult.ok({
            "cpu_percent": cpu,
            "memory": {
                "total": memory.total,
                "available": memory.available,
                "percent": memory.percent,
            },
            "disk": {
                "total": disk.total,
                "free": disk.free,
                "percent": disk.percent,
            },
        })
    except ImportError:
        return ToolResult.fail("需要安装 psutil：pip install psutil")
    except Exception as e:
        return ToolResult.fail(f"获取系统信息失败：{str(e)}")


@register_tool(
    name="get_current_time",
    description="获取当前时间",
    schema={
        "type": "object",
        "properties": {},
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="utility",
    max_frequency=60,
)
async def get_current_time() -> ToolResult:
    from datetime import datetime
    now = datetime.now()
    return ToolResult.ok({
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
    })


@register_tool(
    name="calculator",
    description="数学计算器",
    schema={
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "数学表达式"},
        },
        "required": ["expression"],
    },
    permission=ToolPermission.READ_ONLY,
    category="utility",
    max_frequency=20,
)
async def calculator(expression: str) -> ToolResult:
    import math
    safe_funcs = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sin": math.sin, "cos": math.cos, "tan": math.tan,
        "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
        "pi": math.pi, "e": math.e, "pow": pow,
    }
    try:
        result = eval(expression, {"__builtins__": {}}, safe_funcs)
        return ToolResult.ok({"expression": expression, "result": result})
    except Exception as e:
        return ToolResult.fail(f"计算失败：{str(e)}")


@register_tool(
    name="get_weather",
    description="获取天气信息",
    schema={
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名称", "default": "北京"},
        },
        "required": [],
    },
    permission=ToolPermission.READ_ONLY,
    category="utility",
    max_frequency=5,
)
async def get_weather(city: str = "北京") -> ToolResult:
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://wttr.in/{city}?format=j1", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                current = data.get("current_condition", [{}])[0]
                return ToolResult.ok({
                    "city": city,
                    "temperature": current.get("temp_C", "未知"),
                    "humidity": current.get("humidity", "未知"),
                    "description": current.get("lang_zh", [{}])[0].get("value", "未知") if current.get("lang_zh") else "未知",
                    "wind_speed": current.get("windspeedKmph", "未知"),
                })
            return ToolResult.fail(f"获取天气失败：HTTP {resp.status_code}")
    except Exception as e:
        return ToolResult.fail(f"获取天气失败：{str(e)}")
