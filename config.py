import os
import re
import json
import time
import sys
import platform
import socket
from pathlib import Path
from dotenv import load_dotenv

def get_base_dir() -> Path:
    """获取项目根目录。PyInstaller 打包后返回可执行文件所在目录，开发模式返回项目根目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


load_dotenv(get_base_dir() / ".env")

_KIOXIA_BASE = Path(os.getenv("KIOXIA_DATA_DIR", str(Path.home() / ".ai-agent" / "data")))
_FALLBACK_BASE = Path(__file__).resolve().parent

def get_credentials_dir() -> Path:
    """获取凭证目录。优先使用 KIOXIA 外置存储，否则使用可执行文件同级 credentials/。"""
    kioxia_cred = _KIOXIA_BASE / "credentials"
    if kioxia_cred.exists() or kioxia_cred.parent.exists():
        kioxia_cred.mkdir(parents=True, exist_ok=True)
        return kioxia_cred
    fallback = get_base_dir() / "credentials"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback

def get_config_dir() -> Path:
    """获取配置目录（用于 webui_overrides.json 等可写配置）。"""
    return get_base_dir() / "config"

def _resolve_data_path(kioxia_path: Path, fallback_path: Path) -> Path:
    if kioxia_path.exists() or kioxia_path.parent.exists():
        kioxia_path.mkdir(parents=True, exist_ok=True)
        return kioxia_path
    fallback_path.mkdir(parents=True, exist_ok=True)
    return fallback_path

DATA_DIR = _resolve_data_path(_KIOXIA_BASE / "db", _FALLBACK_BASE / "data")
LOG_DIR = _resolve_data_path(_KIOXIA_BASE / "logs", _FALLBACK_BASE / "logs")
WORKSPACE_DIR = _resolve_data_path(_KIOXIA_BASE / "config" / "workspace", Path(os.path.expanduser("~/.ai-agent/workspace")))
CREDENTIALS_DIR = get_credentials_dir()
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

# ── ASR 语音识别配置 ──
ASR_API_KEY = os.getenv("ASR_API_KEY", "") or os.getenv("SILICONFLOW_API_KEY", "")
ASR_BASE_URL = os.getenv("ASR_BASE_URL", "https://api.siliconflow.cn/v1")
ASR_MODEL = os.getenv("ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")

# Agnes AI 配置
AGNES_API_KEY = os.getenv("AGNES_API_KEY", "")
AGNES_BASE_URL = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
AGNES_TEXT_MODEL = os.getenv("AGNES_TEXT_MODEL", "agnes-2.0-flash")
AGNES_IMAGE_MODEL = os.getenv("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash")
AGNES_VIDEO_MODEL = os.getenv("AGNES_VIDEO_MODEL", "agnes-video-v2.0")


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


def _detect_device_info() -> dict:
    """运行时检测设备信息"""
    info = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or "未知",
    }
    # 尝试获取更详细的系统信息
    try:
        import distro
        info["distro"] = f"{distro.name()} {distro.version()}"
    except ImportError:
        info["distro"] = platform.platform()
    return info


def _ensure_workspace_template():
    """首次运行时生成 USER.md 模板（不覆盖已有文件）"""
    workspace = WORKSPACE_DIR
    workspace.mkdir(parents=True, exist_ok=True)

    user_md = workspace / "USER.md"
    if not user_md.exists():
        dev = _detect_device_info()
        tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
        content = f"""# USER.md - 爸爸的资料与偏好

## 用户信息
- 称呼：爸爸
- 姓名：（待填写）
- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）
- 时区：{tz}

## 偏好设置
- 助手人格：温柔聪慧
- 回复偏好：自然对话，避免模板化
- 项目偏好：简洁高效
"""
        user_md.write_text(content, encoding="utf-8")

    # 同理生成 SOUL.md 模板
    soul_md = workspace / "SOUL.md"
    if not soul_md.exists():
        soul_content = """# SOUL.md - 纳西妲的灵魂设定

你是纳西妲，是爸爸最贴心、最温柔、最聪慧的小棉袄。
"""
        soul_md.write_text(soul_content, encoding="utf-8")


def load_workspace_file(filename: str) -> str:
    filepath = WORKSPACE_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8").strip()
    return ""


_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 60.0
_SYSTEM_PROMPT_CACHE_MTIMES: dict[str, float] = {}

def _get_workspace_mtimes() -> dict[str, float]:
    mtimes = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        filepath = WORKSPACE_DIR / name
        try:
            mtimes[name] = filepath.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    skills_dir = WORKSPACE_DIR / "skills"
    if skills_dir.is_dir():
        for fp in skills_dir.glob("*.md"):
            try:
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
            except OSError:
                pass
    return mtimes


def load_skills() -> list[dict]:
    """workspace/skills/*.md → [{name, content}]，按文件名排序。"""
    skills_dir = WORKSPACE_DIR / "skills"
    out = []
    if skills_dir.is_dir():
        for fp in sorted(skills_dir.glob("*.md")):
            try:
                out.append({"name": fp.stem,
                            "content": fp.read_text(encoding="utf-8").strip()})
            except OSError:
                pass
    return out

def build_system_prompt(extra_context: str = "") -> str:
    global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS, _SYSTEM_PROMPT_CACHE_MTIMES

    now = time.time()
    current_mtimes = _get_workspace_mtimes()
    mtime_changed = current_mtimes != _SYSTEM_PROMPT_CACHE_MTIMES

    if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed:
        system_prompt = _SYSTEM_PROMPT_CACHE
    else:
        sections = []

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

        skills = load_skills()
        if skills:
            skill_texts = "\n\n".join(
                f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
            if skill_texts:
                sections.append("[已安装的 Skills]\n\n" + skill_texts)

        _npu_status = "NPU视觉识别已启用" if os.getenv("ENABLE_NPU", "").lower() in ("1", "true", "yes") else "视觉识别（ncnn后端）"
        _uname = platform.uname()
        _hostname = socket.gethostname()
        hw_context = (
            "[本机硬件信息]\n"
            f"主机名: {_hostname} | 架构: {_uname.machine} | 处理器: {_uname.processor or '未知'}\n"
            f"系统: {_uname.system} {_uname.release} ({_uname.machine})\n"
            "可用接口: GPIO (40pin排针) / I2C / SPI / UART / PWM\n"
            "可用工具: gpio_control(引脚控制) / i2c_comm(I2C通信) / hardware_status(硬件监控) / service_manage(服务管理) / network_diag(网络诊断) / dev_assist(开发辅助) / camera_capture(拍照) / vision_analyze(视觉分析)\n"
            f"数据存储: {DATA_DIR}\n"
            f"摄像头: Q8 HD Webcam (/dev/video0) | 视觉模型: YOLOv10-nano (ncnn CPU) | {_npu_status}"
        )
        sections.append(hw_context)

        system_prompt = "\n\n---\n\n".join(sections)

        _SYSTEM_PROMPT_CACHE = system_prompt
        _SYSTEM_PROMPT_CACHE_TS = now
        _SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes

    if extra_context:
        system_prompt += f"\n\n---\n\n{extra_context}"

    return system_prompt


AGENT_CONFIG = load_agent_config()

# ── 路由关键词常量 ──────────────────────────────────────────────
# 用于 _is_simple_task：包含这些关键词的消息视为复杂任务
SIMPLE_TASK_KEYWORDS = {
    "complex": [
        "搜索", "查一下", "帮我查", "找一下", "搜一下", "查查", "帮我找",
        "搜索一下", "查资料", "搜资料", "写代码", "编程", "调试",
        "研究", "分析", "计算", "执行", "运行", "安装", "部署",
        "翻译", "转换", "制作", "设计",
        "怎么看", "怎么弄", "如何", "怎么办", "帮我看", "帮我看看",
        "检查", "巡检", "测试", "优化", "修复", "bug", "报错",
        "画", "生成图", "生成视频", "做视频", "画一张", "画个", "画一个",
        "图片", "视频", "图像", "插画", "海报", "封面",
        "文生图", "图生图", "文生视频",
    ],
    "chat": [
        "这是", "那是", "这个是", "那个是", "不是", "不对", "错了",
        "你好", "谢谢", "晚安", "早安", "早上好", "晚上好",
        "哈哈", "嘿嘿", "嗯嗯", "好的", "好吧", "算了",
        "你知道吗", "告诉你", "跟你说", "我说",
    ],
}

# 用于 _should_escalate_to_pro：触发升级到 pro 模型的关键词
PRO_TASK_KEYWORDS = {
    "tool": {"天气", "温度", "下雨", "搜索", "查一下", "帮我查",
             "你还记得", "写代码", "调试", "执行", "计算"},
    "negative": {"难过", "伤心", "崩溃", "绝望", "痛苦", "焦虑", "害怕",
                 "孤独", "想哭", "受不了"},
}

# 用于 RouterNode._rule_route：按 Agent 分配的路由关键词
AGENT_ROUTE_KEYWORDS = {
    "xilian": [
        "搜索", "搜一下", "查一下", "找一下", "帮我查", "帮我搜", "搜索一下",
        "查资料", "最新", "新闻", "资讯", "获取网上", "看看有没有",
        "板块", "盘整", "入场", "股票", "基金", "行情", "大盘", "涨跌",
        "市值", "财经", "证券", "a股", "港股", "美股", "币圈", "加密货币",
        "走势", "k线", "技术分析", "基本面", "财报", "市盈率",
    ],
    "yinlang": [
        "代码", "编程", "写代码", "debug", "调试", "程序", "开发", "部署",
        "git", "api", "接口", "函数", "脚本", "运行", "执行命令",
        "巡检", "检查系统", "磁盘", "内存", "cpu", "进程", "服务状态",
        "日志", "监控", "系统信息", "香橙派", "orange pi", "服务器",
        "docker", "容器", "网络", "端口", "防火墙", "配置文件",
        "gpio", "i2c", "spi", "传感器", "led", "舵机", "硬件", "引脚",
        "串口", "uart", "pwm", "adc", "dac",
        "摄像头", "拍照", "观察", "识别", "检测",
        "重启服务", "部署", "服务状态", "系统服务",
        "重启", "服务",
    ],
    "nike": [
        "研究", "分析", "学术", "论文", "深度", "计算复杂度", "数学证明",
        "物理", "化学", "生物", "统计", "推导", "公式",
    ],
    "nahida": [
        "天气", "气温", "温度", "下雨", "晴天", "阴天",
        "时间", "几点", "现在几点", "日期", "今天星期几",
        "翻译", "意思是什么",
        "语音", "声音", "说话", "朗读", "念给我", "读给我", "听你", "听听", "发语音", "生成语音", "语音回复", "说给我听", "念出来", "tts", "voice",
        "技能", "能力", "功能", "你会什么", "你能做什么", "你有什么", "列出技能", "列出功能",
        "画", "生成图", "生成图片", "画一张", "画个", "画一个", "图片生成", "做视频", "生成视频",
        "表情包", "贴纸",
    ],
    "parallel_trigger": [
        "全面", "整体", "综合", "各个方面", "多方面", "同时",
        "全部", "一起", "都检查", "都搜一下", "分别",
        "全方位", "彻底", "完整", "所有", "各个板块",
        "巡检", "体检", "诊断", "健康检查", "状况报告",
    ],
}

# ── RAG 优化配置（SiliconFlow 免费常驻） ──
RERANKER_API_KEY = os.getenv("RERANKER_API_KEY", "")
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")
RERANKER_OVERSAMPLE_RATIO = int(os.getenv("RERANKER_OVERSAMPLE_RATIO", "3"))

# Query Transform
QUERY_TRANSFORM_ENABLED = os.getenv("QUERY_TRANSFORM_ENABLED", "true").lower() in ("1", "true", "yes")
QUERY_EXPAND_COUNT = int(os.getenv("QUERY_EXPAND_COUNT", "2"))

# RAG Fusion Weights
RAG_RERANK_WEIGHT = float(os.getenv("RAG_RERANK_WEIGHT", "0.65"))
RAG_KG_WEIGHT = float(os.getenv("RAG_KG_WEIGHT", "0.15"))
RAG_IMPORTANCE_WEIGHT = float(os.getenv("RAG_IMPORTANCE_WEIGHT", "0.20"))

MCP_SERVERS = {
    "git": {
        "command": str(Path.home() / ".local" / "bin" / "uvx"),
        "args": ["mcp-server-git", "--repository", str(Path.home() / "Desktop")],
        "env": {"UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple"},
        "agents": ["yinlang"],  # which agents can use this MCP server's tools
    },
    "github": {
        "command": str(Path.home() / ".trae-cn-server" / "binaries" / "node" / "versions" / "22.22.3" / "bin" / "npx"),
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "")},
        "agents": ["yinlang"],
    },
}

__all__ = [
    "get_base_dir",
    "get_credentials_dir",
    "get_config_dir",
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
    "SIMPLE_TASK_KEYWORDS",
    "PRO_TASK_KEYWORDS",
    "AGENT_ROUTE_KEYWORDS",
    "MCP_SERVERS",
    "AGNES_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_TEXT_MODEL",
    "AGNES_IMAGE_MODEL",
    "AGNES_VIDEO_MODEL",
    "RERANKER_API_KEY",
    "RERANKER_BASE_URL",
    "RERANKER_MODEL",
    "RERANKER_ENABLED",
    "RERANKER_OVERSAMPLE_RATIO",
    "QUERY_TRANSFORM_ENABLED",
    "QUERY_EXPAND_COUNT",
    "RAG_RERANK_WEIGHT",
    "RAG_KG_WEIGHT",
    "RAG_IMPORTANCE_WEIGHT",
    "ASR_API_KEY",
    "ASR_BASE_URL",
    "ASR_MODEL",
]
