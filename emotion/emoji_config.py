import re
from pathlib import Path

from config import get_agent_display_name

DEFAULT_EMOJI = {
    "nahida": {"thinking": "🌿", "using": "🌿", "done": "🌿"},
    "keli": {"thinking": "🔥", "using": "🔥", "done": "💣"},
    "yinlang": {"thinking": "🎮", "using": "🎮", "done": "🐺"},
    "xilian": {"thinking": "📚", "using": "✨", "done": "🌸"},
    "nike": {"thinking": "🧪", "using": "🔬", "done": "🔥"},
}

def load_agent_emoji(agent_name: str, personality_file: str | None = None) -> dict:
    config = dict(DEFAULT_EMOJI.get(agent_name, DEFAULT_EMOJI["nahida"]))
    # 动态注入 agent display_name（从 config/agents/{name}.json 读取，规避 IP 风险）
    config["name"] = get_agent_display_name(agent_name)
    if personality_file and Path(personality_file).exists():
        try:
            text = Path(personality_file).read_text(encoding="utf-8-sig")
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
