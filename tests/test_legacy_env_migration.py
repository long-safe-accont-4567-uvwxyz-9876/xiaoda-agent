"""旧版 .env 固化默认值的迁移（utils/common.migrate_legacy_env_defaults）。

字段机背景：0.5.80 安装版用户 ~/.ai-agent/.env 遗留 WEBUI_PORT=8080（老
.env.example 随 first-run 复制固化），看门狗一直以 --port 8080 拉起主进程，
违背"安装包统一 8082"。
"""
from pathlib import Path

from utils.common import DEFAULT_WEBUI_PORT, migrate_legacy_env_defaults


def test_migrates_legacy_default_port(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text(
        "WEBUI_PORT=8080\n"
        "FOO=bar\n"
        "# WEBUI_PORT=8080（注释不碰）\n"
        "\n",
        encoding="utf-8",
    )

    assert migrate_legacy_env_defaults(env) is True
    lines = env.read_text(encoding="utf-8").splitlines()
    assert f"WEBUI_PORT={DEFAULT_WEBUI_PORT}" in lines
    assert "FOO=bar" in lines
    assert "# WEBUI_PORT=8080（注释不碰）" in lines


def test_keeps_custom_value_untouched(tmp_path: Path):
    """用户显式自定义的端口（9090）不是旧默认，不得改写。"""
    env = tmp_path / ".env"
    env.write_text("WEBUI_PORT=9090\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) is False
    assert env.read_text(encoding="utf-8").strip() == "WEBUI_PORT=9090"


def test_missing_file_is_noop(tmp_path: Path):
    assert migrate_legacy_env_defaults(tmp_path / "nope.env") is False


def test_migration_is_idempotent(tmp_path: Path):
    env = tmp_path / ".env"
    env.write_text("WEBUI_PORT=8080\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) is True
    assert migrate_legacy_env_defaults(env) is False
