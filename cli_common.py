"""CLI 共享助手与主题色板。

供 `cli.py`（prompt_toolkit 降级路径）与 `cli_app.py`（Textual TUI）共用，
避免两套终端界面各自维护重复的取色/翻译/元数据函数。
"""
from __future__ import annotations

import os
import sys

# 纳西妲绿色主题色板（与 WebUI 品牌色一致）
STYLE: dict[str, str] = {
    "bg": "black",
    "panel": "#1e2a1e",
    "border": "#8bc34a",
    "accent": "#ffd54f",
    "gold": "#ffd54f",
    "user": "#a5d6a7",
    "assistant": "#e8f5e9",
    "muted": "#6b8f6b",
    "leaf": "#8bc34a",
    "grass": "#33691e",
}

# WS 状态事件 → 友好中文（丰富版：含工具/子代理识别）
STATUS_MAP = {
    "thinking": "🌿 小妲正在想……",
    "route": "✨ 人家在看看交给谁比较好～",
    "tool": "🌿 小妲正在查资料～",
    "search": "🔍 人家帮你搜一下～",
    "weather": "🌤️ 人家看看天气怎么样～",
    "browse": "🌐 人家去网上看看～",
    "shell": "💻 人家在跑命令～",
    "python": "🐍 人家在算东西～",
    "camera": "📷 人家看看摄像头～",
    "xiaoda_done": "🌿 小妲整理好了！",
    "xiaoli_done": "💥 小莉完成啦！",
    "xiaolian_done": "🌸 小涟完成啦！",
    "xiaolang_done": "🎮 小狼完成啦！",
    "xiaoke_done": "🔮 小可完成啦！",
    "done": "✅ 搞定啦～",
}

# IP-safe: 动态从 config/agents/*.json 读取 display_name，避免硬编码原名
try:
    from config import get_agent_display_name, agent_names
    from emotion.emoji_config import get_ack_message
    AGENT_NAMES = {name: get_agent_display_name(name) for name in agent_names()}
    # ACK 消息使用自定义配置（随心即言）
    STATUS_MAP["thinking"] = get_ack_message("xiaoda")
except ImportError:
    AGENT_NAMES = {"xiaoda": "小妲", "xiaoli": "小莉", "xiaolian": "小涟", "xiaolang": "小狼", "xiaoke": "小可"}


def status_translate(msg: str) -> str:
    """把主进程 WS 状态事件翻译成友好中文。未识别时原样包裹。"""
    low = msg.lower()
    for key, val in STATUS_MAP.items():
        if key in low:
            return val
    for eng, chn in AGENT_NAMES.items():
        if eng in low:
            return f"✨ 人家让{chn}帮忙看看～"
    if "路由" in msg or "route" in low:
        return "✨ 人家在看看交给谁比较好～"
    if "正在使用" in msg or "使用" in msg:
        tool_hints = {
            "搜索": "🔍 人家帮你搜一下～",
            "天气": "🌤️ 人家看看天气～",
            "网页": "🌐 人家去网上看看～",
            "命令": "💻 人家在跑命令～",
            "python": "🐍 人家在算东西～",
            "摄像": "📷 人家看看摄像头～",
        }
        for hint, val in tool_hints.items():
            if hint in msg:
                return val
        return "🌿 小妲正在忙～"
    if "完成" in msg or "done" in low:
        return "✅ 搞定啦～"
    if "正在" in msg:
        return f"🌿 {msg}"
    return f"🌿 {msg}"


def get_model_info(token: str = "") -> str:
    """读取当前聊天模型显示名；无主进程时返回默认模型名。"""
    import cli_client
    if token:
        try:
            return cli_client.get_chat_model_label(token)
        except Exception:
            return "mimo-v2.5"
    return "mimo-v2.5"


def command_entries() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """从 slash_commands 权威数据源取命令表（公共, 主人级）。"""
    from slash_commands import COMMAND_DESCRIPTIONS, OWNER_ONLY_COMMANDS
    public: list[tuple[str, str]] = []
    owner: list[tuple[str, str]] = []
    for name, desc in COMMAND_DESCRIPTIONS.items():
        if name in OWNER_ONLY_COMMANDS:
            owner.append((name, desc))
        else:
            public.append((name, desc))
    return public, owner


def address_term() -> str:
    """读取当前用户称呼（USER.md 动态），未设置兜底"朋友"。"""
    try:
        from agent_core.core import AgentCore
        term = AgentCore.read_address_term_from_user_md()
        if term:
            return term
    except ImportError:
        pass
    return "朋友"


def cli_should_use_tui() -> bool:
    """判定是否启用 Textual TUI：可导入 && 交互式终端 && TERM 非 dumb。"""
    try:
        import textual  # noqa: F401
    except ImportError:
        return False
    if not sys.stdin.isatty():
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True