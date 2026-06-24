import os
import re
import json
import time
import sys
import shutil
import platform
import socket
from pathlib import Path
from dotenv import load_dotenv

def get_base_dir() -> Path:
    """获取项目根目录。PyInstaller 打包后返回可执行文件所在目录，开发模式返回项目根目录。"""
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def get_env_path() -> Path:
    """返回 .env 文件路径。

    PyInstaller 打包后，如果安装到 C:\\Program Files\\ 等系统保护目录，
    非管理员用户无法写入。此时将 .env 存放到用户目录 ~/.ai-agent/.env，
    确保所有用户都能正常读写配置。
    """
    if getattr(sys, 'frozen', False):
        user_env = Path.home() / ".ai-agent" / ".env"
        user_env.parent.mkdir(parents=True, exist_ok=True)
        # 迁移：如果用户目录没有 .env 但 exe 目录有（旧版以管理员运行过），自动迁移
        if not user_env.exists():
            old_env = Path(sys.executable).parent / ".env"
            if old_env.exists():
                try:
                    import shutil
                    shutil.copy2(old_env, user_env)
                    print(f"[config] .env migrated from {old_env} to {user_env}")
                except Exception:
                    pass  # 迁移失败不阻塞启动，用户可在 Setup 页面重新配置
        return user_env
    # 开发模式：使用项目根目录
    return Path(__file__).resolve().parent / ".env"


ENV_PATH = get_env_path()
load_dotenv(ENV_PATH, override=True)

# 确保 PyInstaller 打包后 HTTPS 请求能找到 CA 证书
# certifi 的 cacert.pem 必须被正确打包，否则所有 API 请求都会因 SSL 错误失败
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

_KIOXIA_BASE = Path(os.getenv("KIOXIA_DATA_DIR", str(Path.home() / ".ai-agent" / "data")))

def _get_fallback_base() -> Path:
    """获取 fallback 基础路径。
    PyInstaller 打包后使用用户目录 ~/.ai-agent/，确保更新安装包时数据不会丢失。
    开发模式使用项目根目录。
    """
    if getattr(sys, 'frozen', False):
        return Path.home() / ".ai-agent"
    return Path(__file__).resolve().parent

_FALLBACK_BASE = _get_fallback_base()


def _migrate_old_data(old_dir: Path, new_dir: Path, name: str):
    """将旧目录的数据迁移到新目录（仅首次）。
    用于从 exe 目录迁移到用户目录，解决更新安装包导致数据丢失的问题。
    """
    if new_dir.exists() and any(new_dir.iterdir()):
        return  # 新目录已有数据，跳过
    if not old_dir.exists() or not any(old_dir.iterdir()):
        return  # 旧目录无数据，跳过
    try:
        import shutil
        shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        print(f"[config] {name} migrated from {old_dir} to {new_dir}")
    except Exception as e:
        print(f"[config] Warning: failed to migrate {name}: {e}")

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
    """获取配置目录（用于 webui_overrides.json 等可写配置）。

    frozen 模式下使用用户目录 ~/.ai-agent/config/，
    避免写入 C:\\Program Files\\ 等需要管理员权限的目录。
    """
    if getattr(sys, 'frozen', False):
        user_config = Path.home() / ".ai-agent" / "config"
        # 迁移：如果旧安装目录有配置文件但用户目录没有，复制过来
        old_config = get_base_dir() / "config"
        if old_config.exists() and not user_config.exists():
            import shutil
            try:
                shutil.copytree(old_config, user_config, dirs_exist_ok=True)
            except Exception:
                pass
        user_config.mkdir(parents=True, exist_ok=True)
        return user_config
    return get_base_dir() / "config"

def _resolve_data_path(kioxia_path: Path, fallback_path: Path) -> Path:
    if kioxia_path.exists() or kioxia_path.parent.exists():
        kioxia_path.mkdir(parents=True, exist_ok=True)
        return kioxia_path
    fallback_path.mkdir(parents=True, exist_ok=True)
    return fallback_path

DATA_DIR = _resolve_data_path(_KIOXIA_BASE / "db", _FALLBACK_BASE / "data")
LOG_DIR = _resolve_data_path(_KIOXIA_BASE / "logs", _FALLBACK_BASE / "logs")
WORKSPACE_DIR = _resolve_data_path(_KIOXIA_BASE / "config" / "workspace", _FALLBACK_BASE / "workspace")
CREDENTIALS_DIR = get_credentials_dir()
AGENT_CONFIG_PATH = (_KIOXIA_BASE / "config" / "agent.json5") if (_KIOXIA_BASE / "config").exists() else _FALLBACK_BASE / "agent.json5"
STICKER_DIR = _resolve_data_path(_KIOXIA_BASE / "stickers", _FALLBACK_BASE / "stickers")
KLEE_STICKER_DIR = _resolve_data_path(_KIOXIA_BASE / "klee-stickers", _FALLBACK_BASE / "klee-stickers")
FILE_DIR = _resolve_data_path(_KIOXIA_BASE / "files", _FALLBACK_BASE / "files")

# ── 数据迁移：frozen 模式下从 exe 目录迁移到用户目录 ──
# 解决更新安装包导致数据丢失（"刷机"）的问题
if getattr(sys, 'frozen', False):
    _exe_base = Path(sys.executable).parent
    # 迁移记忆数据库
    _migrate_old_data(_exe_base / "data", DATA_DIR, "database")
    # 迁移日志
    _migrate_old_data(_exe_base / "logs", LOG_DIR, "logs")
    # 迁移工作区（知识笔记、SOUL.md 等）
    _migrate_old_data(Path(os.path.expanduser("~/.ai-agent/workspace")), WORKSPACE_DIR, "workspace")
    # 迁移贴纸
    _migrate_old_data(_exe_base / "stickers", STICKER_DIR, "stickers")
    # 迁移文件存储
    _migrate_old_data(_exe_base / "files", FILE_DIR, "files")

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


# ── 系统提示词构建相关函数已拆分到 prompt_builder.py ──────────
# 为保持向后兼容，文件末尾通过 `from prompt_builder import *` 重新导出：
#   build_system_prompt / build_safe_system_prompt / load_workspace_file
#   load_skills / _ensure_workspace_template 等


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

# Retrieval Optimization (A1/A2/A3)
RETRIEVAL_SMART_SKIP = os.getenv("RETRIEVAL_SMART_SKIP", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_TRANSFORM = os.getenv("RETRIEVAL_PARALLEL_TRANSFORM", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_SEARCH = os.getenv("RETRIEVAL_PARALLEL_SEARCH", "true").lower() in ("1", "true", "yes")

# ── 性能优化开关 ──────────────────────────────────────────────
# Task 6: TTS 异步化（方案 B）—— 开启后 TTS 在后台合成，先返回文字回复
TTS_ASYNC_MODE = os.getenv("TTS_ASYNC_MODE", "true").lower() in ("1", "true", "yes")
# Task 7: 流式中间状态推送（方案 C1）—— 开启后推送细粒度思考状态
STREAM_STATUS_PUSH = os.getenv("STREAM_STATUS_PUSH", "false").lower() in ("1", "true", "yes")
# Task 9: 简单对话快速路径（方案 E）—— 开启后简单闲聊跳过记忆检索
SIMPLE_CHAT_FASTPATH = os.getenv("SIMPLE_CHAT_FASTPATH", "true").lower() in ("1", "true", "yes")

# RAG Fusion Weights
RAG_RERANK_WEIGHT = float(os.getenv("RAG_RERANK_WEIGHT", "0.65"))
RAG_KG_WEIGHT = float(os.getenv("RAG_KG_WEIGHT", "0.15"))
RAG_IMPORTANCE_WEIGHT = float(os.getenv("RAG_IMPORTANCE_WEIGHT", "0.20"))

# MCP_SERVERS：使用 shutil.which() 动态解析命令路径，兼容 Windows/Linux/macOS
# 不再硬编码 Orange Pi 上的绝对路径，避免在其他设备上失效


def _resolve_command(name: str) -> str:
    """解析命令完整路径，兼容 systemd 等受限 PATH 环境。"""
    path = shutil.which(name)
    if path:
        return path
    # shutil.which 在 systemd 等环境中可能找不到 ~/.local/bin 下的命令
    # 检查常见安装路径
    for candidate in [
        Path.home() / ".local" / "bin" / name,
        Path("/usr/local/bin") / name,
        Path.home() / ".cargo" / "bin" / name,
    ]:
        if candidate.exists():
            return str(candidate)
    # Windows: 检查 npm 全局目录
    if sys.platform == "win32":
        appdata = os.getenv("APPDATA", "")
        if appdata:
            npm_path = Path(appdata) / "npm" / f"{name}.cmd"
            if npm_path.exists():
                return str(npm_path)
    return name  # fallback: 返回命令名本身


MCP_SERVERS = {
    "git": {
        "command": _resolve_command("uvx"),
        "args": ["mcp-server-git", "--repository", str(Path.home() / "Desktop")],
        "env": {"UV_INDEX_URL": "https://pypi.tuna.tsinghua.edu.cn/simple"},
        "agents": ["yinlang"],  # which agents can use this MCP server's tools
    },
    "github": {
        "command": _resolve_command("npx"),
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
    "build_safe_system_prompt",
    "load_agent_config",
    "load_workspace_file",
    "load_skills",
    "_ensure_workspace_template",
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
    "RETRIEVAL_SMART_SKIP",
    "RETRIEVAL_PARALLEL_TRANSFORM",
    "RETRIEVAL_PARALLEL_SEARCH",
    "TTS_ASYNC_MODE",
    "STREAM_STATUS_PUSH",
    "SIMPLE_CHAT_FASTPATH",
    "RAG_RERANK_WEIGHT",
    "RAG_KG_WEIGHT",
    "RAG_IMPORTANCE_WEIGHT",
    "ASR_API_KEY",
    "ASR_BASE_URL",
    "ASR_MODEL",
]

# ── 向后兼容：从 prompt_builder 重新导出已拆分的函数 ──────────
# 放在文件末尾以避免循环导入：此时 config 的常量与 __all__ 均已定义完毕，
# prompt_builder 内部对 config 常量的延迟导入可在调用时正常解析。
from prompt_builder import *  # noqa: E402,F401,F403
