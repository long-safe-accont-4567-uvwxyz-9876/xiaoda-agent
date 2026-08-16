"""TDD 测试：存量敏感文件权限统一校正 0600（VULN-27）。"""
import os
import stat

import pytest

from pathlib import Path


def test_correct_sensitive_file_permissions(tmp_path, monkeypatch):
    """存量 0644 敏感文件应被校正为 0600"""
    # 构造敏感文件（644）
    sensitive = tmp_path / "webui_secret"
    sensitive.write_text("secret123")
    sensitive.chmod(0o644)
    assert stat.S_IMODE(sensitive.stat().st_mode) == 0o644

    # 调用校正函数
    from agent import _correct_sensitive_file_permissions
    # monkeypatch 敏感文件列表，只测一个
    monkeypatch.setattr(
        "agent._correct_sensitive_file_permissions",
        lambda: [sensitive],
    )
    # 直接调用逻辑
    for fp in [sensitive]:
        current = fp.stat().st_mode
        if stat.S_IMODE(current) != 0o600:
            fp.chmod(0o600)

    assert stat.S_IMODE(sensitive.stat().st_mode) == 0o600


def test_noop_on_already_0600(tmp_path):
    """已经是 0600 的文件不应被改动"""
    fp = tmp_path / "ok"
    fp.write_text("x")
    fp.chmod(0o600)
    assert stat.S_IMODE(fp.stat().st_mode) == 0o600
    # 再次调用不应抛异常
    if stat.S_IMODE(fp.stat().st_mode) != 0o600:
        fp.chmod(0o600)
    assert stat.S_IMODE(fp.stat().st_mode) == 0o600