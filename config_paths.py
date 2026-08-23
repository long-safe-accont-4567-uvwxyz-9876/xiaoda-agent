"""config.py 的路径与 workspace 引导块 — 自 config.py 拆分（上帝文件 Phase 1）。

内容：项目根/ENV_PATH 解析与 dotenv 加载、KIOXIA 外置盘数据路径解析与
fallback、全部目录常量（DATA_DIR/LOG_DIR/CONFIG_DIR/WORKSPACE_DIR/…）、
冻结模式（PyInstaller）打包资源复制、旧数据迁移、_ensure_workspace 惰性
初始化。函数体自 config.py 逐字节搬移，仅缩进调整。

兼容契约（tests/test_config_paths_module.py）：
    - 本模块不得 import config（防循环依赖）
    - config 同名 re-export，`from config import DATA_DIR / get_config_dir`
      等既有用法不受影响
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv
from loguru import logger


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
                    logger.debug("config.env_migrate_failed: {}", e)
        return user_env
    # 开发模式：使用项目根目录
    return Path(__file__).resolve().parent / ".env"


ENV_PATH = get_env_path()
# 关键（2026-08-19）：override=False —— .env 仅作为默认值填充，不覆盖已存在的
# 系统/命令行环境变量。此前 override=True 会用项目根 .env 里写死的
# KIOXIA_DATA_DIR=/mnt/usb2/nahida-data 强制覆盖外部环境变量，导致换机器部署时
# 无法用环境变量指定数据盘，且本机 U 盘路径被锁死。改为 False 后：系统环境变量
# 优先，.env 仅兜底（未设环境变量时才生效），兼顾本机特殊 U 盘与可移植部署。
# 全项目唯一的 .env 基准加载点（策略：override=False，进程环境变量优先）。
# agent.py / qq_bot_adapter / cli.py 的同名调用均已对齐此策略；override=True
# 仅允许出现在"用户动作后的显式重载点"（setup 向导完成/凭证保存后刷新）。
load_dotenv(ENV_PATH, override=False)

# 确保 PyInstaller 打包后 HTTPS 请求能找到 CA 证书
# certifi 的 cacert.pem 必须被正确打包，否则所有 API 请求都会因 SSL 错误失败
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    logger.debug("config.certifi_import_unavailable", exc_info=True)

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


def _migrate_old_data(old_dir: Path, new_dir: Path, name: str, ignore_names: tuple[str, ...] = ()) -> None:
    """将旧目录的数据迁移到新目录（仅首次）。
    用于从 exe 目录迁移到用户目录，解决更新安装包导致数据丢失的问题。
    """
    if new_dir.exists():
        # 忽略新目录中已存在（且属于另一迁移目标）的子目录后再判空。
        # 例：WORKSPACE_DIR 顶层 mkdir 会预先创建 CONFIG_DIR/workspace，
        # 若直接判空会误判 CONFIG_DIR 非空而跳过 config 迁移（数据丢失）。
        entries = [e for e in new_dir.iterdir() if e.name not in ignore_names]
        if entries:
            return  # 新目录已有数据，跳过
    if not old_dir.exists() or not any(old_dir.iterdir()):
        return  # 旧目录无数据，跳过
    try:
        shutil.copytree(old_dir, new_dir, dirs_exist_ok=True)
        print(f"[config] {name} migrated from {old_dir} to {new_dir}")
    except Exception as e:
        logger.warning("config.migrate_failed name={} error={}", name, e)


def _merge_dir(old_dir: Path, new_dir: Path, name: str) -> None:
    """按子项合并旧目录数据到新目录（幂等）。

    与 _migrate_old_data 的区别：后者要求新目录整体为空才迁移，若目标目录
    已有旧内容（如 ~/.ai-agent/voice_refs 遗留 keli/nahida），旧目录独有的
    子项（如 U 盘的 xiaoda/xiaoli）会被整体跳过而永久丢失。
    本函数逐个子项复制缺失项，两种来源的内容能合并共存。
    """
    try:
        if not old_dir.exists() or not any(old_dir.iterdir()):
            return  # 旧目录无数据，跳过
        new_dir.mkdir(parents=True, exist_ok=True)
        _copied = 0
        for item in old_dir.iterdir():
            target = new_dir / item.name
            if target.exists():
                continue  # 目标已有同名项，保留（不覆盖用户新数据）
            if item.is_dir():
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)
            _copied += 1
        if _copied:
            print(f"[config] {name} merged {_copied} items from {old_dir} to {new_dir}")
    except Exception as e:
        logger.warning("config.merge_failed name={} error={}", name, e)

def get_credentials_dir() -> Path:
    """获取凭证目录（固定系统盘 ~/.ai-agent/credentials）。

    6c11c84 迁移遗漏修复：凭证（provider API Key / webui_secret / revoked_tokens）
    每次 API 调用与登录鉴权都会热读，且 U 盘拔出即丢失全部 Key 与登录态。
    原实现优先 KIOXIA（_KIOXIA_BASE/credentials），违背"仅数据库用 U 盘"政策，
    改为固定系统盘；旧 U 盘/旧默认位置数据由 _ensure_workspace 幂等迁移一次。
    """
    cred_dir = Path.home() / ".ai-agent" / "credentials"
    try:
        cred_dir.mkdir(parents=True, exist_ok=True)
        return cred_dir
    except (OSError, PermissionError):
        logger.debug("config.credentials_dir_setup_failed", exc_info=True)
        import tempfile
        cred_dir = Path(tempfile.gettempdir()) / "xiaoda-agent" / "credentials"
        cred_dir.mkdir(parents=True, exist_ok=True)
        return cred_dir

def get_config_dir() -> Path:
    """获取配置目录（用于 provider_metadata.json、webui_overrides.json 等可写配置）。

    统一返回 CONFIG_DIR，与 agent.json5 / AGENTS_CONFIG_DIR 同源，避免 U 盘
    生效时 get_config_dir（旧实现硬编码 ~/.ai-agent/config）与 CONFIG_DIR
    （KIOXIA 优先）拆到两个位置。frozen 下若旧安装目录有配置而 CONFIG_DIR
    为空，则迁移到 CONFIG_DIR。
    """
    if getattr(sys, 'frozen', False):
        # 迁移：旧安装目录（含更新前写入的用户配置）有文件而 CONFIG_DIR 尚无
        # agent.json5 时复制，避免"更新安装包丢配置"。
        old_config = get_base_dir() / "config"
        if old_config.exists() and any(old_config.iterdir()):
            try:
                if not (CONFIG_DIR / "agent.json5").exists():
                    shutil.copytree(old_config, CONFIG_DIR, dirs_exist_ok=True)
                    logger.debug("config.dir_migrated_to={}", CONFIG_DIR)
            except (OSError, shutil.Error) as e:
                logger.debug("config.dir_migrate_failed: {}", e)
    return CONFIG_DIR

def _resolve_data_path(kioxia_path: Path, fallback_path: Path) -> Path:
    """解析数据路径，优先使用 KIOXIA 外置存储，失败时降级到 fallback。

    规则：
    - 显式设置 KIOXIA_DATA_DIR 时：仅当外置盘已挂载（base 目录存在）才使用，
      否则回退到 fallback。
    - 未设置 KIOXIA_DATA_DIR 时：_KIOXIA_BASE 默认即 ~/.ai-agent/data，
      作为数据根直接使用 kioxia_path（~/.ai-agent/data/<sub>），位置稳定，
      与更新脚本的备份清单（~/.ai-agent\\data）一致，避免数据被备份遗漏。

    修复：原逻辑用 kioxia_path.parent.exists() 判断，未设 env 时默认
    _KIOXIA_BASE=~/.ai-agent/data，会因 ~/.ai-agent/data 是否恰好存在而在
    ~/.ai-agent/data/<sub> 与 ~/.ai-agent/<sub> 之间翻转，导致数据孤立。

    注意：fallback_path 必须与 kioxia_path 结构一致（如都是 .../db）。
    """
    kioxia_env = os.getenv("KIOXIA_DATA_DIR", "")
    if kioxia_env:
        # 显式配置外置盘：盘未挂载（base 目录不存在）则直接回退，不创建幻影目录
        if not (kioxia_path.exists() or kioxia_path.parent.exists()):
            # 静默回退，不向控制台打印警告：外置盘未挂载是常见状态，
            # 每次启动刷屏会让用户误以为出错。仅记 debug 日志便于排查。
            logger.debug(
                "config.data_path_unavailable kioxia_env=%s fallback=%s",
                kioxia_env, fallback_path,
            )
            return _ensure_fallback(fallback_path)
    try:
        kioxia_path.mkdir(parents=True, exist_ok=True)
        # 运行时只读检测：尝试写入临时文件并读回，验证文件系统是否可写。
        # 修复：FAT 文件系统错误导致 remount 只读时，os.access(W_OK) 仍返回 True，
        # 因此需要实际写入测试文件。
        # 关键修复（2026-08-19）：探针删除（unlink）可能被外部 safe-delete 拦截器
        # 拦截（如 CodeBuddy 的 genie-trash 会尝试移到 /mnt/usb2/.Trash-1000，而该
        # 回收站目录权限拒绝 → 抛 OSError）。此异常与"文件系统只读"无关，绝不能因此
        # 误判外置盘不可写而回退 fallback（曾导致 U 盘向量库/媒体目录全部静默回退到
        # 系统盘，用户数据失效）。判定可写性只看"能写入 + 能读回"，删除探针失败忽略。
        _probe = kioxia_path / ".write_probe"
        try:
            _probe.write_text("probe", encoding="utf-8")
            # 读回验证写入确实落盘（区分真正的只读 FS：写入不报错但读回为空/旧内容）
            if _probe.read_text(encoding="utf-8") != "probe":
                raise OSError("write not persisted")
            logger.debug("config.data_path_writable path={}", kioxia_path)
        except (OSError, PermissionError):
            logger.warning("config.data_path_readonly path={}", kioxia_path)
            raise OSError(f"Filesystem is read-only: {kioxia_path}") from None
        finally:
            # 删除探针：即便被 safe-delete 拦截抛错也忽略，不影响可写性结论
            try:
                _probe.unlink(missing_ok=True)
            except (OSError, PermissionError):
                logger.debug("config.data_path_probe_cleanup_skipped path={}", kioxia_path)
        return kioxia_path
    except (OSError, PermissionError):
        logger.debug("config.data_path_resolve_failed", exc_info=True)
    # 主路径不可用，回退到 fallback
    return _ensure_fallback(fallback_path)


def _ensure_fallback(fallback_path: Path) -> Path:
    """确保 fallback 目录存在并可写，连 fallback 都失败时使用临时目录。"""
    try:
        fallback_path.mkdir(parents=True, exist_ok=True)
    except (OSError, PermissionError):
        import tempfile
        fallback_path = Path(tempfile.gettempdir()) / "xiaoda-agent" / fallback_path.name
        fallback_path.mkdir(parents=True, exist_ok=True)
    return fallback_path

DATA_DIR = _resolve_data_path(_KIOXIA_BASE / "db", _FALLBACK_BASE / "db")
LOG_DIR = _resolve_data_path(_KIOXIA_BASE / "logs", _FALLBACK_BASE / "logs")
# 是否实际使用外置盘：仅当显式配置 KIOXIA_DATA_DIR 且 DATA_DIR 落在其上时。
# 原在 _ensure_workspace 内经 global 回写（2026-08-22 收口后改为模块级纯计算，
# 消除对初始化调用时序的隐式依赖）。修复：原 (_KIOXIA_BASE/"db").exists() 在
# 未设 KIOXIA_DATA_DIR 时查询 ~/.ai-agent/data/db，与 DATA_DIR 实际位置可能矛盾。
_KIOXIA_AVAILABLE = bool(os.getenv("KIOXIA_DATA_DIR", "")) and (
    DATA_DIR == _KIOXIA_BASE / "db"
)
# MD 文件（workspace）固定存放到系统盘用户目录，不再随 KIOXIA_DATA_DIR 走：
# USER.md/MEMORY.md/HEARTBEAT.md 每次请求都会读取注入提示词，放 U 盘会拖慢响应。
# 数据库（DATA_DIR）仍按 KIOXIA_DATA_DIR 解析——只有数据库用 U 盘。
# 所有模式（dev/frozen/远程）统一该路径，与项目根 config/workspace（git 模板源）分离。
WORKSPACE_DIR = Path.home() / ".ai-agent" / "config" / "workspace"
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

    # 使用统一的 CONFIG_DIR（与 AGENT_CONFIG_PATH / AGENTS_CONFIG_DIR 同源），
    # 确保 frozen 模式下配置写入和读取路径一致（Qodo 审查发现）
    user_config_dir = CONFIG_DIR

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
        except (OSError, shutil.Error) as e:
            print(f"[config] Warning: failed to copy agent.json5: {e}")
        except Exception as e:
            logger.exception("config.agent_json5_copy_unexpected")
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
                    except (OSError, shutil.Error) as e:
                        print(f"[config] Warning: failed to copy {item.name}: {e}")
                    except Exception as e:
                        logger.exception("config.agent_file_copy_unexpected name={}", item.name)
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
                except (OSError, PermissionError) as e:
                    print(f"[config] Warning: failed to remove {old_file}: {e}")
                except Exception as e:
                    logger.exception("config.agent_deprecated_remove_unexpected name={}", old_file)
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
        # SOUL.md 是用户人格文件，永不自动覆盖：
        #   - 已存在：无论内容（含旧名 nahida/纳西妲）都不覆盖，
        #     防止升级/部署流程用模板覆盖用户调教好的人格（曾导致人格漂移事故）
        #   - 缺失：仅全新安装（workspace 目录完全为空）时用模板兜底，否则警告跳过
        should_copy = target_name in _force_update_files or not target.exists()
        if target_name == "SOUL.md":
            if target.exists():
                should_copy = False
            else:
                try:
                    workspace_empty = not any(WORKSPACE_DIR.iterdir())
                except OSError:
                    workspace_empty = True
                if workspace_empty:
                    should_copy = True
                else:
                    logger.warning("config.soul_md_missing_skipped")
        if should_copy:
            try:
                shutil.copy2(item, target)
            except (OSError, shutil.Error) as e:
                logger.debug("config.workspace_copy_failed {}: {}", target_name, e)

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
                    logger.debug("config.workspace_sub_copy_failed {}: {}", item.name, e)


_workspace_initialized = False


def _ensure_workspace() -> None:
    """惰性初始化：frozen 模式下复制打包资源、迁移旧数据。

    仅在首次显式调用时执行一次，避免模块导入时产生 IO 副作用。
    """
    global _workspace_initialized
    if _workspace_initialized:
        return
    _workspace_initialized = True

    _init_user_resources()

    # workspace 数据目录迁移：升级前 WORKSPACE_DIR 跟随 KIOXIA_DATA_DIR 解析，
    # 未设 KIOXIA_DATA_DIR 时落在 ~/.ai-agent/data/config/workspace；现在固定到
    # ~/.ai-agent/config/workspace，旧数据需迁移（新目录为空时才会执行，安全幂等）。
    _migrate_old_data(
        Path(os.path.expanduser("~/.ai-agent/data/config/workspace")),
        WORKSPACE_DIR,
        "workspace",
    )

    # 配置目录迁移：升级前 CONFIG_DIR 跟随 KIOXIA_DATA_DIR（未设时 ~/.ai-agent/data/config）；
    # 现在固定到 ~/.ai-agent/config，旧配置需迁移（新目录为空时才会执行）。
    # ignore workspace：WORKSPACE_DIR 顶层 mkdir 已创建 CONFIG_DIR/workspace，
    # 需忽略该子目录后再判空，否则 agent.json5/agents 等旧配置永远不会迁移。
    _migrate_old_data(
        Path(os.path.expanduser("~/.ai-agent/data/config")),
        CONFIG_DIR,
        "config",
        ignore_names=("workspace",),
    )

    # ── 6c11c84 迁移遗漏补全：小体积热路径目录从 U 盘/旧默认位置迁到系统盘 ──
    # 升级前 STICKER_DIR/VOICE_REF_DIR/MEMORY_STATE_DIR/PLUGINS_CONFIG_DIR/CREDENTIALS
    # 跟随 KIOXIA_DATA_DIR（U 盘）或旧默认 ~/.ai-agent/data；现在固定系统盘。
    # _KIOXIA_BASE 未设 env 时即 ~/.ai-agent/data，两种来源一并覆盖。
    # 用"按子项合并"而非整体跳过：目标目录可能已有旧内容（如 ~/.ai-agent/voice_refs
    # 遗留 keli/nahida），整体跳过会导致 U 盘独有子目录（xiaoda/xiaoli）永不迁移。
    for _sub, _name in (
        ("credentials", "credentials"),
        ("stickers", "stickers"),
        ("xiaoli-stickers", "xiaoli-stickers"),
        ("agent-stickers", "agent-stickers"),
        ("voice_refs", "voice_refs"),
        ("memory_state", "memory_state"),
        ("plugins", "plugins"),
    ):
        _merge_dir(_KIOXIA_BASE / _sub, Path.home() / ".ai-agent" / _name, _name)

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
        # 旧版（v0.5.5x 静态资源架构）壁纸存放在 exe 目录 web/dist/assets/wallpapers/，
        # 新版改为用户数据目录 MEDIA_DIR/wallpapers/（避免安装包覆盖/升级丢失）。
        # 新目录已有壁纸（非空）时自动跳过，不覆盖用户自定义壁纸。
        _migrate_old_data(
            _exe_base / "web" / "dist" / "assets" / "wallpapers",
            MEDIA_DIR / "wallpapers",
            "wallpapers",
        )
        _migrate_old_data(_exe_base / "voice_refs", VOICE_REF_DIR, "voice_refs")
        _migrate_old_data(_exe_base / "memory_state", MEMORY_STATE_DIR, "memory_state")
        _migrate_old_data(_exe_base / "plugins", PLUGINS_CONFIG_DIR, "plugins")

    # 是否实际使用外置盘：仅当显式配置 KIOXIA_DATA_DIR 且 DATA_DIR 落在其上时。
    # 修复：原 (_KIOXIA_BASE/"db").exists() 在未设 KIOXIA_DATA_DIR 时查询
    # ~/.ai-agent/data/db，与 DATA_DIR 实际位置可能矛盾。


# 路径定义必须在 _ensure_workspace() 之前：迁移逻辑引用这些变量
# 统一 CONFIG_DIR：初始化写入、AGENT_CONFIG_PATH 读取、AGENTS_CONFIG_DIR 都从此派生，
# 确保 KIOXIA 只读时回退到同一 fallback 路径（Qodo 审查发现：原 AGENT_CONFIG_PATH
# fallback 是 _FALLBACK_BASE/agent.json5，与写入的 _FALLBACK_BASE/config/agent.json5 不一致）
# 配置目录固定存放到系统盘用户目录，不再随 KIOXIA_DATA_DIR 走：
# agent.json5 / webui_overrides / permission_mode / security_patterns / agents(人格 MD+JSON)
# 都是每次请求或高频读取的小文件，放 U 盘会拖慢响应并因 USB IO 卡住引发事件循环冻结
# （见 agent_context.build_messages 的 P0 修复注释）。只有数据库（DATA_DIR）保留 U 盘。
CONFIG_DIR = Path.home() / ".ai-agent" / "config"
AGENT_CONFIG_PATH = CONFIG_DIR / "agent.json5"
# ── 6c11c84 迁移遗漏补全：小体积热路径目录固定系统盘，仅数据库与大体积内容用 U 盘 ──
# 每轮回复都要列表情包目录/读参考音频/读记忆状态/读插件配置，放 U 盘会拖慢响应
# 且 U 盘拔出即失效（与 WORKSPACE_DIR/CONFIG_DIR 同一政策）。旧 U 盘数据由
# _ensure_workspace() 幂等迁移到系统盘；大体积内容（files/media/tts_cache/logs）
# 仍留在 U 盘（系统盘仅剩 3.8G，eMMC 空间有限）。
STICKER_DIR = Path.home() / ".ai-agent" / "stickers"
XIAOLI_STICKER_DIR = Path.home() / ".ai-agent" / "xiaoli-stickers"
# 通用智能体表情包根目录：每个子智能体的表情包存放在 {AGENT_STICKER_BASE}/{agent_name}/
AGENT_STICKER_BASE = Path.home() / ".ai-agent" / "agent-stickers"
FILE_DIR = _resolve_data_path(_KIOXIA_BASE / "files", _FALLBACK_BASE / "files")
# 媒体目录（用户上传图片、生成的 TTS/图片/视频、壁纸等可写资源）。
# 固定系统盘用户目录，不参与 _resolve_data_path 的 U 盘分支：壁纸/小图资源仅 5 张、
# 体积极小，与 U 盘解耦（U 盘只承载向量库 db/files 等大体积数据）。与 STICKER_DIR /
# VOICE_REF_DIR / CONFIG_DIR 同构，使用 Path.home()/.ai-agent 体系，非硬编码绝对路径。
MEDIA_DIR = Path.home() / ".ai-agent" / "media"
# 参考音频目录（用户上传的 TTS 参考音频，按 agent 分子目录）——每次 TTS 热读，迁系统盘
VOICE_REF_DIR = Path.home() / ".ai-agent" / "voice_refs"
# 记忆状态目录（记忆编码状态等运行时可写数据）——高频读写，迁系统盘
MEMORY_STATE_DIR = Path.home() / ".ai-agent" / "memory_state"
# 插件配置目录——启动与鉴权读取，迁系统盘
PLUGINS_CONFIG_DIR = Path.home() / ".ai-agent" / "plugins"
# 子 Agent 配置目录（人格文件、配置 JSON）
# 从统一 CONFIG_DIR 派生，确保与 AGENT_CONFIG_PATH 和 _init_user_resources 同源
AGENTS_CONFIG_DIR = CONFIG_DIR / "agents"

# ── 初始化收口（2026-08-22 config import 副作用瘦身）──────────────
# 原：9 个目录 mkdir + _ensure_workspace()（迁移扫描×7 + frozen 迁移×11 +
# 模板播种）在模块顶层执行——任何 import 都产生目录创建与文件 IO，
# 测试/工具脚本被迫依赖预播种的 ~/.ai-agent。
# 现：全部收进幂等的 initialize_config()，由真实入口（agent.py/cli.py/
# web server 启动/qq 独立运行）调用；纯 import 只得到纯路径常量。
_INITIALIZED = False


def initialize_config() -> None:
    """幂等配置初始化：建目录 + workspace 迁移/模板播种。入口必须早调。

    多次调用安全；并发场景下最坏情况是重复 mkdir(exist_ok=True) 与一次
    冗余迁移扫描（_ensure_workspace 自身按存在性跳过），无正确性影响。
    """
    global _INITIALIZED
    if _INITIALIZED:
        return
    WORKSPACE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STICKER_DIR.mkdir(parents=True, exist_ok=True)
    XIAOLI_STICKER_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_STICKER_BASE.mkdir(parents=True, exist_ok=True)
    VOICE_REF_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_STATE_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # 路径常量已全部定义（函数体调用时执行），_ensure_workspace 的迁移逻辑可安全引用。
    # _INITIALIZED 置位放最后：中途失败不短路，下次调用可重试（review 补漏），
    # mkdir(exist_ok)/迁移逻辑自身幂等，重试安全。
    _ensure_workspace()
    _INITIALIZED = True
