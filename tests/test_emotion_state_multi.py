"""EmotionState 多情绪共存与 shift_pad 测试

测试覆盖:
1. get_current() 向下兼容：仍返回 (emotion, intensity) 元组
2. get_active_emotions()：更新一个情绪后返回列表，多个后最多 3 个
3. shift_pad()：微调 PAD 值，权重 0.1 时偏移 10%
4. get_pad()：返回当前 PAD 值
5. 多情绪独立衰减：happy(0.8) 后再 sad(0.6)，两者都在 active 列表
6. 保留强度最高的 3 个：更新 4 个情绪后只有 3 个
7. 持久化：_save / _load 包含 active_emotions 和 pad 字段
"""
import json

import pytest

import emotion.emotion_state as es
from emotion.emotion_state import EmotionState


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """提供一个指向临时路径的全新 EmotionState 实例（隔离全局单例）。"""
    persist = tmp_path / "emotion_state_multi.json"
    monkeypatch.setenv("EMOTION_STATE_PATH", str(persist))
    # 重置全局单例，避免受先前测试影响
    es._instance = None
    state = EmotionState()
    yield state
    es._instance = None


# ── 1. get_current 向下兼容 ──────────────────────────────────


class TestGetCurrentBackwardCompat:
    """get_current() 仍返回 (emotion, intensity) 元组"""

    def test_returns_tuple(self, fresh_state):
        fresh_state.update("happy", 0.8)
        result = fresh_state.get_current()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_returns_emotion_and_intensity(self, fresh_state):
        fresh_state.update("happy", 0.8)
        emotion, intensity = fresh_state.get_current()
        assert emotion == "happy"
        assert intensity == pytest.approx(0.8, abs=1e-6)

    def test_neutral_returns_neutral_tuple(self, fresh_state):
        emotion, intensity = fresh_state.get_current()
        assert emotion == "neutral"
        assert intensity == 0.0


# ── 2. get_active_emotions ───────────────────────────────────


class TestGetActiveEmotions:
    """get_active_emotions() 返回活跃情绪列表"""

    def test_returns_list(self, fresh_state):
        fresh_state.update("happy", 0.8)
        active = fresh_state.get_active_emotions()
        assert isinstance(active, list)

    def test_single_emotion_present(self, fresh_state):
        fresh_state.update("happy", 0.8)
        active = fresh_state.get_active_emotions()
        assert len(active) >= 1
        emotions = [e for e, _ in active]
        assert "happy" in emotions

    def test_items_are_tuple_of_str_float(self, fresh_state):
        fresh_state.update("happy", 0.8)
        active = fresh_state.get_active_emotions()
        for item in active:
            assert isinstance(item, tuple)
            assert len(item) == 2
            assert isinstance(item[0], str)
            assert isinstance(item[1], float)

    def test_at_most_three(self, fresh_state):
        for emo, i in [("happy", 0.8), ("sad", 0.7), ("angry", 0.6), ("surprised", 0.5)]:
            fresh_state.update(emo, i)
        active = fresh_state.get_active_emotions()
        assert len(active) <= 3

    def test_sorted_by_intensity_desc(self, fresh_state):
        fresh_state.update("sad", 0.6)
        fresh_state.update("happy", 0.9)
        active = fresh_state.get_active_emotions()
        intensities = [i for _, i in active]
        assert intensities == sorted(intensities, reverse=True)


# ── 3. shift_pad ─────────────────────────────────────────────


class TestShiftPad:
    """shift_pad() 微调 PAD 值"""

    def test_default_weight_shifts_10_percent(self, fresh_state):
        fresh_state.shift_pad({"P": 1.0, "A": 1.0, "D": 1.0})
        pad = fresh_state.get_pad()
        # 初始 P=0.0 → 0.0*0.9 + 1.0*0.1 = 0.1
        assert pad["P"] == pytest.approx(0.1, abs=1e-6)
        # 初始 A=0.0 → 0.0*0.9 + 1.0*0.1 = 0.1
        assert pad["A"] == pytest.approx(0.1, abs=1e-6)
        # 初始 D=0.5 → 0.5*0.9 + 1.0*0.1 = 0.55
        assert pad["D"] == pytest.approx(0.55, abs=1e-6)

    def test_explicit_weight_0_1(self, fresh_state):
        fresh_state.shift_pad({"P": 1.0, "A": 1.0, "D": 1.0}, weight=0.1)
        pad = fresh_state.get_pad()
        assert pad["P"] == pytest.approx(0.1, abs=1e-6)
        assert pad["A"] == pytest.approx(0.1, abs=1e-6)
        assert pad["D"] == pytest.approx(0.55, abs=1e-6)

    def test_weight_zero_no_change(self, fresh_state):
        fresh_state.shift_pad({"P": 1.0, "A": 1.0, "D": 1.0}, weight=0.0)
        pad = fresh_state.get_pad()
        assert pad["P"] == pytest.approx(0.0, abs=1e-6)
        assert pad["A"] == pytest.approx(0.0, abs=1e-6)
        assert pad["D"] == pytest.approx(0.5, abs=1e-6)

    def test_weight_one_full_replace(self, fresh_state):
        fresh_state.shift_pad({"P": -0.5, "A": 0.7, "D": 0.9}, weight=1.0)
        pad = fresh_state.get_pad()
        assert pad["P"] == pytest.approx(-0.5, abs=1e-6)
        assert pad["A"] == pytest.approx(0.7, abs=1e-6)
        assert pad["D"] == pytest.approx(0.9, abs=1e-6)

    def test_p_clamped_to_negative_one(self, fresh_state):
        fresh_state.shift_pad({"P": -5.0, "A": 0.0, "D": 0.5}, weight=1.0)
        pad = fresh_state.get_pad()
        assert pad["P"] == pytest.approx(-1.0, abs=1e-6)

    def test_cumulative_shift(self, fresh_state):
        fresh_state.shift_pad({"P": 1.0, "A": 1.0, "D": 1.0}, weight=0.1)
        fresh_state.shift_pad({"P": 1.0, "A": 1.0, "D": 1.0}, weight=0.1)
        pad = fresh_state.get_pad()
        # 第二次: P = 0.1*0.9 + 1.0*0.1 = 0.19
        assert pad["P"] == pytest.approx(0.19, abs=1e-6)


# ── 4. get_pad ───────────────────────────────────────────────


class TestGetPad:
    """get_pad() 返回当前 PAD 值"""

    def test_initial_pad_values(self, fresh_state):
        pad = fresh_state.get_pad()
        assert isinstance(pad, dict)
        assert set(pad.keys()) == {"P", "A", "D"}
        assert pad["P"] == pytest.approx(0.0, abs=1e-6)
        assert pad["A"] == pytest.approx(0.0, abs=1e-6)
        assert pad["D"] == pytest.approx(0.5, abs=1e-6)

    def test_returns_copy_not_reference(self, fresh_state):
        pad1 = fresh_state.get_pad()
        pad1["P"] = 999.0
        pad2 = fresh_state.get_pad()
        assert pad2["P"] == pytest.approx(0.0, abs=1e-6)


# ── 5. 多情绪独立衰减 ────────────────────────────────────────


class TestMultiEmotionCoexist:
    """多个情绪同时存在，各自独立记录"""

    def test_happy_and_sad_both_active(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.6)
        active = fresh_state.get_active_emotions()
        emotions = [e for e, _ in active]
        assert "happy" in emotions
        assert "sad" in emotions

    def test_both_above_threshold(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.6)
        active = fresh_state.get_active_emotions()
        for _, intensity in active:
            assert intensity >= fresh_state.NEUTRAL_THRESHOLD

    def test_three_emotions_coexist(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.6)
        fresh_state.update("angry", 0.5)
        active = fresh_state.get_active_emotions()
        emotions = {e for e, _ in active}
        assert {"happy", "sad", "angry"}.issubset(emotions)


# ── 6. 保留强度最高的 3 个 ───────────────────────────────────


class TestKeepTopThree:
    """更新 4 个情绪后只保留强度最高的 3 个"""

    def test_four_emotions_keeps_three(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.7)
        fresh_state.update("angry", 0.6)
        fresh_state.update("surprised", 0.4)
        active = fresh_state.get_active_emotions()
        assert len(active) == 3

    def test_lowest_intensity_dropped(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.7)
        fresh_state.update("angry", 0.6)
        fresh_state.update("surprised", 0.4)
        active = fresh_state.get_active_emotions()
        emotions = [e for e, _ in active]
        assert "surprised" not in emotions

    def test_top_three_preserved(self, fresh_state):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.7)
        fresh_state.update("angry", 0.6)
        fresh_state.update("surprised", 0.4)
        active = fresh_state.get_active_emotions()
        emotions = {e for e, _ in active}
        assert {"happy", "sad", "angry"}.issubset(emotions)


# ── 7. 持久化 ────────────────────────────────────────────────


class TestPersistence:
    """_save / _load 包含 active_emotions 和 pad 字段"""

    def test_save_includes_active_emotions_field(self, fresh_state, tmp_path):
        fresh_state.update("happy", 0.8)
        fresh_state._save()
        data = json.loads(
            (tmp_path / "emotion_state_multi.json").read_text(encoding="utf-8")
        )
        assert "active_emotions" in data

    def test_save_includes_pad_field(self, fresh_state, tmp_path):
        fresh_state.shift_pad({"P": 0.3, "A": 0.2, "D": 0.6}, weight=0.5)
        fresh_state._save()
        data = json.loads(
            (tmp_path / "emotion_state_multi.json").read_text(encoding="utf-8")
        )
        assert "pad" in data

    def test_save_active_emotions_content(self, fresh_state, tmp_path):
        fresh_state.update("happy", 0.8)
        fresh_state.update("sad", 0.6)
        fresh_state._save()
        data = json.loads(
            (tmp_path / "emotion_state_multi.json").read_text(encoding="utf-8")
        )
        assert "happy" in data["active_emotions"]
        assert "sad" in data["active_emotions"]

    def test_load_restores_active_emotions(self, tmp_path, monkeypatch):
        persist = tmp_path / "emotion_state_load.json"
        monkeypatch.setenv("EMOTION_STATE_PATH", str(persist))
        es._instance = None
        state1 = EmotionState()
        state1.update("happy", 0.8)
        state1.update("sad", 0.6)
        state1._save()
        es._instance = None

        state2 = EmotionState()
        assert "happy" in state2._active_emotions
        assert "sad" in state2._active_emotions
        es._instance = None

    def test_load_restores_pad(self, tmp_path, monkeypatch):
        persist = tmp_path / "emotion_state_load_pad.json"
        monkeypatch.setenv("EMOTION_STATE_PATH", str(persist))
        es._instance = None
        state1 = EmotionState()
        state1.shift_pad({"P": 0.4, "A": 0.3, "D": 0.7}, weight=1.0)
        state1._save()
        es._instance = None

        state2 = EmotionState()
        pad = state2.get_pad()
        assert pad["P"] == pytest.approx(0.4, abs=1e-6)
        assert pad["A"] == pytest.approx(0.3, abs=1e-6)
        assert pad["D"] == pytest.approx(0.7, abs=1e-6)
        es._instance = None

    def test_shift_pad_persists_without_explicit_save(self, tmp_path, monkeypatch):
        """shift_pad 应自动持久化，无需显式调用 _save()"""
        persist = tmp_path / "emotion_state_shift_persist.json"
        monkeypatch.setenv("EMOTION_STATE_PATH", str(persist))
        es._instance = None
        state1 = EmotionState()
        state1.shift_pad({"P": 0.5, "A": 0.4, "D": 0.8}, weight=1.0)
        # 不调用 _save()
        es._instance = None

        state2 = EmotionState()
        pad = state2.get_pad()
        assert pad["P"] == pytest.approx(0.5, abs=1e-6)
        assert pad["A"] == pytest.approx(0.4, abs=1e-6)
        assert pad["D"] == pytest.approx(0.8, abs=1e-6)
        es._instance = None

    def test_load_defaults_when_missing(self, tmp_path, monkeypatch):
        """旧格式文件（无 active_emotions/pad）加载时使用默认值"""
        persist = tmp_path / "emotion_state_old.json"
        persist.write_text(
            json.dumps({
                "current": "happy",
                "intensity": 0.5,
                "last_update": 0.0,
                "history": [],
            }),
            encoding="utf-8",
        )
        monkeypatch.setenv("EMOTION_STATE_PATH", str(persist))
        es._instance = None
        state = EmotionState()
        assert state._active_emotions == {}
        assert state._pad == {"P": 0.0, "A": 0.0, "D": 0.5}
        es._instance = None
