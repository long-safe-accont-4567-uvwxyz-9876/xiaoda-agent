#!/usr/bin/env python3
"""凭证迁移脚本 - 把 .env 中的明文 API Key 加密为 enc:v1: 格式

使用方法：
    python scripts/migrate_credentials.py
    python scripts/migrate_credentials.py --env /path/to/.env

特性：
- 幂等：重复运行不会重复加密已加密的值
- 机器绑定：加密后的值仅可在本机解密，复制到其他机器将无法使用
- 向后兼容：未加密的明文值仍可被程序正常读取（运行时自动解密）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 将项目根目录加入 sys.path，使 security.credential_vault 可被导入
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from security.credential_vault import migrate_env_file  # noqa: E402


def _default_env_path() -> str:
    """获取默认 .env 路径

    PyInstaller 打包后使用用户目录 ~/.ai-agent/.env，
    开发模式使用项目根目录的 .env。
    此处不导入 config 模块以避免副作用（config 导入会触发 load_dotenv 等）。
    """
    if getattr(sys, "frozen", False):
        return str(Path.home() / ".ai-agent" / ".env")
    return str(_PROJECT_ROOT / ".env")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="迁移 .env 中的明文凭证为加密格式（enc:v1:）"
    )
    parser.add_argument(
        "--env",
        default=None,
        help=".env 文件路径（默认使用项目根目录或 ~/.ai-agent/.env）",
    )
    args = parser.parse_args()

    env_path = args.env or _default_env_path()

    if not Path(env_path).exists():
        print(f"[migrate] .env 文件不存在：{env_path}")
        print("[migrate] 请先复制 .env.example 为 .env 并填写 API Key")
        return 1

    print(f"[migrate] 开始扫描：{env_path}")
    migrated = migrate_env_file(env_path)

    if migrated == 0:
        print("[migrate] 未发现明文凭证（所有敏感字段已加密或为空）")
    else:
        print(f"[migrate] 成功加密 {migrated} 个明文凭证")
        print("[migrate] 提示：加密后的值仅可在本机解密，复制到其他机器将无法使用")

    return 0


if __name__ == "__main__":
    sys.exit(main())
