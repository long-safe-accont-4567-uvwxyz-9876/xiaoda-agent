"""审计报告批量修复测试

验证 Terminal#804-918 中 CRITICAL 和 HIGH 级别 BUG 的修复。
"""
import inspect
import os
import time


class TestC1EpisodicMemoriesDDL:
    """C-1: episodic_memories DDL 应包含 content_hash 和 version 列"""

    def test_ddl_has_content_hash(self):
        from db import database
        source = inspect.getsource(database)
        assert "content_hash" in source, "episodic_memories DDL应包含content_hash列"

    def test_ddl_has_version(self):
        from db import database
        source = inspect.getsource(database)
        assert "version INTEGER DEFAULT 1" in source, "episodic_memories DDL应包含version列"

    def test_schema_sql_has_content_hash(self):
        schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
        with open(schema_path, encoding="utf-8") as f:
            content = f.read()
        assert "content_hash" in content, "schema.sql应包含content_hash"


class TestC2KnowledgeRelationsCreated:
    """C-2: knowledge_relations DDL 应包含 created_at 字段"""

    def test_ddl_has_created_at(self):
        from db import database
        source = inspect.getsource(database)
        # 检查 knowledge_relations 表定义中包含 created_at
        assert "created_at REAL DEFAULT 0" in source, "knowledge_relations DDL应包含created_at"


class TestC4DreamImportanceNotHardcoded:
    """C-4: dream_consolidation 不应硬编码 importance=0.5"""

    def test_uses_row_get_for_importance(self):
        from core import dream_consolidation
        source = inspect.getsource(dream_consolidation)
        # 应使用 row.get("importance", ...) 而非硬编码 0.5
        assert 'importance=row.get("importance"' in source or "importance=row.get('importance'" in source, \
            "应使用row.get('importance')而非硬编码0.5"


class TestH1BoostCapped:
    """H-1: FluidMemory boost 有上限"""

    def test_max_boost_constant_exists(self):
        from memory.fluid_memory import FluidMemory
        # FluidMemory v0.6 使用稳定性模型替代 boost 上限
        assert hasattr(FluidMemory, "STABILITY_BASE_DAYS"), "FluidMemory应有STABILITY_BASE_DAYS常量"

    def test_high_access_doesnt_exceed_new_memory(self):
        from memory.fluid_memory import FluidMemory
        fm = FluidMemory()
        now = time.time()
        new_score = fm.score(similarity=1.0, created_at=now, access_count=0)
        old_score = fm.score(similarity=0.3, created_at=now - 365*86400, access_count=1000)
        assert new_score > old_score, "新记忆应比高频访问的旧记忆分数高"


class TestH2CreatedAtNotLastAccess:
    """H-2: dream_consolidation 应用 created_at 而非 last_access 作为衰减基准"""

    def test_uses_created_at_for_scoring(self):
        from core import dream_consolidation
        source = inspect.getsource(dream_consolidation)
        # 不应使用 created_at=m.last_access
        assert "created_at=m.last_access" not in source, \
            "不应使用last_access作为created_at"


class TestH3PrefixClustering15:
    """H-3: 前缀聚类应使用15字符而非30字符"""

    def test_prefix_length_is_15(self):
        from core import dream_consolidation
        source = inspect.getsource(dream_consolidation)
        assert "[:15]" in source, "前缀聚类应使用15字符"
        assert "[:30]" not in source, "不应使用30字符前缀"


class TestH4SingleChannelRrfScore:
    """H-4: 单通道检索结果应补充 rrf_score"""

    def test_single_channel_has_rrf_score(self):
        from memory import memory_manager
        source = inspect.getsource(memory_manager)
        # 单通道路径应设置 rrf_score
        assert "rrf_score" in source, "单通道结果应补充rrf_score字段"


class TestH6CjkNormalize:
    """H-6: _normalize_for_dedupe 应处理 CJK 标点"""

    def test_removes_cjk_punctuation(self):
        from memory.memory_manager import _normalize_for_dedupe
        # CJK标点应被去除
        result = _normalize_for_dedupe("你好，世界。")
        assert "，" not in result, "应去除中文逗号"
        assert "。" not in result, "应去除中文句号"

    def test_preserves_cjk_content(self):
        from memory.memory_manager import _normalize_for_dedupe
        result = _normalize_for_dedupe("你好世界")
        assert "你好世界" in result, "应保留中文内容"

    def test_dedupe_with_punctuation(self):
        from memory.memory_manager import _normalize_for_dedupe
        # 带标点和不带标点应归一化为相同结果
        a = _normalize_for_dedupe("今天天气真好！")
        b = _normalize_for_dedupe("今天天气真好")
        assert a == b, f"带标点和不带标点应相同: '{a}' vs '{b}'"


class TestH8GreetingThresholdFromConfig:
    """H-8: greeting_threshold 应从 config_service 读取"""

    def test_reads_threshold_from_config(self):
        from emotion import nudge_engine
        source = inspect.getsource(nudge_engine)
        assert "schedule.greeting_threshold" in source, \
            "应从config_service读取greeting_threshold"


class TestH11CuriousAlias:
    """H-11: "好奇" 应映射到 CURIOUS 而非 CONFUSED"""

    def test_haocurious_maps_to_curious(self):
        from emotion.emotion_enum import EMOTION_ALIASES, Emotion
        assert EMOTION_ALIASES.get("好奇") == Emotion.CURIOUS, \
            "'好奇'应映射到CURIOUS"

    def test_curious_maps_to_curious(self):
        from emotion.emotion_enum import EMOTION_ALIASES, Emotion
        assert EMOTION_ALIASES.get("curious") == Emotion.CURIOUS, \
            "'curious'应映射到CURIOUS"

    def test_confused_not_mapped_from_haoqi(self):
        from emotion.emotion_enum import EMOTION_ALIASES, Emotion
        assert EMOTION_ALIASES.get("好奇") != Emotion.CONFUSED, \
            "'好奇'不应映射到CONFUSED"


class TestH14ExceptionReturnsDeny:
    """H-14: 异常时应返回 deny 而非 allow"""

    def test_exception_returns_deny(self):
        from tool_engine import tool_guardrails
        source = inspect.getsource(tool_guardrails)
        # 异常处理应返回 deny
        assert 'return "deny"' in source, "异常时应返回deny"


class TestH17WalMode:
    """H-17: belief_router 应启用 WAL 模式"""

    def test_load_from_db_has_wal(self):
        from belief_router import BeliefRouter
        source = inspect.getsource(BeliefRouter)
        assert "PRAGMA journal_mode=WAL" in source, \
            "BeliefRouter应启用WAL模式"
