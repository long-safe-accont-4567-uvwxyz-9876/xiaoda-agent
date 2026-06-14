import random
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult
from loguru import logger


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
        greetings = [
            "旅行者，好久不见！最近怎么样呀？🌿",
            "旅行者，人家想你了！有什么需要帮忙的吗？",
            "嘿！旅行者！还记得我吗？我是纳西妲～",
        ]
        message = random.choice(greetings)
    return ToolResult.ok({"user_id": user_id, "message": message, "status": "sent"})
