import re
from pathlib import Path

DEFAULT_EMOJI = {
    "nahida": {"name": "纳西妲", "thinking": "🌿", "using": "🌿", "done": "🌿"},
    "keli": {"name": "可莉", "thinking": "🔥", "using": "🔥", "done": "💣"},
    "yinlang": {"name": "银狼", "thinking": "🎮", "using": "🎮", "done": "🐺"},
    "xilian": {"name": "昔涟", "thinking": "📚", "using": "✨", "done": "🌸"},
    "nike": {"name": "尼可", "thinking": "🧪", "using": "🔬", "done": "🔥"},
}

def load_agent_emoji(agent_name: str, personality_file: str | None = None) -> dict:
    config = dict(DEFAULT_EMOJI.get(agent_name, DEFAULT_EMOJI["nahida"]))
    if personality_file and Path(personality_file).exists():
        try:
            text = Path(personality_file).read_text(encoding="utf-8")
            for key in ["thinking", "using", "done"]:
                m = re.search(rf'^\s*-\s*{key}:\s*(\S+)', text, re.MULTILINE)
                if m:
                    config[key] = m.group(1)
        except Exception:
            pass
    return config

def get_status_msg(agent_name: str, action: str, display_name: str, personality_file: str | None = None) -> str:
    emoji_cfg = load_agent_emoji(agent_name, personality_file)
    agent_display = emoji_cfg.get("name", agent_name)
    e = emoji_cfg.get(action, "🌿")
    if action == "thinking":
        return f"{agent_display}{e}"
    elif action == "using":
        return f"{agent_display}正在使用{display_name}～{e}"
    elif action == "done":
        return f"{display_name}完成啦～{e}"
    return f"{display_name}{e}"
