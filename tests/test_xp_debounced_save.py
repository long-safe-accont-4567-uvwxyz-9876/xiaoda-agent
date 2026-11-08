"""审计 H2：XP 状态落盘合并（debounce）与原子写语义。

背景（2026-11-08 只读审计）：core/xp_system.py:_save() 把 33 个用户的
全部状态 json.dumps(indent=2) 后 mkstemp+fsync+os.replace 全量重写
（生产文件 449,049 字节，序列化实测 32.0ms），而 add_chat_xp 挂在每条
消息的热路径上，等于每说一句话写 449KB 并 fsync 一次。

修复：热路径只「标记脏 + 合并延迟落盘」，进程退出 atexit flush 兜底，
原子写（tmp + fsync + replace）语义不变。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import core.xp_system as mod


@pytest.fixture(autouse=True)
def _xp_enabled(monkeypatch):
    monkeypatch.setenv("XP_SYSTEM_ENABLED", "true")
    mod._reset_persona_cache()
    yield


def _make(tmp_path, interval: float = 60.0) -> "mod.XPSystem":
    return mod.XPSystem(data_dir=tmp_path, flush_interval=interval)


def test_many_messages_collapse_into_one_write(tmp_path):
    """50 条消息只落盘一次（修复前 50 次全量重写）。"""
    xp = _make(tmp_path, interval=60)
    for _ in range(50):
        xp.add_chat_xp("u1", 10)

    assert xp.write_count == 0, "热路径不应每次消息都写盘"
    assert xp.pending_users == {"u1"}
    assert not xp._state_path.exists()

    xp.flush()
    assert xp.write_count == 1
    assert xp.pending_users == set()
    data = json.loads(xp._state_path.read_text(encoding="utf-8"))
    assert data["users"]["u1"]["xp"] == 50


def test_flush_interval_zero_keeps_old_behaviour(tmp_path):
    """flush_interval=0 时每次变更立即落盘（排障/兼容旧行为）。"""
    xp = _make(tmp_path, interval=0)
    for _ in range(5):
        xp.add_chat_xp("u1", 10)
    # 6 = 首条消息建用户时 get_state 标记 1 次 + add_xp 标记 1 次，其余各 1 次
    assert xp.write_count == 6
    assert xp.pending_users == set()


def test_timer_flushes_without_explicit_call(tmp_path):
    """合并窗口到期后由后台定时器自动落盘。"""
    xp = _make(tmp_path, interval=0.05)
    xp.add_chat_xp("u1", 10)
    deadline = time.time() + 2.0
    while xp.write_count == 0 and time.time() < deadline:
        time.sleep(0.01)
    assert xp.write_count == 1
    assert xp.pending_users == set()
    assert xp._state_path.exists()


def test_save_is_still_synchronous(tmp_path):
    """_save() 仍是同步原子写（老调用方/排障路径依赖）。"""
    xp = _make(tmp_path, interval=60)
    xp.add_chat_xp("u1", 10)
    xp._save()
    assert xp.write_count == 1
    data = json.loads(xp._state_path.read_text(encoding="utf-8"))
    assert data["users"]["u1"]["xp"] == 1


def test_atomic_write_leaves_no_temp_file(tmp_path):
    """原子写语义不变：落盘后目录里只剩 xp_state.json。"""
    xp = _make(tmp_path, interval=0)
    xp.add_chat_xp("u1", 10)
    xp.flush()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["xp_state.json"], names


def test_concurrent_writers_do_not_lose_xp(tmp_path):
    """多线程（to_thread 工作线程）并发加 XP 不丢增量。"""
    xp = _make(tmp_path, interval=0.01)

    def _worker() -> None:
        for _ in range(20):
            xp.add_chat_xp("shared", 10)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    xp.flush()

    data = json.loads(xp._state_path.read_text(encoding="utf-8"))
    assert data["users"]["shared"]["xp"] == 8 * 20


def test_daily_login_respects_single_flush(tmp_path):
    """每日登录同一天只加一次，且不会因为合并写而丢 last_daily_login_date。"""
    xp = _make(tmp_path, interval=60)
    xp.add_daily_login_xp("u1")
    xp.add_daily_login_xp("u1")
    xp.flush()
    data = json.loads(xp._state_path.read_text(encoding="utf-8"))
    assert data["users"]["u1"]["xp"] == 5
    assert data["users"]["u1"]["last_daily_login_date"] != ""
