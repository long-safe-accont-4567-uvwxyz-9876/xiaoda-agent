import os
from loguru import logger


class EmojiConfig:
    STATUS_MESSAGES = {
        "thinking": {
            "nahida": ["🌿 纳西妲正在思考...", "🌿 让我想想...", "🌿 嗯..."],
            "xilian": ["🔍 希兰正在搜索...", "🔍 让我查查..."],
            "yinlang": ["⚔️ 银狼正在分析...", "⚔️ 处理中..."],
            "nike": ["📚 妮可正在研究...", "📚 查阅资料中..."],
            "keli": ["💥 可莉正在准备...", "💥 嘿嘿..."],
        },
        "using": {
            "nahida": ["🔧 纳西妲正在使用"],
            "xilian": ["🔍 希兰正在搜索"],
            "yinlang": ["⚔️ 银狼正在执行"],
            "nike": ["📚 妮可正在查阅"],
        },
        "done": {
            "nahida": ["✅ 完成啦！", "✅ 好了～"],
            "xilian": ["✅ 搜索完成！"],
            "yinlang": ["✅ 执行完成！"],
            "nike": ["✅ 研究完成！"],
        },
        "error": {
            "nahida": ["❌ 出了点问题..."],
        },
    }

    @classmethod
    def get_status_msg(cls, agent: str, status: str, detail: str = "", personality: str = None) -> str:
        agent_msgs = cls.STATUS_MESSAGES.get(status, {}).get(agent, [])
        if not agent_msgs:
            agent_msgs = cls.STATUS_MESSAGES.get(status, {}).get("nahida", [f"{status}..."])
        import random
        msg = random.choice(agent_msgs)
        if detail:
            msg += f" {detail}"
        return msg


def get_status_msg(agent: str, status: str, detail: str = "", personality: str = None) -> str:
    return EmojiConfig.get_status_msg(agent, status, detail, personality)
