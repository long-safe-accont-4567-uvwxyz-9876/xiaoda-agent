"""web_search 引擎优先级（Tavily 优先 / Bing 兜底）与复杂关键词稳定性测试。

背景：修复前 Bing 优先，中文专有名词"纳西妲"被误匹配成"纳西族"返回无关结果，
且 Bing 返回非空后不再触发 Tavily 兜底，导致 LLM 拿到垃圾内容反复搜索直至超时降级。
修复后 Tavily 优先、Bing 兜底。本测试用 mock 数据验证该优先级逻辑在复杂关键词下稳定。
"""
import asyncio

import pytest

import tools.web_tools_v2 as wt


def _mock_tavily(results, answer=""):
    """构造 Tavily mock 返回 (results, answer)。注意：真实函数是同步的，
    _do_search 经 asyncio.to_thread 调用，故 mock 必须为同步函数。"""
    def _inner(query, max_results=6, search_depth="basic", news=False):
        return list(results), answer
    return _inner


def _mock_bing(results):
    """构造 Bing mock 返回 results。"""
    def _inner(query, max_results=8):
        return list(results)
    return _inner


@pytest.fixture
def enable_tavily(monkeypatch):
    """确保走 Tavily 分支（设置 key + 强制 _tavily_available 为 True）。"""
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-key")
    monkeypatch.setattr(wt, "_tavily_available", lambda: True)
    yield


# 复杂关键词：含游戏术语、人名、技能名、多个概念词，历史上正是这类查询触发歧义
COMPLEX_QUERIES = [
    "原神 纳西妲 战技 所闻遍计 挂草能力 草元素附着",
    "纳西妲 元素战技 种子 机制 挂草频率 种门 攻略",
    "原神 Nahida 元素战技 蕴种印 灭净三业 挂草",
    "温暖 原神 纳西妲 草神 后台挂草 反应核心",
]


@pytest.mark.parametrize("query", COMPLEX_QUERIES)
def test_tavily_priority_complex_keywords(monkeypatch, enable_tavily, query):
    """Tavily 可用且返回结果时，复杂关键词应走 Tavily，不调用 Bing。"""
    tavily_calls = []
    bing_calls = []

    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        tavily_calls.append(q)
        return [{"title": f"纳西妲攻略-{q[:6]}", "url": "https://example.com/nahida",
                 "content": "所闻遍计施加蕴种印，触发反应产生灭净三业伤害。"}], "AI摘要"

    def fake_bing(q, max_results=8):
        bing_calls.append(q)
        # 模拟旧 bug：Bing 返回纳西族无关结果
        return [{"title": "纳西族", "url": "https://example.com/naxi",
                 "content": "纳西族主要分布于云南。"}]

    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)

    results, engine, answer = asyncio.run(wt._do_search(query, max_results=8))

    assert engine == "Tavily"
    assert results and results[0]["title"].startswith("纳西妲攻略")
    assert answer == "AI摘要"
    # Tavily 有结果时绝不调用 Bing（避免旧 bug 的歧义结果被采用）
    assert bing_calls == []


def test_tavily_empty_falls_back_to_bing(monkeypatch, enable_tavily):
    """Tavily 无结果时回退 Bing。"""
    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        return [], ""

    def fake_bing(q, max_results=8):
        return [{"title": "B站纳西妲攻略", "url": "https://bilibili.com/1",
                 "content": "元素战技详解"}]

    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)

    results, engine, answer = asyncio.run(wt._do_search("原神 纳西妲 战技 所闻遍计 挂草"))

    assert engine == "Bing"
    assert results[0]["title"] == "B站纳西妲攻略"


def test_tavily_exception_falls_back_to_bing(monkeypatch, enable_tavily):
    """Tavily 抛异常时回退 Bing，不中断。"""
    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        raise RuntimeError("tavily api down")

    def fake_bing(q, max_results=8):
        return [{"title": "纳西妲(原神角色)", "url": "https://example.com/1",
                 "content": "草神攻略"}]

    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)

    results, engine, answer = asyncio.run(wt._do_search("纳西妲 战技 所闻遍计 挂草"))

    assert engine == "Bing"
    assert results[0]["title"] == "纳西妲(原神角色)"


def test_time_sensitive_uses_tavily_news(monkeypatch, enable_tavily):
    """时效性查询优先走 Tavily 新闻通道（news=True）。"""
    seen_news = {}

    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        seen_news["news"] = news
        return [{"title": "纳西妲最新攻略", "url": "https://example.com/1",
                 "content": "最新版本草神配队"}], "摘要"

    def fake_bing(q, max_results=8):
        return [{"title": "无关", "url": "https://example.com/x", "content": ""}]

    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)

    results, engine, answer = asyncio.run(wt._do_search("原神 纳西妲 挂草 最新 攻略"))

    assert engine == "Tavily新闻"
    assert seen_news["news"] is True


def test_no_tavily_key_uses_bing(monkeypatch):
    """无 Tavily key 时直接走 Bing，不尝试 Tavily。"""
    tavily_called = []

    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        tavily_called.append(q)
        return [{"title": "不应被调用", "url": "https://example.com/1", "content": ""}], ""

    def fake_bing(q, max_results=8):
        return [{"title": "Bing结果", "url": "https://example.com/2", "content": "内容"}]

    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(wt, "_tavily_available", lambda: False)
    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)

    results, engine, answer = asyncio.run(wt._do_search("纳西妲 原神 挂草"))

    assert engine == "Bing"
    assert tavily_called == []


def test_web_search_complex_keyword_end_to_end(monkeypatch, enable_tavily):
    """web_search 入口在复杂关键词下稳定返回 Tavily 结果（含缓存命中）。"""
    def fake_tavily(q, max_results=6, search_depth="basic", news=False):
        return [{"title": "纳西妲攻略", "url": "https://example.com/1",
                 "content": "所闻遍计挂草详解"}], "AI摘要"

    def fake_bing(q, max_results=8):
        return [{"title": "纳西族", "url": "https://example.com/naxi", "content": "民族"}]

    monkeypatch.setattr(wt, "_tavily_search_sync", fake_tavily)
    monkeypatch.setattr(wt, "_bing_search_sync", fake_bing)
    # 清空缓存，避免历史脏数据污染
    wt._search_cache.clear()
    wt._SEARCH_CACHE_MAX_SIZE = 256

    query = "原神 纳西妲 战技 所闻遍计 挂草能力 草元素附着"
    r = asyncio.run(wt.web_search(query))

    assert r.success is True
    assert "纳西妲攻略" in r.data
    assert "AI摘要" in r.data
    # 结果不应包含旧 bug 的"纳西族"
    assert "纳西族" not in r.data
    # 二次调用命中缓存，结果一致
    r2 = asyncio.run(wt.web_search(query))
    assert r2.data == r.data


def test_clean_query_complex_strip():
    """_clean_query 对复杂关键词做前缀/语气词清理。"""
    cleaned = wt._clean_query("帮我搜索一下原神纳西妲战技所闻遍计的挂草能力呢")
    assert "帮我" not in cleaned
    assert "搜索一下" not in cleaned
    assert cleaned.endswith("挂草能力")
    assert "原神纳西妲战技所闻遍计" in cleaned
