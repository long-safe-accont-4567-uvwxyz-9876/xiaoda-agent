"""审查发现：落盘失败不能丢脏标记（H2 的写失败路径）。"""
from __future__ import annotations

import pytest

from core.xp_system import XPSystem


def test_flush_failure_keeps_dirty(tmp_path, monkeypatch):
    xp = XPSystem(data_dir=tmp_path, flush_interval=10.0)

    def _boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(xp, "_save", _boom)
    xp._mark_dirty("u1")
    with pytest.raises(OSError):
        xp.flush()
    # 写失败后脏标记必须还在，下一窗口才能重试
    assert xp.pending_users == {"u1"}


def test_flush_success_clears_dirty(tmp_path):
    xp = XPSystem(data_dir=tmp_path, flush_interval=10.0)
    xp._mark_dirty("u1")
    assert xp.pending_users == {"u1"}
    xp.flush()
    assert xp.pending_users == set()
    assert xp.write_count >= 1
