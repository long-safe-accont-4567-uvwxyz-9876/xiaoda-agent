"""事实类记忆永久化测试。

验证含生日/电话/地址等关键词的记忆在写入时直接置 PERMANENT，
跳过 BUFFER/DECAY 衰减，避免关键事实被遗忘。
"""
from memory.fsrs_model import (
    S_PERMANENT,
    MemoryPhase,
    MemoryState,
    is_fact_memory,
    should_be_permanent_on_create,
)


class TestFactMemoryDetection:
    """事实类记忆关键词检测。"""

    def test_birthday_is_fact(self):
        assert is_fact_memory("我的生日是3月15日") is True

    def test_phone_is_fact(self):
        assert is_fact_memory("电话号码13800138000") is True

    def test_address_is_fact(self):
        assert is_fact_memory("我家住址是北京海淀区") is True

    def test_name_is_fact(self):
        assert is_fact_memory("我的名字是小妲") is True

    def test_date_is_fact(self):
        assert is_fact_memory("纪念日日期是2026年1月1日") is True

    def test_number_is_fact(self):
        assert is_fact_memory("身份证号码110105199001011234") is True

    def test_preference_not_fact(self):
        """偏好类不应触发事实永久（'喜欢'不在 _FACT_KEYWORDS）"""
        assert is_fact_memory("我喜欢吃苹果") is False

    def test_abstract_not_fact(self):
        """抽象类不应触发事实永久"""
        assert is_fact_memory("因为这个原理很重要") is False

    def test_empty_string(self):
        assert is_fact_memory("") is False

    def test_should_be_permanent_fact(self):
        assert should_be_permanent_on_create("我生日是5月1日") is True

    def test_should_be_permanent_non_fact(self):
        assert should_be_permanent_on_create("今天天气不错") is False


class TestPermanentStateRetrievability:
    """永久态记忆的 retrievability 应恒为 1.0（不衰减）。"""

    def test_permanent_retrievability_always_one(self):
        state = MemoryState(
            difficulty=5.0,
            stability=S_PERMANENT,
            phase=MemoryPhase.PERMANENT,
            last_review=0.0,  # 很久以前
            created_at=0.0,
            reinforcement_count=1,
        )
        # 即使 last_review=0（1970年），PERMANENT 相 R 仍为 1.0
        now = 1_800_000_000.0  # 2027 年
        assert state.retrievability(now) == 1.0

    def test_permanent_transition_stays_permanent(self):
        """PERMANENT 相经 transition 后仍为 PERMANENT。"""
        state = MemoryState(
            difficulty=5.0,
            stability=S_PERMANENT,
            phase=MemoryPhase.PERMANENT,
            last_review=0.0,
            created_at=0.0,
            reinforcement_count=1,
        )
        assert state.transition(1_800_000_000.0) == MemoryPhase.PERMANENT


class TestFactPermanentInitValues:
    """事实类记忆写入时应使用的初始值（供 _memory_encoder 调用）。"""

    def test_fact_memory_init_values(self):
        """模拟 _memory_encoder 对事实类记忆的初始化逻辑。"""
        content = "我生日是5月1日"
        if should_be_permanent_on_create(content):
            init_stability = S_PERMANENT
            init_phase = "permanent"
            init_rc = 1
        else:
            init_stability = 3.0
            init_phase = "buffer"
            init_rc = 0

        assert init_stability == S_PERMANENT
        assert init_phase == "permanent"
        assert init_rc == 1

    def test_non_fact_memory_init_values(self):
        """非事实类记忆仍走 buffer 初始化。"""
        content = "今天心情不错"
        if should_be_permanent_on_create(content):
            init_stability = S_PERMANENT
            init_phase = "permanent"
            init_rc = 1
        else:
            init_stability = 3.0
            init_phase = "buffer"
            init_rc = 0

        assert init_stability == 3.0
        assert init_phase == "buffer"
        assert init_rc == 0
