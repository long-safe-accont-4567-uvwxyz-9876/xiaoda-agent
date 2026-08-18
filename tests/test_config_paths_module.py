"""config.py Phase 1（路径与 workspace 引导块抽出）结构契约测试。

背景：config.py 是 1412 行、变更热度全项目第 1（134 次）的模块级命名空间。
Phase 1 把路径解析与 workspace 引导块（get_base_dir / ENV_PATH+dotenv 加载 /
KIOXIA 数据路径解析 / 目录常量 / 冻结模式资源复制 / 数据迁移 / _ensure_workspace）
抽为 config_paths.py，逐字节搬移。

契约：
    1. config_paths 可独立导入（不依赖 config，无循环导入）
    2. config 同名 re-export：`from config import DATA_DIR / get_config_dir` 等
       既有用法不受影响（同对象）
    3. dotenv 在 config_paths 导入时加载（config 传递导入后环境变量可用）
    4. 路径语义不变：DATA_DIR 遵循 KIOXIA_DATA_DIR 显式配置 + 挂载检测
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

# ── 1/2. 独立导入 + re-export 同对象 ─────────────────────────────

def test_config_paths_imports_standalone():
    import importlib
    mod = importlib.import_module("config_paths")
    for name in ("get_base_dir", "get_env_path", "get_credentials_dir",
                 "get_config_dir", "_resolve_data_path", "_ensure_workspace",
                 "DATA_DIR", "LOG_DIR", "CONFIG_DIR", "WORKSPACE_DIR",
                 "ENV_PATH", "STICKER_DIR", "MEDIA_DIR", "AGENT_CONFIG_PATH"):
        assert hasattr(mod, name), f"缺少符号 {name}"


@pytest.mark.parametrize("name", [
    "get_base_dir", "get_env_path", "get_credentials_dir", "get_config_dir",
    "is_data_dir_writable", "_ensure_workspace",
    "ENV_PATH", "_KIOXIA_BASE", "_FALLBACK_BASE",
    "DATA_DIR", "LOG_DIR", "WORKSPACE_DIR", "CREDENTIALS_DIR",
    "CONFIG_DIR", "AGENT_CONFIG_PATH", "STICKER_DIR", "XIAOLI_STICKER_DIR",
    "AGENT_STICKER_BASE", "FILE_DIR", "MEDIA_DIR", "VOICE_REF_DIR",
    "MEMORY_STATE_DIR", "PLUGINS_CONFIG_DIR", "AGENTS_CONFIG_DIR",
])
def test_config_reexports_same_objects(name):
    import config
    import config_paths
    assert hasattr(config, name), f"config 缺少兼容别名 {name}"
    assert getattr(config, name) is getattr(config_paths, name), name


# ── 3. dotenv 副作用保持 ──────────────────────────────────────────

def test_dotenv_loaded_on_config_import(monkeypatch):
    """config 导入链上 dotenv(ENV_PATH) 已执行（.env 中已定义的变量进 os.environ）"""
    import config_paths
    # 从 .env 文件读取一个键，验证导入后可见（用 ENV_PATH 解析而非硬编码键名）
    env_text = Path(config_paths.ENV_PATH).read_text(encoding="utf-8", errors="ignore") \
        if Path(config_paths.ENV_PATH).exists() else ""
    defined_keys = [ln.split("=")[0].strip() for ln in env_text.splitlines()
                    if "=" in ln and not ln.strip().startswith("#") and ln.split("=")[0].strip()]
    assert defined_keys, ".env 为空时本用例无意义，跳过断言也可"
    for key in defined_keys[:5]:
        assert key in os.environ, f".env 中的 {key} 应已进入 os.environ"


# ── 4. 路径语义不变 ───────────────────────────────────────────────

def test_data_dir_respects_kioxia_env():
    """显式 KIOXIA_DATA_DIR 指向不存在路径时，DATA_DIR 不落在幻影目录"""
    import config_paths
    env_set = bool(os.getenv("KIOXIA_DATA_DIR"))
    if not env_set:
        # 未设 env：DATA_DIR 落在默认 ~/.ai-agent/data/db
        assert str(config_paths.DATA_DIR).endswith("db")
    else:
        # 显式设置：DATA_DIR 要么在 KIOXIA 下，要么是 fallback，但必须可写存在
        assert config_paths.DATA_DIR.exists()


def test_workspace_and_config_on_system_disk():
    """WORKSPACE_DIR/CONFIG_DIR 固定系统盘（不随 KIOXIA 走）——政策契约"""
    import config_paths
    assert str(config_paths.WORKSPACE_DIR).startswith(str(Path.home()))
    assert str(config_paths.CONFIG_DIR).startswith(str(Path.home()))
