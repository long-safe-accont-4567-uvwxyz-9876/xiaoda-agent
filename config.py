import logging
import os
import re
import json
import sys

logger = logging.getLogger(__name__)
from typing import Any
import shutil
from pathlib import Path
from dotenv import load_dotenv
from utils.encrypted_credential import protect_credential
from security import credential_vault


def get_secret(name: str, default: str = "") -> str:
    """读取敏感环境变量并自动解密 enc:v1: 格式的密文

    非 enc:v1: 前缀的值视为明文直接返回（向后兼容）。
    解密失败（如机器不匹配、HMAC 验证失败）返回空字符串，避免明文泄漏。
    仅用于 API Key / Token / Secret 类敏感配置，普通配置仍使用 os.getenv。
    """
    value = os.getenv(name)
    if value is None:
        return default
    if not value:
        return value
    try:
        return credential_vault.decrypt(value)
    except credential_vault.DecryptionError as e:
        logger.warning(f"config.decrypt_failed: {name} ({e})")
        return default


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
        migration_marker = user_env.parent / ".env.migrated"
        # 迁移：如果用户目录没有 .env 但 exe 目录有（旧版以管理员运行过），自动迁移
        # 使用标记文件避免 Docker 重启时重复迁移
        if not user_env.exists() and not migration_marker.exists():
            old_env = Path(sys.executable).parent / ".env"
            if old_env.exists():
                try:
                    shutil.copy2(old_env, user_env)
                    migration_marker.touch()
                    print(f"[config] .env migrated from {old_env} to {user_env}")
                except (OSError, shutil.Error) as e:
                    logger.debug("config.env_migrate_failed: %s", e)
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


def _migrate_old_data(old_dir: Path, new_dir: Path, name: str) -> None:
    """将旧目录的数据迁移到新目录（仅首次）。
    用于从 exe 目录迁移到用户目录，解决更新安装包导致数据丢失的问题。
    """
    if new_dir.exists() and any(new_dir.iterdir()):
        return  # 新目录已有数据，跳过
    if not old_dir.exists() or not any(old_dir.iterdir()):
        return  # 旧目录无数据，跳过
    try:
        shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        print(f"[config] {name} migrated from {old_dir} to {new_dir}")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("config.migrate_failed name=%s error=%s", name, e)

def get_credentials_dir() -> Path:
    """获取凭证目录。优先使用 KIOXIA 外置存储，否则使用可执行文件同级 credentials/。"""
    kioxia_cred = _KIOXIA_BASE / "credentials"
    try:
        if kioxia_cred.exists() or kioxia_cred.parent.exists():
            kioxia_cred.mkdir(parents=True, exist_ok=True)
            return kioxia_cred
    except (OSError, PermissionError):
        logger.debug("config.credentials_dir_setup_failed", exc_info=True)
    fallback = _FALLBACK_BASE / "credentials"
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
            try:
                shutil.copytree(old_config, user_config, dirs_exist_ok=True)
            except (OSError, shutil.Error) as e:
                logger.debug("config.dir_migrate_failed: %s", e)
        user_config.mkdir(parents=True, exist_ok=True)
        return user_config
    # Docker 环境：使用 KIOXIA_DATA_DIR（volume 挂载的持久化目录）
    kioxia = os.getenv("KIOXIA_DATA_DIR", "")
    if kioxia:
        config_dir = Path(kioxia) / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        return config_dir
    return get_base_dir() / "config"

def _resolve_data_path(kioxia_path: Path, fallback_path: Path) -> Path:
    """解析数据路径，优先使用 KIOXIA 外置存储，失败时降级到 fallback。

    注意：fallback_path 必须与 kioxia_path 结构一致（如都是 .../db），
    避免首次/二次启动路径翻转导致数据孤立。
    """
    kioxia_env = os.getenv("KIOXIA_DATA_DIR", "")
    try:
        if kioxia_path.exists() or kioxia_path.parent.exists():
            kioxia_path.mkdir(parents=True, exist_ok=True)
            # 运行时只读检测：尝试写入临时文件验证文件系统是否可写
            # 修复：FAT 文件系统错误导致 remount 只读时，os.access(W_OK) 仍返回 True
            # 因此需要实际写入测试文件
            _probe = kioxia_path / ".write_probe"
            try:
                _probe.write_text("probe", encoding="utf-8")
                _probe.unlink(missing_ok=True)
                logger.debug("config.data_path_writable path=%s", kioxia_path)
            except (OSError, PermissionError):
                logger.warning("config.data_path_readonly path=%s", kioxia_path)
                raise OSError(f"Filesystem is read-only: {kioxia_path}")
            return kioxia_path
    except (OSError, PermissionError):
        logger.debug("config.data_path_resolve_failed", exc_info=True)
    # 外置盘未挂载或不可写时降级到 fallback，并输出警告
    if kioxia_env:
        print(f"[config] WARNING: KIOXIA_DATA_DIR={kioxia_env} not available, "
              f"falling back to {fallback_path}")
    try:
        fallback_path.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        # 连 fallback 都失败，使用临时目录
        import tempfile
        fallback_path = Path(tempfile.gettempdir()) / "xiaoda-agent" / fallback_path.name
        fallback_path.mkdir(parents=True, exist_ok=True)
    return fallback_path

DATA_DIR = _resolve_data_path(_KIOXIA_BASE / "db", _FALLBACK_BASE / "db")
LOG_DIR = _resolve_data_path(_KIOXIA_BASE / "logs", _FALLBACK_BASE / "logs")
WORKSPACE_DIR = _resolve_data_path(_KIOXIA_BASE / "config" / "workspace", _FALLBACK_BASE / "config" / "workspace")
CREDENTIALS_DIR = get_credentials_dir()


def is_data_dir_writable() -> bool:
    """运行时检测数据目录是否可写（用于外置盘只读故障检测）。

    Returns:
        True 表示可写，False 表示文件系统已变为只读。
    此函数适用于运行时周期检测，不会阻塞主流程。
    """
    try:
        _probe = DATA_DIR / ".write_probe_runtime"
        _probe.write_text("probe", encoding="utf-8")
        _probe.unlink(missing_ok=True)
        return True
    except (OSError, PermissionError):
        return False


def _init_user_resources() -> None:
    """frozen 模式下首次运行时，从打包资源（_MEIPASS）复制配置文件到用户目录。

    解决问题：agent.json5/workspace 模板打包在 _internal/config/ 里，
    但用户目录 ~/.ai-agent/data/config/ 首次运行时是空的，导致配置丢失。
    """
    if not getattr(sys, 'frozen', False):
        return
    meipass = getattr(sys, '_MEIPASS', '')
    if not meipass:
        return
    bundled_config = Path(meipass) / "config"
    if not bundled_config.exists():
        return

    user_config_dir = _KIOXIA_BASE / "config"
    user_config_dir.mkdir(parents=True, exist_ok=True)

    _init_agent_json5(bundled_config, user_config_dir)
    _init_agents_subdir(bundled_config, user_config_dir)
    _init_workspace_templates(bundled_config)


def _init_agent_json5(bundled_config: Path, user_config_dir: Path) -> None:
    """复制 agent.json5 到用户配置目录（首次运行）"""
    bundled_agent_json5 = bundled_config / "agent.json5"
    user_agent_json5 = user_config_dir / "agent.json5"
    if bundled_agent_json5.exists() and not user_agent_json5.exists():
        try:
            shutil.copy2(bundled_agent_json5, user_agent_json5)
            print("[config] agent.json5 initialized from bundled resource")
        except Exception as e:
            print(f"[config] Warning: failed to copy agent.json5: {e}")


def _init_agents_subdir(bundled_config: Path, user_config_dir: Path) -> None:
    """复制 agents/ 子目录（子 Agent 配置和人格文件）并清理旧版配置"""
    bundled_agents = bundled_config / "agents"
    user_agents = user_config_dir / "agents"
    if bundled_agents.exists():
        user_agents.mkdir(parents=True, exist_ok=True)
        # 逐文件补复制缺失的配置和人格文件（升级时也补齐）
        for item in bundled_agents.iterdir():
            if item.is_file():
                target = user_agents / item.name
                if not target.exists():
                    try:
                        shutil.copy2(item, target)
                        print(f"[config] Copied new agent file: {item.name}")
                    except Exception as e:
                        print(f"[config] Warning: failed to copy {item.name}: {e}")

    # 清理旧版 agent 配置文件（升级后旧名称不应残留）
    if user_agents.exists():
        _deprecated_agents = {"nahida.json", "keli.json", "yinlang.json", "xilian.json", "nike.json"}
        for old_file in _deprecated_agents:
            old_path = user_agents / old_file
            if old_path.exists():
                try:
                    old_path.unlink()
                    print(f"[config] Removed deprecated agent config: {old_file}")
                except Exception as e:
                    print(f"[config] Warning: failed to remove {old_file}: {e}")


def _init_workspace_templates(bundled_config: Path) -> None:
    """复制 workspace/ 模板文件（SOUL.md, IDENTITY.md 等）及子目录

    非用户编辑类文件（TOOLS.md, AGENTS.md）强制更新，用户编辑类文件不覆盖。
    """
    bundled_workspace = bundled_config / "workspace"
    if not bundled_workspace.exists():
        return

    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    # 这些文件用户不会编辑，每次启动强制更新
    _force_update_files = {"TOOLS.md", "AGENTS.md", "HEARTBEAT.md"}
    for item in bundled_workspace.iterdir():
        if item.is_dir():
            continue
        # .tpl 文件复制时去除 .tpl 后缀
        target_name = item.name[:-4] if item.name.endswith('.tpl') else item.name
        target = WORKSPACE_DIR / target_name
        # 强制更新文件总是覆盖，用户编辑文件不覆盖
        # SOUL.md 特殊处理：如果包含旧名（nahida），强制更新
        should_copy = target_name in _force_update_files or not target.exists()
        if not should_copy and target_name == "SOUL.md" and target.exists():
            try:
                old_content = target.read_text(encoding="utf-8")
                if "nahida" in old_content.lower() or "纳西妲" in old_content:
                    should_copy = True
                    print("[config] Updating outdated SOUL.md (contains old name)")
            except (OSError, UnicodeDecodeError):
                logger.debug("config.soul_md_check_failed", exc_info=True)
        if should_copy:
            try:
                shutil.copy2(item, target)
            except (OSError, shutil.Error) as e:
                logger.debug("config.workspace_copy_failed %s: %s", target_name, e)

    # 复制 workspace/ 子目录（workflows/, skills/ 等默认资源，不覆盖已有文件）
    for sub_name in ("workflows", "skills"):
        bundled_sub = bundled_workspace / sub_name
        if not bundled_sub.is_dir():
            continue
        user_sub = WORKSPACE_DIR / sub_name
        user_sub.mkdir(parents=True, exist_ok=True)
        for item in bundled_sub.iterdir():
            if not item.is_file():
                continue
            target = user_sub / item.name
            if not target.exists():
                try:
                    shutil.copy2(item, target)
                except (OSError, shutil.Error) as e:
                    logger.debug("config.workspace_sub_copy_failed %s: %s", item.name, e)


_workspace_initialized = False


def _ensure_workspace() -> None:
    """惰性初始化：frozen 模式下复制打包资源、迁移旧数据。

    仅在首次显式调用时执行一次，避免模块导入时产生 IO 副作用。
    """
    global _workspace_initialized, _KIOXIA_AVAILABLE
    if _workspace_initialized:
        return
    _workspace_initialized = True

    _init_user_resources()

    # ── 数据迁移：frozen 模式下从 exe 目录迁移到用户目录 ──
    # 解决更新安装包导致数据丢失（"刷机"）的问题
    if getattr(sys, 'frozen', False):
        _exe_base = Path(sys.executable).parent
        _migrate_old_data(_exe_base / "data", DATA_DIR, "database")
        _migrate_old_data(_exe_base / "logs", LOG_DIR, "logs")
        _migrate_old_data(Path(os.path.expanduser("~/.ai-agent/workspace")), WORKSPACE_DIR, "workspace")
        _migrate_old_data(_exe_base / "stickers", STICKER_DIR, "stickers")
        _migrate_old_data(_exe_base / "xiaoli-stickers", XIAOLI_STICKER_DIR, "xiaoli-stickers")
        _migrate_old_data(_exe_base / "agent-stickers", AGENT_STICKER_BASE, "agent-stickers")
        _migrate_old_data(_exe_base / "files", FILE_DIR, "files")
        _migrate_old_data(_exe_base / "media", MEDIA_DIR, "media")
        _migrate_old_data(_exe_base / "voice_refs", VOICE_REF_DIR, "voice_refs")
        _migrate_old_data(_exe_base / "memory_state", MEMORY_STATE_DIR, "memory_state")
        _migrate_old_data(_exe_base / "plugins", PLUGINS_CONFIG_DIR, "plugins")

    _KIOXIA_AVAILABLE = (_KIOXIA_BASE / "db").exists()


# 在路径解析前执行初始化，确保 frozen 模式下的资源复制和数据迁移正常进行
# 原始代码在模块顶层直接调用 _init_user_resources()，此处保持相同行为
_ensure_workspace()


AGENT_CONFIG_PATH = (_KIOXIA_BASE / "config" / "agent.json5") if (_KIOXIA_BASE / "config").exists() else _FALLBACK_BASE / "agent.json5"
STICKER_DIR = _resolve_data_path(_KIOXIA_BASE / "stickers", _FALLBACK_BASE / "stickers")
XIAOLI_STICKER_DIR = _resolve_data_path(_KIOXIA_BASE / "xiaoli-stickers", _FALLBACK_BASE / "xiaoli-stickers")
# 通用智能体表情包根目录：每个子智能体的表情包存放在 {AGENT_STICKER_BASE}/{agent_name}/
AGENT_STICKER_BASE = _resolve_data_path(_KIOXIA_BASE / "agent-stickers", _FALLBACK_BASE / "agent-stickers")
FILE_DIR = _resolve_data_path(_KIOXIA_BASE / "files", _FALLBACK_BASE / "files")
# 媒体目录（用户上传图片、生成的 TTS/图片/视频、壁纸等可写资源）
MEDIA_DIR = _resolve_data_path(_KIOXIA_BASE / "media", _FALLBACK_BASE / "media")
# 参考音频目录（用户上传的 TTS 参考音频，按 agent 分子目录）
VOICE_REF_DIR = _resolve_data_path(_KIOXIA_BASE / "voice_refs", _FALLBACK_BASE / "voice_refs")
# 记忆状态目录（记忆编码状态等运行时可写数据）
MEMORY_STATE_DIR = _resolve_data_path(_KIOXIA_BASE / "memory_state", _FALLBACK_BASE / "memory_state")
# 插件配置目录
PLUGINS_CONFIG_DIR = _resolve_data_path(_KIOXIA_BASE / "plugins", _FALLBACK_BASE / "plugins")
# 子 Agent 配置目录（人格文件、配置 JSON）
AGENTS_CONFIG_DIR = _KIOXIA_BASE / "config" / "agents"
if not AGENTS_CONFIG_DIR.exists():
    AGENTS_CONFIG_DIR = _FALLBACK_BASE / "config" / "agents"
AGENTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)

DEEPSEEK_API_KEY = get_secret("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# MIMO_API_KEY：先用 get_secret 解密 enc:v1: 密文，再交给 protect_credential 做内存态保护
MIMO_API_KEY = protect_credential(get_secret("MIMO_API_KEY", ""))
MIMO_BASE_URL = os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")
MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")

# ── 反代客户端 IP 解析 ──
# 默认 False：使用 TCP 对端 request.client.host（最安全）。
# 设为 True 时从 X-Forwarded-For 末尾取真实 IP，仅在你确信部署在可信反代
# （如 nginx/Caddy）后才启用，否则攻击者可伪造 XFF 绕过登录限流/白名单。
TRUST_FORWARDED_FOR = os.getenv("TRUST_FORWARDED_FOR", "").strip().lower() in ("1", "true", "yes", "on")

# ── 默认 Provider ──
# 初始值：环境变量 DEFAULT_PROVIDER > mimo（MiMo 是默认兜底）
# 运行时可通过 set_default_provider() 动态更新（Web UI 切换模型时调用）
DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "mimo").strip().lower()


def set_default_provider(provider: str) -> None:
    """运行时更新 DEFAULT_PROVIDER（Web UI 切换模型时调用）。

    同时更新模块级变量 DEFAULT_PROVIDER，使所有 import 了该变量的模块
    在下次读取时获得最新值。
    """
    global DEFAULT_PROVIDER
    DEFAULT_PROVIDER = provider.strip().lower()

# ── Provider → 默认模型映射 ──
# 当 MODEL_NAME 未在 .env 中显式设置时，根据 DEFAULT_PROVIDER 自动选择
_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "mimo": "mimo-v2.5",
    "siliconflow": "deepseek-ai/DeepSeek-V3-0324",
    "deepseek": "deepseek-chat",
    "agnes": "agnes-v1",
}
if os.getenv("MODEL_NAME"):
    MODEL_NAME = os.getenv("MODEL_NAME")
else:
    MODEL_NAME = _PROVIDER_DEFAULT_MODELS.get(DEFAULT_PROVIDER, "mimo-v2.5")
PRO_MODEL_NAME = os.getenv("PRO_MODEL_NAME", "")
FLASH_MODEL_NAME = os.getenv("FLASH_MODEL_NAME", "")


# Agnes AI 配置（在 get_provider_config 之前定义，避免前向引用）
AGNES_API_KEY = get_secret("AGNES_API_KEY", "")
AGNES_BASE_URL = os.getenv("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
AGNES_TEXT_MODEL = os.getenv("AGNES_TEXT_MODEL", "agnes-2.0-flash")
AGNES_IMAGE_MODEL = os.getenv("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash")
AGNES_VIDEO_MODEL = os.getenv("AGNES_VIDEO_MODEL", "agnes-video-v2.0")

# ── Provider 配置映射（base_url / api_key_env）──
# 子代理注册时根据 provider 自动选择正确的连接参数
def get_provider_config(provider: str) -> dict:
    """返回 provider 对应的 base_url 和 api_key_env。"""
    _PROVIDER_MAP = {
        "mimo": {"base_url": MIMO_BASE_URL, "api_key_env": "MIMO_API_KEY"},
        "siliconflow": {"base_url": "https://api.siliconflow.cn/v1", "api_key_env": "SILICONFLOW_API_KEY"},
        "deepseek": {"base_url": DEEPSEEK_BASE_URL, "api_key_env": "DEEPSEEK_API_KEY"},
        "agnes": {"base_url": AGNES_BASE_URL, "api_key_env": "AGNES_API_KEY"},
    }
    return _PROVIDER_MAP.get(provider, {"base_url": "", "api_key_env": ""})


# ── Agent display_name 动态读取（规避 IP 风险，用户可自定义）──
# 默认 display_name（当用户未自定义时的 fallback）
_DEFAULT_DISPLAY_NAMES: dict[str, str] = {
    "xiaoda": "小妲",
    "xiaoli": "小莉",
    "xiaolang": "小狼",
    "xiaolian": "小涟",
    "xiaoke": "小可",
}
_display_name_cache: dict[str, tuple[float, str]] = {}  # {name: (mtime, display_name)}


def clear_display_name_cache(name: str | None = None):
    """清除显示名缓存。

    当 display_name 变更时调用，确保下次读取时获取最新值。
    Args:
        name: 指定 agent 名称清除，None 则清除全部
    """
    if name:
        _display_name_cache.pop(name, None)
    else:
        _display_name_cache.clear()
    # 同时清除 prompt_builder 的模块缓存
    try:
        from prompt_builder import clear_module_cache
        clear_module_cache()
    except ImportError:
        pass


def agent_names() -> list[str]:
    """返回所有 agent key（通过扫描 config/agents/ 目录）。"""
    return [
        fp.stem for fp in AGENTS_CONFIG_DIR.glob("*.json")
        if fp.stem and not fp.stem.startswith("_")
    ]


def get_temperature(default: float = 0.7) -> float:
    """读取全局 temperature：优先 webui_overrides，回退 default。"""
    try:
        from web.config_service import get_config_service
        override = get_config_service().get("models.temperature")
        if override is not None:
            return float(override)
    except Exception:
        logger.debug("get_global_temperature.webui_read_failed")
    return default


def get_agent_display_name(name: str) -> str:
    """读取 agent 的 display_name（从 config/agents/{name}.json）。

    用于规避 IP 风险：发布版可改默认值为中性名，用户拿到后改回原名即可全局生效。
    带文件 mtime 缓存，避免频繁 IO。
    """
    if not name:
        return ""
    fp = AGENTS_CONFIG_DIR / f"{name}.json"
    default = _DEFAULT_DISPLAY_NAMES.get(name, name)
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return default
    cached = _display_name_cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        data = json.loads(fp.read_text(encoding="utf-8"))
        dn = data.get("display_name") or default
    except Exception:
        dn = default
    _display_name_cache[name] = (mtime, dn)
    return dn


# ── Agent 原名 → display_name 全局替换 ────────────────────────
# 每个 agent 的人格文件中使用原名，运行时自动替换为用户配置的显示名。
# 全局统一机制：所有 agent 共用一套替换逻辑，不分主次。
# 旧名映射从 config/agents/*.json 的 deprecated_names 字段读取，无需手动维护。

# 硬编码兜底（当配置文件缺失或无 deprecated_names 时使用）
_FALLBACK_DEPRECATED_NAMES: dict[str, str] = {
    "纳西妲": "xiaoda", "nahida": "xiaoda",
    "可莉": "xiaoli", "keli": "xiaoli",
    "银狼": "xiaolang", "yinlang": "xiaolang",
    "昔涟": "xiaolian", "xilian": "xiaolian",
    "尼可": "xiaoke", "nike": "xiaoke",
}

# 缓存: {agent_key: (mtime, deprecated_names_list)}
_deprecated_names_cache: dict[str, tuple[float, list[str]]] = {}


def get_agent_deprecated_names(agent_key: str) -> list[str]:
    """读取 agent 的旧名列表（从 config/agents/{name}.json 的 deprecated_names 字段）。"""
    fp = AGENTS_CONFIG_DIR / f"{agent_key}.json"
    try:
        mtime = fp.stat().st_mtime
    except OSError:
        return [k for k, v in _FALLBACK_DEPRECATED_NAMES.items() if v == agent_key]
    cached = _deprecated_names_cache.get(agent_key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        import json
        data = json.loads(fp.read_text(encoding="utf-8"))
        names = data.get("deprecated_names", [])
    except Exception:
        names = []
    if not names:
        names = [k for k, v in _FALLBACK_DEPRECATED_NAMES.items() if v == agent_key]
    _deprecated_names_cache[agent_key] = (mtime, names)
    return names


def get_all_deprecated_names() -> dict[str, str]:
    """返回所有旧名 → agent_key 的映射（从配置文件自动生成）。"""
    result: dict[str, str] = {}
    for key in agent_names():
        for old_name in get_agent_deprecated_names(key):
            result[old_name] = key
    return result


def apply_agent_name_replacements(content: str) -> str:
    """将人格文件中所有 agent 原名替换为 config 中的显示名。

    替换来源（优先级从高到低）：
    1. 配置文件 deprecated_names 字段（旧名）
    2. 当前 display_name（新名）
    3. agent key（如 xiaoda）
    按原名长度降序替换，避免短名破坏长名。
    """
    def _best(agent_key: str) -> str:
        return get_agent_display_name(agent_key) or agent_key

    # 1. 替换旧名（从配置文件读取）
    for old_name, agent_key in sorted(
        get_all_deprecated_names().items(), key=lambda x: -len(x[0])
    ):
        dn = _best(agent_key)
        if dn and dn != old_name:
            content = content.replace(old_name, dn)
    # 2. 替换当前 display_name（如用户改了显示名，旧人格文件中的新名也要同步）
    for agent_key in agent_names():
        dn = _best(agent_key)
        if dn and dn != agent_key:
            content = content.replace(agent_key, dn)
    return content


def reverse_agent_name_replacements(content: str) -> str:
    """将 display_name 还原为原名（用于编辑器保存时还原模板）。

    与 apply_agent_name_replacements 互为逆操作。
    还原中文显示名 → 原名、agent key → 原名。
    """
    def _best(agent_key: str) -> str:
        return get_agent_display_name(agent_key) or agent_key

    # 还原 agent key（必须先还原，因为显示名可能包含 agent key）
    for agent_key in agent_names():
        dn = _best(agent_key)
        if dn and dn != agent_key:
            content = content.replace(dn, agent_key)
    # 还原中文 display_name → 原名（使用配置文件中的第一个旧名）
    for agent_key in agent_names():
        dn = _best(agent_key)
        deprecated = get_agent_deprecated_names(agent_key)
        if dn and deprecated:
            # 还原到第一个中文旧名（优先）
            cn_names = [n for n in deprecated if any('\u4e00' <= c <= '\u9fff' for c in n)]
            target = cn_names[0] if cn_names else deprecated[0]
            content = content.replace(dn, target)
    return content


# ── ASR 语音识别配置 ──
ASR_API_KEY = get_secret("ASR_API_KEY", "") or get_secret("SILICONFLOW_API_KEY", "")
ASR_BASE_URL = os.getenv("ASR_BASE_URL", "https://api.siliconflow.cn/v1")
ASR_MODEL = os.getenv("ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")

# Jina Reader API key（可选）：有则 500 RPM，无则免费 20 RPM
JINA_API_KEY = get_secret("JINA_API_KEY", "")


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
    return re.sub(r',\s*([}\]])', r'\1', cleaned)


def load_agent_config() -> dict:
    """加载并解析 agent 配置文件（JSON5 风格，自动去除注释）。"""
    if not AGENT_CONFIG_PATH.exists():
        return {}
    raw = AGENT_CONFIG_PATH.read_text(encoding="utf-8")
    cleaned = _strip_json5_comments(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        logger.warning("config.agent_json5_parse_failed", error=str(e))
        return {}


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
        # 记忆/回顾类：明确的回忆意图，需要检索长期记忆，不能走 fast_path
        "回忆", "还记得", "还记得吗", "记不记得", "记得", "记住",
        "上次我们", "上次聊", "上次说", "之前我们", "之前聊", "之前说",
        "早上", "上午", "下午", "晚上", "昨天", "前天", "上周", "上个月",
        "几号", "哪天", "什么时候", "那天", "那天",
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
    "xiaolian": [
        "搜索", "搜一下", "查一下", "找一下", "帮我查", "帮我搜", "搜索一下",
        "查资料", "最新", "新闻", "资讯", "获取网上", "看看有没有",
        "板块", "盘整", "入场", "股票", "基金", "行情", "大盘", "涨跌",
        "市值", "财经", "证券", "a股", "港股", "美股", "币圈", "加密货币",
        "走势", "k线", "技术分析", "基本面", "财报", "市盈率",
    ],
    "xiaolang": [
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
    "xiaoke": [
        "研究", "分析", "学术", "论文", "深度", "计算复杂度", "数学证明",
        "物理", "化学", "生物", "统计", "推导", "公式",
    ],
    "xiaoda": [
        "天气", "气温", "温度", "下雨", "晴天", "阴天",
        "时间", "几点", "现在几点", "日期", "今天星期几",
        "翻译", "意思是什么",
        "语音", "声音", "说话", "朗读", "念给我", "读给我", "听你", "听听", "发语音", "生成语音", "语音回复", "说给我听", "念出来", "tts", "voice",
        "技能", "能力", "功能", "你会什么", "你能做什么", "你有什么", "列出技能", "列出功能",
        "画", "生成图", "生成图片", "画一张", "画个", "画一个", "图片生成", "做视频", "生成视频",
        "表情包", "贴纸",
        "回忆", "记得", "记忆", "recall", "remember", "记得吗", "上次", "昨天", "前几天", "上周",
    ],
    "parallel_trigger": [
        "全面", "整体", "综合", "各个方面", "多方面", "同时",
        "全部", "一起", "都检查", "都搜一下", "分别",
        "全方位", "彻底", "完整", "所有", "各个板块",
        "巡检", "体检", "诊断", "健康检查", "状况报告",
    ],
}

# ── 子代理任务类型映射（EnhancedBeliefRouter 使用） ──
AGENT_TASK_MAP = {
    "xiaolang": "debug",
    "xiaoke": "research",
    "xiaolian": "info_search",
    "xiaoda": "memory",
}

# ── RAG 优化配置（SiliconFlow 免费常驻） ──
RERANKER_API_KEY = get_secret("RERANKER_API_KEY", "")
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")


from utils.common import safe_int as _safe_int


def _safe_float(env_val: str | None, default: float) -> float:
    """安全解析浮点数环境变量, 非法值回退到 default."""
    if env_val is None:
        return default
    try:
        return float(env_val)
    except (ValueError, TypeError):
        return default


RERANKER_OVERSAMPLE_RATIO = _safe_int(os.getenv("RERANKER_OVERSAMPLE_RATIO"), 3)

# Query Transform
QUERY_TRANSFORM_ENABLED = os.getenv("QUERY_TRANSFORM_ENABLED", "true").lower() in ("1", "true", "yes")
QUERY_EXPAND_COUNT = _safe_int(os.getenv("QUERY_EXPAND_COUNT"), 2)
# 意图分类 LLM 调用：默认开启（GLM-Z1-9B-0414 推理质量高，速度可接受）
# 设置 INTENT_LLM_CLASSIFY=false 可关闭 LLM 分类，仅用规则匹配（更快）
INTENT_LLM_CLASSIFY = os.getenv("INTENT_LLM_CLASSIFY", "false").lower() in ("1", "true", "yes")
# 意图分类 LLM 调用超时（秒），默认 5.0s（从 2.0s 提升，避免误超时）
INTENT_CLASSIFY_TIMEOUT = _safe_float(os.getenv("INTENT_CLASSIFY_TIMEOUT"), 15.0)

# Retrieval Optimization (A1/A2/A3)
RETRIEVAL_SMART_SKIP = os.getenv("RETRIEVAL_SMART_SKIP", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_TRANSFORM = os.getenv("RETRIEVAL_PARALLEL_TRANSFORM", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_SEARCH = os.getenv("RETRIEVAL_PARALLEL_SEARCH", "true").lower() in ("1", "true", "yes")
# 查询语义缓存开关：命中缓存时跳过完整检索流水线
QUERY_CACHE_ENABLED = os.getenv("QUERY_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
# P3-9: 查询缓存参数配置化（之前硬编码在 QueryCache 默认参数中，无法运行时调节）
# threshold: 余弦相似度阈值，>= 此值视为命中（0.88 严格匹配，避免误命中返回无关记忆）
# max_size: LRU 最大条目数（256 足够覆盖活跃话题，过大占用内存）
# ttl: 缓存过期时间秒（300s = 5 分钟，与 kg query_entity_cache 对齐）
QUERY_CACHE_THRESHOLD = float(os.getenv("QUERY_CACHE_THRESHOLD", "0.88"))
QUERY_CACHE_MAX_SIZE = _safe_int(os.getenv("QUERY_CACHE_MAX_SIZE"), 256)
QUERY_CACHE_TTL = _safe_int(os.getenv("QUERY_CACHE_TTL"), 300)

# ── 父子Chunk RAG 优化 ──
PARENT_CHILD_CHUNK_ENABLED = os.getenv("PARENT_CHILD_CHUNK_ENABLED", "true").lower() in ("1", "true", "yes")
# ── KG v2 知识图谱优化 ──
KG_V2_ENABLED = os.getenv("KG_V2_ENABLED", "false").lower() in ("1", "true", "yes")
CONTEXTUAL_RETRIEVAL_ENABLED = os.getenv("CONTEXTUAL_RETRIEVAL_ENABLED", "true").lower() in ("1", "true", "yes")
CHILD_CHUNK_OVERLAP_CHARS = _safe_int(os.getenv("CHILD_CHUNK_OVERLAP_CHARS"), 30)
CHILD_CHUNK_MAX_PER_PARENT = _safe_int(os.getenv("CHILD_CHUNK_MAX_PER_PARENT"), 10)
CHILD_CHUNK_SEGMENT_MAX_LEN = _safe_int(os.getenv("CHILD_CHUNK_SEGMENT_MAX_LEN"), 200)
CHILD_VEC_TABLE = "memories_child_vec"
CHILD_CHUNK_TYPES = ["segment", "entity", "decision", "topic"]

# ── 子Agent LLM调用超时配置 ──
# 单次LLM API调用超时(秒); 网络抖动时会重试一次(用半超时值)
SUB_AGENT_API_TIMEOUT = _safe_int(os.getenv("SUB_AGENT_API_TIMEOUT"), 60)
# 整个对话循环(多轮工具调用)总超时(秒)
SUB_AGENT_TOTAL_TIMEOUT = _safe_int(os.getenv("SUB_AGENT_TOTAL_TIMEOUT"), 150)
# LLM调用超时后重试次数(0=不重试, 1=重试1次用半超时)
SUB_AGENT_API_RETRY = _safe_int(os.getenv("SUB_AGENT_API_RETRY"), 1)

# ── 性能优化开关 ──────────────────────────────────────────────
# Task 6: TTS 异步化（方案 B）—— 开启后 TTS 在后台合成，先返回文字回复
TTS_ASYNC_MODE = os.getenv("TTS_ASYNC_MODE", "true").lower() in ("1", "true", "yes")
# Task 7: 流式中间状态推送（方案 C1）—— 开启后推送细粒度思考状态
STREAM_STATUS_PUSH = os.getenv("STREAM_STATUS_PUSH", "false").lower() in ("1", "true", "yes")
# Task 9: 简单对话快速路径（方案 E）—— 开启后简单闲聊跳过记忆检索
SIMPLE_CHAT_FASTPATH = os.getenv("SIMPLE_CHAT_FASTPATH", "true").lower() in ("1", "true", "yes")

# P0: WebSocket 流式文本推送 —— LLM 流式调用 + 逐 token 推送
STREAM_TEXT_PUSH = os.getenv("STREAM_TEXT_PUSH", "true").lower() in ("1", "true", "yes")
# P0: 工具调用中间状态推送（started/completed/failed）
STREAM_TOOL_STATUS = os.getenv("STREAM_TOOL_STATUS", "true").lower() in ("1", "true", "yes")

# Task 12: 熔断器智能恢复配置（P2）
# COOLDOWN 从 60→30：熔断后恢复更快，避免长时间快速失败拖累用户体验
CIRCUIT_BREAKER_COOLDOWN = _safe_int(os.getenv("CIRCUIT_BREAKER_COOLDOWN"), 30)
CIRCUIT_BREAKER_HALF_OPEN_PROBES = _safe_int(os.getenv("CIRCUIT_BREAKER_HALF_OPEN_PROBES"), 2)
CIRCUIT_BREAKER_MAX_COOLDOWN = _safe_int(os.getenv("CIRCUIT_BREAKER_MAX_COOLDOWN"), 300)

# P5: 失败经验→规则闭环 —— 命中规则时是否拒绝调用（true=拒绝，false=仅记录警告日志）
ERROR_RULE_STRICT_MODE = os.getenv("ERROR_RULE_STRICT_MODE", "true").lower() in ("1", "true", "yes")

# P6: 增量上下文构建与 Prompt Caching —— 开启后拆分系统提示稳定段/动态段并标记缓存
PROMPT_CACHING_ENABLED = os.getenv("PROMPT_CACHING_ENABLED", "false").lower() in ("1", "true", "yes")

# RAG Fusion Weights
RAG_RERANK_WEIGHT = _safe_float(os.getenv("RAG_RERANK_WEIGHT"), 0.65)
RAG_KG_WEIGHT = _safe_float(os.getenv("RAG_KG_WEIGHT"), 0.15)
RAG_IMPORTANCE_WEIGHT = _safe_float(os.getenv("RAG_IMPORTANCE_WEIGHT"), 0.20)

# RAG 候选集大小（每路召回 Top-N，RRF 融合后送 Reranker 的数量）
RAG_RECALL_LIMIT = _safe_int(os.getenv("RAG_RECALL_LIMIT"), 50)
RAG_RERANK_LIMIT = _safe_int(os.getenv("RAG_RERANK_LIMIT"), 50)

# ── 记忆/情绪阈值 (可环境变量覆盖) ──
# 情绪触发安慰记忆检索的强度阈值 (0.0~1.0)
EMOTION_TRIGGER_THRESHOLD = _safe_float(os.getenv("EMOTION_TRIGGER_THRESHOLD"), 0.5)
# B 级场景粘性阈值: 低于此权重时不重排, 防止低质量闲聊触发重排
SCENE_STICKINESS_THRESHOLD = _safe_float(os.getenv("SCENE_STICKINESS_THRESHOLD"), 0.5)

# ── 冷启动路由配置 (环境变量覆盖) ──
# 私有记忆条数: < COLD_MAX 为冷用户(纯FTS), COLD_MAX~WARM_MAX 为温用户(向量低权重), >= WARM_MAX 为热用户(均衡混合)
MEMORY_COLD_MAX = _safe_int(os.getenv("MEMORY_COLD_MAX"), 0)
MEMORY_WARM_MAX = _safe_int(os.getenv("MEMORY_WARM_MAX"), 10)
# 温用户向量融合权重 (0.0~1.0): 冷=0.0, 温=0.2, 热=0.5(均衡)
MEMORY_WARM_VEC_WEIGHT = _safe_float(os.getenv("MEMORY_WARM_VEC_WEIGHT"), 0.2)

# ── P3 记忆蒸馏压缩配置 ──
MAX_EPISODIC_MEMORIES = _safe_int(os.getenv("MAX_EPISODIC_MEMORIES"), 200)
MEMORY_DISTILL_BATCH = _safe_int(os.getenv("MEMORY_DISTILL_BATCH"), 30)
MEMORY_DISTILL_ENABLED = os.getenv("MEMORY_DISTILL_ENABLED", "false").lower() in ("1", "true", "yes")

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
        "agents": ["xiaolang"],  # which agents can use this MCP server's tools
    },
    "github": {
        "command": _resolve_command("npx"),
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": get_secret("GITHUB_PERSONAL_ACCESS_TOKEN", "")},
        "agents": ["xiaolang"],
    },
}

__all__ = [
    "AGENTS_CONFIG_DIR",
    "AGENT_CONFIG",
    "AGENT_ROUTE_KEYWORDS",
    "AGENT_STICKER_BASE",
    "AGENT_TASK_MAP",
    "AGNES_API_KEY",
    "AGNES_BASE_URL",
    "AGNES_IMAGE_MODEL",
    "AGNES_TEXT_MODEL",
    "AGNES_VIDEO_MODEL",
    "ASR_API_KEY",
    "ASR_BASE_URL",
    "ASR_MODEL",
    "CIRCUIT_BREAKER_COOLDOWN",
    "CIRCUIT_BREAKER_HALF_OPEN_PROBES",
    "CIRCUIT_BREAKER_MAX_COOLDOWN",
    "CREDENTIALS_DIR",
    "DATA_DIR",
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "ERROR_RULE_STRICT_MODE",
    "FILE_DIR",
    "JINA_API_KEY",
    "LOG_DIR",
    "MAX_EPISODIC_MEMORIES",
    "MCP_SERVERS",
    "MEDIA_DIR",
    "MEMORY_COLD_MAX",
    "MEMORY_DISTILL_BATCH",
    "MEMORY_DISTILL_ENABLED",
    "MEMORY_STATE_DIR",
    "MEMORY_WARM_MAX",
    "MEMORY_WARM_VEC_WEIGHT",
    "MODEL_NAME",
    "PLUGINS_CONFIG_DIR",
    "PROMPT_CACHING_ENABLED",
    "PRO_TASK_KEYWORDS",
    "QUERY_EXPAND_COUNT",
    "QUERY_TRANSFORM_ENABLED",
    "INTENT_LLM_CLASSIFY",
    "INTENT_CLASSIFY_TIMEOUT",
    "RAG_IMPORTANCE_WEIGHT",
    "RAG_KG_WEIGHT",
    "RAG_RECALL_LIMIT",
    "RAG_RERANK_LIMIT",
    "RAG_RERANK_WEIGHT",
    "RERANKER_API_KEY",
    "RERANKER_BASE_URL",
    "RERANKER_ENABLED",
    "RERANKER_MODEL",
    "RERANKER_OVERSAMPLE_RATIO",
    "RETRIEVAL_PARALLEL_SEARCH",
    "RETRIEVAL_PARALLEL_TRANSFORM",
    "RETRIEVAL_SMART_SKIP",
    "QUERY_CACHE_ENABLED",
    "QUERY_CACHE_THRESHOLD",
    "QUERY_CACHE_MAX_SIZE",
    "QUERY_CACHE_TTL",
    "SIMPLE_CHAT_FASTPATH",
    "SIMPLE_TASK_KEYWORDS",
    "STICKER_DIR",
    "STREAM_STATUS_PUSH",
    "STREAM_TEXT_PUSH",
    "STREAM_TOOL_STATUS",
    "SUB_AGENT_API_RETRY",
    "SUB_AGENT_API_TIMEOUT",
    "SUB_AGENT_TOTAL_TIMEOUT",
    "TTS_ASYNC_MODE",
    "VOICE_REF_DIR",
    "WORKSPACE_DIR",
    "XIAOLI_STICKER_DIR",
    "agent_names",
    "get_agent_display_name",
    "get_base_dir",
    "get_config_dir",
    "get_credentials_dir",
    "load_agent_config",
]

# ── 向后兼容：从 prompt_builder 重新导出已拆分的函数 ──────────
# 使用 PEP 562 模块级 __getattr__ 延迟导入, 彻底打破 config <-> prompt_builder 循环.
# 此前 `from prompt_builder import *` 是顶层 import, 会立即触发 prompt_builder 加载,
# 而 prompt_builder 在调用时 (函数内) 又会回头读 config 常量 — 虽然 prompt_builder
# 已用函数内延迟导入规避了运行时崩溃, 但顶层 `from prompt_builder import *` 仍会在
# 静态分析层面形成循环. 改为 __getattr__ 后, 只有实际访问这些名称时才触发导入.
_PROMPT_BUILDER_REEXPORTS = frozenset({
    "build_system_prompt",
    "build_safe_system_prompt",
    "build_scene_aware_prompt",
    "load_workspace_file",
    "load_skills",
    "_ensure_workspace_template",
    "_detect_device_info",
    "_get_workspace_mtimes",
    "_strip_owner_references",
    "_build_stable_prompt",
    "_build_dynamic_prompt",
    "_classify_scene",
    "_canary_manager",
})


def __getattr__(name: str) -> Any:
    """模块级 __getattr__ — 从 prompt_builder 延迟导入, 避免循环导入.

    只有访问 _PROMPT_BUILDER_REEXPORTS 中的名称时才触发 prompt_builder 加载.
    首次访问后将结果缓存到 globals(), 后续直接命中, 无 import 开销.
    """
    if name in _PROMPT_BUILDER_REEXPORTS:
        from importlib import import_module
        _pb = import_module("prompt_builder")
        value = getattr(_pb, name)
        globals()[name] = value  # 缓存, 下次直接访问
        return value
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# ── J-Space 架构优化配置 ──────────────────────────────────────
ENABLE_J_SPACE_HOOKS = os.getenv("ENABLE_J_SPACE_HOOKS", "true").lower() == "true"
DIRECTION_REGISTRY_PATH = os.getenv("DIRECTION_REGISTRY_PATH", str(DATA_DIR / "direction_registry.json"))
SIGNAL_STREAM_MAX_HISTORY = _safe_int(os.getenv("SIGNAL_STREAM_MAX_HISTORY"), 1000)
INTERVENTION_DEFAULT_COOLDOWN = _safe_float(os.getenv("INTERVENTION_DEFAULT_COOLDOWN"), 30.0)