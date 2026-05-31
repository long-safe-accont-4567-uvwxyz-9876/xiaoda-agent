import os
import re
import json
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

_KIOXIA_BASE = Path(os.getenv("KIOXIA_DATA_DIR", "/media/orangepi/KIOXIA/nahida-data"))
_FALLBACK_BASE = Path(__file__).resolve().parent

def _resolve_data_path(kioxia_path: Path, fallback_path: Path) -> Path:
    if kioxia_path.exists() or kioxia_path.parent.exists():
        kioxia_path.mkdir(parents=True, exist_ok=True)
        return kioxia_path
    fallback_path.mkdir(parents=True, exist_ok=True)
    return fallback_path

DATA_DIR = _resolve_data_path(_KIOXIA_BASE / "db", _FALLBACK_BASE / "data")
LOG_DIR = _resolve_data_path(_KIOXIA_BASE / "logs", _FALLBACK_BASE / "logs")
WORKSPACE_DIR = _resolve_data_path(_KIOXIA_BASE / "config" / "workspace", Path(os.path.expanduser("~/.ai-agent/workspace")))
CREDENTIALS_DIR = _resolve_data_path(_KIOXIA_BASE / "credentials", Path(os.path.expanduser("~/.ai-agent/credentials")))
AGENT_CONFIG_PATH = (_KIOXIA_BASE / "config" / "agent.json5") if (_KIOXIA_BASE / "config").exists() else Path(os.path.expanduser("~/.ai-agent/agent.json5"))
STICKER_DIR = _KIOXIA_BASE / "stickers"
KLEE_STICKER_DIR = _KIOXIA_BASE / "klee-stickers"
FILE_DIR = _KIOXIA_BASE / "files"

_KIOXIA_AVAILABLE = (_KIOXIA_BASE / "db").exists()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
MODEL_NAME = os.getenv("MODEL_NAME", "mimo-v2.5")

MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")


def _strip_json5_comments(text: str) -> str:
    result = []
    in_string = False
    in_block_comment = False
    i = 0
    while i < len(text):
        if in_block_comment:
            if text[i:i+2] == '*/':
                in_block_comment = False
                i += 2
                continue
            i += 1
            continue
        if in_string:
            result.append(text[i])
            if text[i] == '\\' and i + 1 < len(text):
                result.append(text[i+1])
                i += 2
                continue
            if text[i] == '"':
                in_string = False
            i += 1
            continue
        if text[i:i+2] == '/*':
            in_block_comment = True
            i += 2
            continue
        if text[i:i+2] == '//':
            while i < len(text) and text[i] != '\n':
                i += 1
            continue
        if text[i] == '"':
            in_string = True
            result.append(text[i])
            i += 1
            continue
        result.append(text[i])
        i += 1
    cleaned = ''.join(result)
    cleaned = re.sub(r',\s*([}\]])', r'\1', cleaned)
    return cleaned


def load_agent_config() -> dict:
    if not AGENT_CONFIG_PATH.exists():
        return {}
    raw = AGENT_CONFIG_PATH.read_text(encoding="utf-8")
    cleaned = _strip_json5_comments(raw)
    return json.loads(cleaned)


def load_workspace_file(filename: str) -> str:
    filepath = WORKSPACE_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8").strip()
    return ""


_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 5.0

def build_system_prompt(extra_context: str = "") -> str:
    global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS

    now = time.time()
    if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL:
        system_prompt = _SYSTEM_PROMPT_CACHE
    else:
        from datetime import datetime
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M") + " (北京时间)"

        sections = []

        time_section = f"[当前时间] {current_time}"
        sections.append(time_section)

        agents_rules = load_workspace_file("AGENTS.md")
        if agents_rules:
            sections.append(agents_rules)

        soul = load_workspace_file("SOUL.md")
        if soul:
            sections.append(soul)

        identity = load_workspace_file("IDENTITY.md")
        if identity:
            sections.append(identity)

        user = load_workspace_file("USER.md")
        if user:
            sections.append(user)

        tools_rules = load_workspace_file("TOOLS.md")
        if tools_rules:
            sections.append(tools_rules)

        memory = load_workspace_file("MEMORY.md")
        if memory:
            sections.append(memory)

        heartbeat = load_workspace_file("HEARTBEAT.md")
        if heartbeat:
            sections.append(heartbeat)

        hw_context = (
            "[香橙派硬件信息]\n"
            "板卡: Orange Pi 4 Pro | SoC: 全志 T507 | 架构: ARMv8 (6×A55 + 2×A76 big.LITTLE)\n"
            "系统: Debian 12 (bookworm) arm64\n"
            "可用接口: GPIO (40pin排针) / I2C / SPI / UART / PWM\n"
            "可用工具: gpio_control(引脚控制) / i2c_comm(I2C通信) / hardware_status(硬件监控) / service_manage(服务管理) / network_diag(网络诊断) / dev_assist(开发辅助) / camera_capture(拍照) / vision_analyze(视觉分析)\n"
            "数据存储: KIOXIA 外挂存储 (/media/orangepi/KIOXIA/nahida-data/)\n"
            "摄像头: Q8 HD Webcam (/dev/video0) | 视觉模型: YOLOv10-nano (ncnn CPU) | NPU视觉识别已禁用"
        )
        sections.append(hw_context)

        system_prompt = "\n\n---\n\n".join(sections)

        _SYSTEM_PROMPT_CACHE = system_prompt
        _SYSTEM_PROMPT_CACHE_TS = now

    if extra_context:
        system_prompt += f"\n\n---\n\n{extra_context}"

    return system_prompt


AGENT_CONFIG = load_agent_config()

__all__ = [
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "MODEL_NAME",
    "AGENT_CONFIG",
    "DATA_DIR",
    "LOG_DIR",
    "WORKSPACE_DIR",
    "CREDENTIALS_DIR",
    "STICKER_DIR",
    "KLEE_STICKER_DIR",
    "FILE_DIR",
    "build_system_prompt",
    "load_agent_config",
    "load_workspace_file",
]