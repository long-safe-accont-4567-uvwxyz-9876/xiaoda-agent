"""旧版 .env 固化默认值的迁移（utils/common.migrate_legacy_env_defaults）。

字段机背景：0.5.80 安装版用户 ~/.ai-agent/.env 遗留 WEBUI_PORT=8080（老
.env.example 随 first-run 复制固化），看门狗一直以 --port 8080 拉起主进程；
WEBUI_HOST=0.0.0.0 + 无密码则触发 VULN-11 fail-closed，免密登录连本机也 403。
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

    assert migrate_legacy_env_defaults(env) == ["WEBUI_PORT"]
    lines = env.read_text(encoding="utf-8").splitlines()
    assert f"WEBUI_PORT={DEFAULT_WEBUI_PORT}" in lines
    assert "FOO=bar" in lines
    assert "# WEBUI_PORT=8080（注释不碰）" in lines


def test_migrates_webui_host_when_no_password(tmp_path: Path, monkeypatch):
    """无密码 + 0.0.0.0 = 免密死结（VULN-11 拒本机），条件迁移改绑回环。"""
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("WEBUI_HOST=0.0.0.0\nWEBUI_PASSWORD=\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) == ["WEBUI_HOST"]
    text = env.read_text(encoding="utf-8")
    assert "WEBUI_HOST=127.0.0.1" in text


def test_keeps_webui_host_when_password_set(tmp_path: Path, monkeypatch):
    """已设密码的用户可能刻意开放局域网，WEBUI_HOST 不得动。"""
    monkeypatch.setenv("WEBUI_PASSWORD", "super-secret-pass")
    env = tmp_path / ".env"
    env.write_text("WEBUI_HOST=0.0.0.0\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) == []
    assert env.read_text(encoding="utf-8").strip() == "WEBUI_HOST=0.0.0.0"


def test_migrates_both_keys_together(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("WEBUI_PORT=8080\nWEBUI_HOST=0.0.0.0\n", encoding="utf-8")

    assert sorted(migrate_legacy_env_defaults(env)) == ["WEBUI_HOST", "WEBUI_PORT"]


def test_keeps_custom_value_untouched(tmp_path: Path, monkeypatch):
    """用户显式自定义的端口（9090）不是旧默认，不得改写。"""
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("WEBUI_PORT=9090\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) == []
    assert env.read_text(encoding="utf-8").strip() == "WEBUI_PORT=9090"


def test_missing_file_is_noop(tmp_path: Path):
    assert migrate_legacy_env_defaults(tmp_path / "nope.env") == []


def test_migration_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WEBUI_PASSWORD", raising=False)
    env = tmp_path / ".env"
    env.write_text("WEBUI_PORT=8080\n", encoding="utf-8")

    assert migrate_legacy_env_defaults(env) == ["WEBUI_PORT"]
    assert migrate_legacy_env_defaults(env) == []
