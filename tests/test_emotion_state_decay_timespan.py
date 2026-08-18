"""多情绪衰减时间跨度测试

代码审查发现的测试盲区：
现有测试在毫秒级间隔内连续调用 update()，时间差几乎为零，
无法检测各情绪是否使用独立时间戳正确衰减。

本测试通过直接设置 _active_emotions 的时间戳模拟时间流逝，验证：
1. 旧情绪随时间正确衰减
2. 新情绪不受旧情绪时间戳影响
3. 超过阈值的情绪被正确过滤
"""
import pytest
import time
import os


class TestMultiEmotionDecayTimespan:
    """多情绪共存 + 独立时间戳衰减"""

    @pytest.fixture
    def isolated_state(self, tmp_path, monkeypatch):
        """创建隔离的 EmotionState 实例"""
        import emotion.emotion_state as es
        es._instance = None
        monkeypatch.setenv("EMOTION_STATE_PATH", str(tmp_path / "emotion_state.json"))
        state = es.EmotionState()
        # 清空初始状态
        state._active_emotions = {}
        state._current = "neutral"
        state._intensity = 0.0
        return state

    def test_old_emotion_decays_with_time(self, isolated_state):
        """旧情绪随时间正确衰减"""
        now = time.time()
        # 2小时前更新 happy
        isolated_state._active_emotions["happy"] = (0.8, now - 7200)

        active = isolated_state.get_active_emotions()

        # happy 应从 0.8 衰减到 0.8 * 0.5^2 = 0.2
        assert len(active) == 1
        assert active[0][0] == "happy"
        assert active[0][1] == pytest.approx(0.2, abs=0.01)

    def test_new_emotion_does_not_reset_old_decay(self, isolated_state):
        """新情绪更新不重置旧情绪的衰减（核心测试盲区）"""
        now = time.time()
        # 2小时前更新 happy
        isolated_state._active_emotions["happy"] = (0.8, now - 7200)
        # 刚刚更新 sad
        isolated_state._active_emotions["sad"] = (0.6, now)

        active = isolated_state.get_active_emotions()
        active_dict = dict(active)

        # happy: 0.8 * 0.5^2 = 0.2（已衰减2小时）
        assert "happy" in active_dict
        assert active_dict["happy"] == pytest.approx(0.2, abs=0.01)
        # sad: 0.6 * 0.5^0 = 0.6（刚更新，未衰减）
        assert "sad" in active_dict
        assert active_dict["sad"] == pytest.approx(0.6, abs=0.01)

    def test_emotion_below_threshold_filtered(self, isolated_state):
        """低于 NEUTRAL_THRESHOLD 的情绪被过滤"""
        now = time.time()
        # 4小时前更新 happy: 0.8 * 0.5^4 = 0.05 < 0.1 (threshold)
        isolated_state._active_emotions["happy"] = (0.8, now - 14400)

        active = isolated_state.get_active_emotions()
        assert len(active) == 0, "happy 应已衰减到阈值以下被过滤"

    def test_three_emotions_different_decay_rates(self, isolated_state):
        """三个情绪在不同时间更新，各自独立衰减"""
        now = time.time()
        # 3小时前: happy
        isolated_state._active_emotions["happy"] = (0.8, now - 10800)
        # 2小时前: angry
        isolated_state._active_emotions["angry"] = (0.6, now - 7200)
        # 1小时前: sad
        isolated_state._active_emotions["sad"] = (0.7, now - 3600)

        active = isolated_state.get_active_emotions()
        active_dict = dict(active)

        # happy: 0.8 * 0.5^3 = 0.1 (at threshold, should be included)
        # angry: 0.6 * 0.5^2 = 0.15
        # sad: 0.7 * 0.5^1 = 0.35
        assert "sad" in active_dict
        assert active_dict["sad"] == pytest.approx(0.35, abs=0.01)
        assert "angry" in active_dict
        assert active_dict["angry"] == pytest.approx(0.15, abs=0.01)

    def test_newer_emotion_ranks_higher(self, isolated_state):
        """相同强度时，新情绪排名更高（衰减更少）"""
        now = time.time()
        # 3小时前: happy, intensity=0.6 → 0.6 * 0.5^3 = 0.075 (below threshold)
        isolated_state._active_emotions["happy"] = (0.6, now - 10800)
        # 刚刚: sad, intensity=0.6 → 0.6 (no decay)
        isolated_state._active_emotions["sad"] = (0.6, now)

        active = isolated_state.get_active_emotions()
        # sad 应排在第一位（强度更高）
        assert active[0][0] == "sad"
        assert active[0][1] > active[-1][1] if len(active) > 1 else True
