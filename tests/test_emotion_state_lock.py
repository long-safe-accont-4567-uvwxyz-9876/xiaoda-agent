"""EmotionState J-Space Hook 锁外写 _intensity 并发竞态修复测试。

覆盖:
1. 结构性测试（确定性 RED→GREEN）：J-Space Hook 的 _intensity 读改写
   必须在 self._lock 临界区内执行。
2. 并发测试：多线程并发 update() 的最终 _intensity 与串行执行结果一致。
3. 功能性测试：单次 update(emotion=..., context={"emotion_offset": ...})
   后 _intensity 被正确更新。
"""
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import emotion.emotion_state as es_module
from emotion.emotion_state import EmotionState


@pytest.fixture
def hooked_state(tmp_path, monkeypatch):
    """全新 EmotionState：临时持久化路径、关闭落盘、强制开启 J-Space Hook。"""
    import config as config_mod

    monkeypatch.setenv("EMOTION_STATE_PATH", str(tmp_path / "emotion_state_lock.json"))
    monkeypatch.setattr(config_mod, "ENABLE_J_SPACE_HOOKS", True)
    state = EmotionState()
    monkeypatch.setattr(state, "_save", lambda: None)
    return state


def test_j_space_hook_updates_intensity_under_lock(hooked_state, monkeypatch):
    """J-Space Hook 修改 _intensity 时必须在 self._lock 临界区内。

    通过替换 logger.debug 捕获 "direction_applied" 日志时刻的锁状态：
    修复前该日志在锁外（locked() == False），修复后在锁内（locked() == True）。
    """
    calls = []

    class _FakeLogger:
        def debug(self, event, **kwargs):
            calls.append((event, hooked_state._lock.locked()))

        def info(self, *args, **kwargs):
            pass

    monkeypatch.setattr(es_module, "logger", _FakeLogger())

    hooked_state.update("happy", 0.5, context={"emotion_offset": 0.3})

    direction_calls = [held for event, held in calls if event == "emotion_state.direction_applied"]
    assert direction_calls, "J-Space Hook 未触发 direction_applied 日志，测试前提不成立"
    assert all(direction_calls), (
        "J-Space Hook 的 _intensity 读改写必须在 self._lock 临界区内执行"
    )


def test_update_applies_emotion_offset(hooked_state):
    """功能性测试：emotion_offset 按 0.1 权重作用于 _intensity。"""
    hooked_state.update("happy", 0.5, context={"emotion_offset": 0.5})
    # 0.5 + 0.5 * 0.1 = 0.55
    assert hooked_state._intensity == pytest.approx(0.55, abs=1e-9)


def test_concurrent_updates_match_serial(tmp_path, monkeypatch):
    """并发 update() 的最终 _intensity 必须与串行执行相同操作序列一致。

    关闭时间衰减后，主流程对相同情绪（intensity=0.0）不改变强度，仅 Hook
    每次 +0.05；若 Hook 的读改写未加锁保护，并发下会发生丢失更新。
    """
    import config as config_mod

    monkeypatch.setenv("EMOTION_STATE_PATH", str(tmp_path / "emotion_state_conc.json"))
    monkeypatch.setattr(config_mod, "ENABLE_J_SPACE_HOOKS", True)

    def make_state():
        state = EmotionState()
        monkeypatch.setattr(state, "_save", lambda: None)
        # 关闭时间衰减，保证并发与串行结果可精确比较
        monkeypatch.setattr(state, "_decayed_intensity", lambda: state._intensity)
        state._current = "happy"
        state._intensity = 0.0
        return state

    n = 20
    offset = 0.5  # 每次 +0.05，串行结果 = 1.0（clamp）

    serial = make_state()
    for _ in range(n):
        serial.update("happy", 0.0, context={"emotion_offset": offset})
    expected = serial._intensity

    state = make_state()
    barrier = threading.Barrier(n)

    def worker(_):
        barrier.wait()
        state.update("happy", 0.0, context={"emotion_offset": offset})

    old_switch = sys.getswitchinterval()
    try:
        # 降低 GIL 切换间隔，放大竞态窗口
        sys.setswitchinterval(1e-5)
        with ThreadPoolExecutor(max_workers=n) as pool:
            list(pool.map(worker, range(n)))
    finally:
        sys.setswitchinterval(old_switch)

    assert state._intensity == pytest.approx(expected, abs=1e-9)
