import random
import re
from pathlib import Path

from loguru import logger

from config import get_agent_display_name

DEFAULT_EMOJI = {
    "xiaoda": {"thinking": "🌿", "using": "🌿", "done": "🌿"},
    "xiaoli": {"thinking": "🔥", "using": "🔥", "done": "💣"},
    "xiaolang": {"thinking": "🎮", "using": "🎮", "done": "🐺"},
    "xiaolian": {"thinking": "📚", "using": "✨", "done": "🌸"},
    "xiaoke": {"thinking": "🧪", "using": "🔬", "done": "🔥"},
}

def load_agent_emoji(agent_name: str, personality_file: str | None = None) -> dict:
    """加载 agent 的 emoji 配置，并合并人格文件中的自定义项。"""
    config = dict(DEFAULT_EMOJI.get(agent_name, DEFAULT_EMOJI["xiaoda"]))
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
            logger.debug("emoji_config.load_agent_emoji_failed", exc_info=True)
    return config

def get_status_msg(agent_name: str, action: str, display_name: str, personality_file: str | None = None) -> str:
    """根据 agent 与动作生成带 emoji 的状态提示文案。"""
    emoji_cfg = load_agent_emoji(agent_name, personality_file)
    agent_display = emoji_cfg.get("name", agent_name)
    e = emoji_cfg.get(action, "🌿")
    if action == "thinking":
        return f"{agent_display}{e}"
    if action == "using":
        return f"{agent_display}正在使用{display_name}～{e}"
    if action == "done":
        return f"{display_name}完成啦～{e}"
    return f"{display_name}{e}"


def get_ack_message(agent_name: str, personality_file: str | None = None) -> str:
    """返回 agent 的 ACK 状态消息（thinking 阶段提示）。

    优先从 agent 配置文件的 ack_messages 字段随机选取一条，
    并将旧名/agent key 替换为当前 display_name。
    未配置时返回默认 f"{display_name}收到啦，正在想～🌿"。
    """
    import json
    try:
        from config import AGENTS_CONFIG_DIR as _acfg
        fp = _acfg / f"{agent_name}.json"
        if fp.exists():
            cfg = json.loads(fp.read_text(encoding="utf-8-sig"))
            ack_messages = cfg.get("ack_messages", [])
            if ack_messages:
                display_name = cfg.get("display_name", agent_name)
                msg = random.choice(ack_messages)
                # 旧名 → display_name 替换
                for old in cfg.get("deprecated_names", []):
                    if old and old in msg:
                        msg = msg.replace(old, display_name)
                # agent key → display_name 替换
                if agent_name in msg:
                    msg = msg.replace(agent_name, display_name)
                return msg
    except Exception as e:
        logger.debug("emoji_config.ack_message_build_failed", error=str(e))
    # 默认格式
    display_name = get_agent_display_name(agent_name)
    emoji_cfg = load_agent_emoji(agent_name, personality_file)
    e = emoji_cfg.get("thinking", "🌿")
    return f"{display_name}收到啦，正在想～{e}"
