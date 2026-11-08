"""审计 L2：只读文件系统检测不再每次初始化都写探针文件。

原实现为了判断"目录是否可写"，每次 DatabaseManager 初始化都写一个
.db_write_probe 再删掉（外置盘/SD 卡上多出两次元数据写入）。改为先读
挂载标志（statvfs 的 ST_RDONLY）与目录权限（os.access），判定不了才算失败。
"""
from __future__ import annotations

import os
import types

import pytest

from db.database import DatabaseManager


def _db(tmp_path) -> DatabaseManager:
    return DatabaseManager(db_path=tmp_path / "agent.db")


def test_writable_dir_writes_no_probe_file(tmp_path):
    db = _db(tmp_path)
    db._ensure_writable_dir()
    assert list(tmp_path.glob(".db_write_probe")) == []


def test_readonly_mount_rejected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(
        os, "statvfs", lambda _p: types.SimpleNamespace(f_flag=os.ST_RDONLY))
    with pytest.raises(OSError) as excinfo:
        db._ensure_writable_dir()
    assert "ST_RDONLY" in str(excinfo.value)


def test_unwritable_dir_rejected(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(
        os, "statvfs", lambda _p: types.SimpleNamespace(f_flag=0))
    monkeypatch.setattr(os, "access", lambda *_a, **_k: False)
    with pytest.raises(OSError) as excinfo:
        db._ensure_writable_dir()
    assert "os.access" in str(excinfo.value)


def test_statvfs_failure_rejected(tmp_path, monkeypatch):
    db = _db(tmp_path)

    def _boom(_p):
        raise OSError("stale mount")

    monkeypatch.setattr(os, "statvfs", _boom)
    with pytest.raises(OSError):
        db._ensure_writable_dir()
