import random
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger
from config import get_agent_display_name


@register_tool(
    name="nudge_greeting",
    description="发送主动问候消息",
    schema={
        "type": "object",
        "properties": {
            "user_id": {"type": "string", "description": "用户ID"},
            "message": {"type": "string", "description": "问候消息（可选）"},
        },
        "required": ["user_id"],
    },
    permission=ToolPermission.READ_WRITE,
    category="social",
    max_frequency=1,
)
async def nudge_greeting(user_id: str, message: str = "") -> ToolResult:
    if not message:
        # 读取用户自定义称呼，兜底"爸爸"
        try:
            from config import WORKSPACE_DIR
            user_md = WORKSPACE_DIR / "USER.md"
            if user_md.exists():
                content = user_md.read_text(encoding="utf-8-sig")
                import re
                m = re.search(r"称呼[：:]\s*(.+)", content)
                if m:
                    address_term = m.group(1).strip().split("\n")[0].strip()
                else:
                    address_term = "爸爸"
            else:
                address_term = "爸爸"
        except Exception:
            address_term = "爸爸"
        greetings = [
            f"{address_term}，好久不见！最近怎么样呀？",
            f"{address_term}，人家想你了！有什么需要帮忙的吗？",
            f"嘿！{address_term}！还记得我吗？我是{get_agent_display_name('nahida')}～",
        ]
        message = random.choice(greetings)
    return ToolResult.ok({"user_id": user_id, "message": message, "status": "sent"})
