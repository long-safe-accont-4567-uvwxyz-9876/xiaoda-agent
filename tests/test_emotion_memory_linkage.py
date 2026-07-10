"""情绪记忆与情绪状态双向联动测试

验证 EmotionalMemoryManager 与 EmotionState 之间的闭环：
1. anchor 情绪记忆时 → 同步更新 emotion_state
2. recall_and_enact 时 → 微调当前情绪 PAD（shift_pad）
3. 联动异常安全：emotion_state 不可用时，anchor/recall_and_enact 仍正常工作
4. CN_TO_EN_MAP：所有 10 个中文标签都有对应英文值
"""
import pytest

import emotion.emotion_state as es
import memory.emotional_memory as em


@pytest.fixture
def isolated_managers(tmp_path, monkeypatch):
    """创建隔离的管理器实例（重置全局单例 + 临时持久化路径）。"""
    # 重置单例
    es._instance = None
    em._emotional_memory_manager = None
    # 设置临时路径
    monkeypatch.setenv("EMOTION_STATE_PATH", str(tmp_path / "emotion_state.json"))
    # 创建管理器
    from memory.emotional_memory import EmotionalMemoryManager
    manager = EmotionalMemoryManager(data_dir=tmp_path)
    yield manager
    # 清理单例，避免影响后续测试
    es._instance = None
    em._emotional_memory_manager = None


# ── 1. anchor 联动 ───────────────────────────────────────────


class TestAnchorLinkage:
    """anchor() 后 emotion_state 被同步更新"""

    def test_anchor_updates_emotion_state(self, isolated_managers, monkeypatch):
        """anchor 喜悦 → emotion_state.get_current() 返回 happy"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
        isolated_managers.anchor(
            user_id="u1",
            event="升职庆祝",
            emotion="喜悦",
            context="今天升职了非常开心",
            keywords=["升职", "开心"],
        )
        emotion, intensity = es.get_emotion_state().get_current()
        assert emotion == "happy"
        assert intensity > 0.0

    def test_anchor_sad_links_to_sad(self, isolated_managers, monkeypatch):
        """anchor 悲伤 → emotion_state 为 sad"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
        isolated_managers.anchor(
            user_id="u1",
            event="丢失钱包",
            emotion="悲伤",
            context="钱包丢了很难过",
            keywords=["钱包", "丢"],
        )
        emotion, _ = es.get_emotion_state().get_current()
        assert emotion == "sad"


# ── 2. recall 联动 ───────────────────────────────────────────


class TestRecallLinkage:
    """recall_and_enact() 后 emotion_state 的 PAD 值被微调"""

    def test_recall_shifts_pad(self, isolated_managers, monkeypatch):
        """recall_and_enact 后 PAD 值发生变化"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
        # anchor 一条带明显 PAD 的记忆（喜悦 → P=0.8, A=0.5, D=0.6）
        isolated_managers.anchor(
            user_id="u1",
            event="工作压力",
            emotion="喜悦",
            context="工作压力 加薪",
            keywords=["工作", "压力"],
        )
        # 记录 anchor 之后的 PAD（anchor 不修改 PAD，应仍为初始值）
        pad_before = es.get_emotion_state().get_pad()
        # 召回并 enact（应触发 shift_pad 微调）
        isolated_managers.recall_and_enact("u1", "工作 压力", user_xp_level=2)
        pad_after = es.get_emotion_state().get_pad()
        # PAD 应发生变化（至少一个维度不同）
        assert pad_after != pad_before

    def test_recall_pad_p_increased_for_positive_emotion(self, isolated_managers, monkeypatch):
        """召回喜悦记忆后 P 维度应正向偏移"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
        isolated_managers.anchor(
            user_id="u1",
            event="工作压力",
            emotion="喜悦",
            context="工作压力 加薪",
            keywords=["工作", "压力"],
        )
        es.get_emotion_state()  # 触发单例创建
        p_before = es.get_emotion_state().get_pad()["P"]
        isolated_managers.recall_and_enact("u1", "工作 压力", user_xp_level=2)
        p_after = es.get_emotion_state().get_pad()["P"]
        # 喜悦 PAD 的 P 为正，微调后 P 应增大
        assert p_after > p_before


# ── 3. 联动异常安全 ──────────────────────────────────────────


class TestLinkageExceptionSafety:
    """emotion_state 不可用时，anchor/recall_and_enact 仍正常工作"""

    def test_anchor_safe_when_emotion_state_broken(self, isolated_managers, monkeypatch):
        """get_emotion_state 抛异常时，anchor 不应抛出"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")

        def broken_get_emotion_state():
            raise RuntimeError("emotion_state unavailable")

        monkeypatch.setattr(
            "emotion.emotion_state.get_emotion_state",
            broken_get_emotion_state,
        )
        # 不应抛出异常
        mem = isolated_managers.anchor(
            user_id="u1",
            event="测试事件",
            emotion="喜悦",
            context="测试上下文",
            keywords=["测试"],
        )
        assert mem.emotion == "喜悦"

    def test_recall_safe_when_emotion_state_broken(self, isolated_managers, monkeypatch):
        """get_emotion_state 抛异常时，recall_and_enact 不应抛出"""
        monkeypatch.setenv("EMOTIONAL_MEMORY_ENABLED", "1")
        isolated_managers.anchor(
            user_id="u1",
            event="工作压力",
            emotion="喜悦",
            context="工作压力 加薪",
            keywords=["工作", "压力"],
        )

        def broken_get_emotion_state():
            raise RuntimeError("emotion_state unavailable")

        monkeypatch.setattr(
            "emotion.emotion_state.get_emotion_state",
            broken_get_emotion_state,
        )
        # 不应抛出异常，且仍返回 enact 文本
        out = isolated_managers.recall_and_enact("u1", "工作 压力", user_xp_level=1)
        assert out.startswith("[情感记忆召回]")


# ── 4. CN_TO_EN_MAP 映射完整性 ──────────────────────────────


class TestCnToEnMap:
    """CN_TO_EN_MAP：所有 10 个中文标签都有对应英文值"""

    EXPECTED = {
        "喜悦": "happy",
        "兴奋": "excited",
        "悲伤": "sad",
        "愤怒": "angry",
        "焦虑": "anxious",
        "害羞": "shy",
        "好奇": "confused",
        "思考": "thinking",
        "恐惧": "fear",
        "平静": "neutral",
    }

    def test_map_importable(self):
        from memory.emotional_memory import CN_TO_EN_MAP
        assert isinstance(CN_TO_EN_MAP, dict)

    def test_all_ten_labels_present(self):
        from memory.emotional_memory import CN_TO_EN_MAP
        for cn, en in self.EXPECTED.items():
            assert cn in CN_TO_EN_MAP, f"缺少中文标签: {cn}"

    def test_all_values_are_english_strings(self):
        from memory.emotional_memory import CN_TO_EN_MAP
        for cn, en in self.EXPECTED.items():
            value = CN_TO_EN_MAP[cn]
            assert isinstance(value, str)
            assert value == en, f"{cn} 期望 {en}，实际 {value}"
            # 英文值应为非空
            assert value, f"{cn} 的英文值为空"

    def test_map_has_exactly_ten_entries(self):
        from memory.emotional_memory import CN_TO_EN_MAP
        assert len(CN_TO_EN_MAP) == 10
