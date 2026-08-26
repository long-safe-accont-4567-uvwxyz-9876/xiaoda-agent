"""Key 提取器单元测试"""

from memory.key_extractor import KeyExtractor


def test_extract_basic():
    ke = KeyExtractor()
    keys = ke.extract("Redis 是一个内存数据库，常用于缓存")
    assert isinstance(keys, list)
    assert len(keys) > 0
    assert "redis" in [k.lower() for k in keys]


def test_extract_filters_stopwords():
    ke = KeyExtractor()
    keys = ke.extract("的 是 一个 了 在 和 与")
    # 全是停用词，应为空
    assert len(keys) == 0


def test_extract_normalizes_synonyms():
    ke = KeyExtractor()
    keys = ke.extract("postgres 性能优化")
    keys_lower = [k.lower() for k in keys]
    assert "postgresql" in keys_lower
    assert "postgres" not in keys_lower


def test_extract_filters_short_words():
    ke = KeyExtractor()
    keys = ke.extract("a b c 数据库")
    # len < 2 的词被过滤
    assert "a" not in keys
    assert "b" not in keys
    assert "数据库" in keys


def test_max_keys_limit():
    ke = KeyExtractor()
    # 生成大量关键词
    text = " ".join([f"关键词{i}" for i in range(50)])
    keys = ke.extract(text)
    assert len(keys) <= KeyExtractor.MAX_KEYS


def test_extract_empty_text():
    ke = KeyExtractor()
    assert ke.extract("") == []
    assert ke.extract("   ") == []


def test_extract_chinese_and_english():
    ke = KeyExtractor()
    keys = ke.extract("Python 编程语言 开发 framework")
    keys_lower = [k.lower() for k in keys]
    assert "python" in keys_lower
    assert "编程语言" in keys or "编程" in keys


def test_extract_query_mode():
    """查询模式：正常提取（is_query 参数存在但不改变基础行为）"""
    ke = KeyExtractor()
    keys = ke.extract("redis 缓存", is_query=True)
    assert isinstance(keys, list)
    assert len(keys) > 0


def test_normalize_mapping_exists():
    assert "postgres" in KeyExtractor.NORMALIZE
    assert KeyExtractor.NORMALIZE["postgres"] == "postgresql"
    assert KeyExtractor.NORMALIZE["前端"] == "frontend"


def test_max_keys_value():
    assert KeyExtractor.MAX_KEYS == 24


def test_role_words_not_hardcoded_but_filtered():
    """2026-08-27 防复发：会话角色词不硬编码在 _STOPWORDS 里，
    而是由 config.KEY_EXTRACTOR_ROLE_WORDS + agent 显示名动态注入。
    提取结果中不得出现角色词（它们 DF 过高，曾致概念图百万边事故）。"""
    from memory.key_extractor import _STOPWORDS, _get_key_stopwords

    ke = KeyExtractor()
    keys = ke.extract("爸爸和小妲还有用户一起吃火锅")
    joined = set(keys)
    for role in ("爸爸", "小妲", "用户"):
        assert role not in _STOPWORDS, f"{role} 不应硬编码在静态表"
        assert role not in joined, f"{role} 应被动态停用词过滤"
    assert "吃火锅" in joined


def test_key_stopwords_include_display_name():
    """动态停用词集应包含 agent 显示名（与 _get_topic_stopwords 同模式）。"""
    from config import get_agent_display_name
    from memory.key_extractor import _get_key_stopwords

    sw = _get_key_stopwords()
    display = get_agent_display_name("xiaoda")
    if display:
        assert display.lower() in sw
