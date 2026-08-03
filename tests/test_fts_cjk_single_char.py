"""回归测试：FTS5 CJK 单字搜索失效 Bug。

根因：_build_fts_query 和 _tokenize_for_fts 使用 min_length=2 过滤 token，
但 jieba 对不在词典中的 CJK 词（如"妲己"）切成单字 ["妲","己"]，
全部被 min_length=2 过滤，导致：
1. _build_fts_query 返回空字符串 → FTS 搜索静默返回空结果
2. _tokenize_for_fts 丢弃不在词典的 CJK 子串 → 索引数据不完整

修复：
- _extract_fts_keywords 补入 bigram（2-gram），确保不在词典中的 CJK 词仍可检索
- 单字 CJK 文本（如实体名"王"）也存入索引
- _build_fts_query 对 CJK 查询使用 min_length=1，单字 token 加前缀通配符
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.fts_utils import _tokenize_for_fts, _build_fts_query, _extract_fts_keywords


# ── _tokenize_for_fts 索引侧测试 ──


def test_tokenize_cjk_single_char_indexed():
    """单字 CJK 文本应存入索引（否则搜'王'永远找不到名为'王'的实体）"""
    result = _tokenize_for_fts("王")
    assert "王" in result.split(), f"单字'王'应被索引，实际: {result!r}"


def test_tokenize_cjk_unknown_word_bigram():
    """不在 jieba 词典中的 CJK 词（如'妲己'）应通过 bigram 补全索引"""
    result = _tokenize_for_fts("妲己")
    tokens = result.split()
    assert "妲己" in tokens, f"'妲己' bigram 应在索引中，实际: {tokens}"


def test_tokenize_cjk_mixed_jieba_and_bigram():
    """jieba 识别的词 + bigram 补全共存"""
    result = _tokenize_for_fts("小妲己")
    tokens = result.split()
    # jieba 识别"小妲己"为完整词
    assert "小妲己" in tokens, f"'小妲己'应在索引中，实际: {tokens}"
    # bigram 补全
    assert "小妲" in tokens, f"'小妲' bigram 应在索引中，实际: {tokens}"
    assert "妲己" in tokens, f"'妲己' bigram 应在索引中，实际: {tokens}"


def test_tokenize_cjk_known_word_unchanged():
    """jieba 能识别的词应正常索引"""
    result = _tokenize_for_fts("苹果手机")
    tokens = result.split()
    assert "苹果" in tokens, f"'苹果'应在索引中，实际: {tokens}"
    assert "手机" in tokens, f"'手机'应在索引中，实际: {tokens}"


def test_tokenize_cjk_dajizh传说():
    """'妲己传说'：jieba 输出单字+词，bigram 补全"""
    result = _tokenize_for_fts("妲己传说")
    tokens = result.split()
    assert "传说" in tokens, f"'传说'应在索引中，实际: {tokens}"
    assert "妲己" in tokens, f"'妲己' bigram 应在索引中，实际: {tokens}"


# ── _build_fts_query 查询侧测试 ──


def test_build_fts_query_cjk_single_char():
    """单字 CJK 查询应生成前缀通配符，非空字符串"""
    result = _build_fts_query("妲")
    assert result != "", "单字 CJK 查询不应返回空字符串"
    assert "妲*" in result, f"单字 CJK 应使用前缀通配符，实际: {result!r}"


def test_build_fts_query_cjk_jieba_single_chars():
    """jieba 切成单字的 CJK 查询（如'妲己'）不应返回空"""
    result = _build_fts_query("妲己")
    assert result != "", "'妲己'查询不应返回空字符串"


def test_build_fts_query_cjk_known_word():
    """jieba 能识别的 CJK 查询应正常"""
    result = _build_fts_query("小妲")
    assert '"小妲"' in result, f"'小妲'应精确匹配，实际: {result!r}"


def test_build_fts_query_non_cjk_unchanged():
    """非 CJK 查询行为不变（min_length=2）"""
    result = _build_fts_query("hello")
    assert '"hello"' in result


def test_build_fts_query_non_cjk_single_char_filtered():
    """非 CJK 单字查询仍被过滤（min_length=2）"""
    result = _build_fts_query("a")
    assert result == "", "英文单字查询应返回空"


# ── SQLite 端到端测试 ──


def _fts_e2e(index_texts: list[str], query: str) -> int:
    """辅助：创建 FTS 索引并搜索，返回命中数"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE t USING fts5(content, tokenize='unicode61')")
    for text in index_texts:
        tokenized = _tokenize_for_fts(text)
        if tokenized.strip():
            conn.execute("INSERT INTO t VALUES(?)", (tokenized,))
    conn.commit()
    fts_query = _build_fts_query(query)
    if not fts_query:
        conn.close()
        return 0
    try:
        rows = conn.execute("SELECT * FROM t WHERE t MATCH ?", (fts_query,)).fetchall()
        conn.close()
        return len(rows)
    except Exception:
        conn.close()
        return 0


def test_e2e_search_single_char_cjk():
    """端到端：搜'妲'能命中包含'小妲己'的记录"""
    hits = _fts_e2e(["小妲己", "苹果手机"], "妲")
    assert hits >= 1, f"搜'妲'应至少命中1条，实际: {hits}"


def test_e2e_search_jieba_single_char_query():
    """端到端：搜'妲己'能命中包含'妲己传说'的记录"""
    hits = _fts_e2e(["妲己传说", "苹果手机"], "妲己")
    assert hits >= 1, f"搜'妲己'应至少命中1条，实际: {hits}"


def test_e2e_search_known_cjk_word():
    """端到端：搜'苹果'能命中"""
    hits = _fts_e2e(["苹果手机"], "苹果")
    assert hits >= 1, f"搜'苹果'应命中，实际: {hits}"


def test_e2e_search_single_char_entity():
    """端到端：搜'王'能命中名为'王'的实体"""
    hits = _fts_e2e(["王", "小李"], "王")
    assert hits >= 1, f"搜'王'应命中，实际: {hits}"


def main():
    tests = [
        test_tokenize_cjk_single_char_indexed,
        test_tokenize_cjk_unknown_word_bigram,
        test_tokenize_cjk_mixed_jieba_and_bigram,
        test_tokenize_cjk_known_word_unchanged,
        test_tokenize_cjk_dajizh传说,
        test_build_fts_query_cjk_single_char,
        test_build_fts_query_cjk_jieba_single_chars,
        test_build_fts_query_cjk_known_word,
        test_build_fts_query_non_cjk_unchanged,
        test_build_fts_query_non_cjk_single_char_filtered,
        test_e2e_search_single_char_cjk,
        test_e2e_search_jieba_single_char_query,
        test_e2e_search_known_cjk_word,
        test_e2e_search_single_char_entity,
    ]
    passed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
            passed += 1
        except Exception as e:
            print(f"  FAIL  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} 通过")
    return 0 if passed == len(tests) else 1


if __name__ == "__main__":
    sys.exit(main())
