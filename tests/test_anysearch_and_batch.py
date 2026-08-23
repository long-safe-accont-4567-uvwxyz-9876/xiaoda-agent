# tests/test_anysearch_and_batch.py — AnySearch 引擎接入与批量搜索工具
"""覆盖：信封解析、401 不降级匿名、熔断器、引擎链回退、批量工具校验与并行。"""
import asyncio

import pytest

import tools.anysearch_client as acs
import tools.web_tools_v2 as wt


# ---------------------------------------------------------------- AnySearch 客户端
def _envelope(results=None, answer="", code=0, error_code=""):
    data = {"code": code, "message": "success" if code == 0 else "fail",
            "request_id": "req-test-uuid"}
    if code == 0:
        data["data"] = {"results": results or [], "answer": answer}
    else:
        data["error_code"] = error_code
    return data


@pytest.fixture
def anysearch_on(monkeypatch):
    monkeypatch.setenv("ANYSEARCH_API_KEY", "as_sk_test_key")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(wt, "_tavily_available", lambda: False)
    acs._fail_streak = 0
    acs._break_until = 0.0
    yield
    acs._fail_streak = 0
    acs._break_until = 0.0


def test_envelope_success_and_field_mapping(anysearch_on, monkeypatch):
    monkeypatch.setattr(acs, "_http_post_json",
                        lambda path, payload: _envelope(
                            results=[{"title": "t", "url": "https://a",
                                      "snippet": "摘要", "content": "正文"}],
                            answer="AI答案"))
    results, answer = acs.anysearch_search_sync("q")
    assert results[0]["title"] == "t" and results[0]["url"] == "https://a"
    assert results[0]["content"] == "正文"  # content 优先，snippet 回退
    assert answer == "AI答案"


def test_top_level_results_envelope_compat(anysearch_on, monkeypatch):
    monkeypatch.setattr(
        acs, "_http_post_json",
        lambda path, payload: {"code": 0, "request_id": "r",
                               "results": [{"title": "x", "url": "https://b",
                                            "snippet": "s"}]})
    results, _ = acs.anysearch_search_sync("q")
    assert results[0]["content"] == "s"


def test_envelope_error_raises(anysearch_on, monkeypatch):
    monkeypatch.setattr(acs, "_http_post_json",
                        lambda path, payload: _envelope(code=-1, error_code="rate_limited"))
    with pytest.raises(RuntimeError, match="rate_limited"):
        acs.anysearch_search_sync("q")


def test_auth_error_no_anonymous_fallback(anysearch_on, monkeypatch):
    """401/403 上抛 AnySearchAuthError（PermissionError 子类），不静默降级匿名。"""
    def _raise(path, payload):
        raise acs.AnySearchAuthError("anysearch key 无效或被拒(status=401)")
    monkeypatch.setattr(acs, "_http_post_json", _raise)
    with pytest.raises(PermissionError):
        acs.anysearch_search_sync("q")


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_ENABLED", raising=False)
    assert acs.anysearch_available() is False


def test_enabled_flag_without_key(monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.setenv("ANYSEARCH_ENABLED", "true")
    assert acs.anysearch_available() is True


def test_breaker_opens_after_streak(anysearch_on, monkeypatch):
    """连续失败 3 次熔断：available() 变 False，冷却期后恢复半开。"""
    calls = []

    def _fail(path, payload):
        calls.append(path)
        raise acs.AnySearchAuthError("denied")

    monkeypatch.setattr(acs, "_http_post_json", _fail)
    for _ in range(3):
        with pytest.raises(PermissionError):
            acs.anysearch_search_sync("q")
    assert acs.anysearch_available() is False  # 熔断打开
    with pytest.raises(ConnectionError, match="熔断"):
        acs.anysearch_search_sync("q")  # 熔断期直接拒绝，不再发请求
    assert len(calls) == 3
    # 冷却结束（把 _break_until 拨回过去）→ 半开可用
    acs._break_until = 0.0
    assert acs.anysearch_available() is True


# ---------------------------------------------------------------- 引擎链集成
def _mk_bing(results):
    def _inner(query, max_results=8):
        return list(results)
    return _inner


def test_do_search_uses_anysearch_first(anysearch_on, monkeypatch):
    monkeypatch.setattr(wt, "anysearch_available", lambda: True)
    monkeypatch.setattr(acs, "_http_post_json",
                        lambda path, payload: _envelope(
                            results=[{"title": "来自AnySearch", "url": "https://as",
                                      "snippet": "内容"}]))
    monkeypatch.setattr(wt, "anysearch_search_sync", acs.anysearch_search_sync)
    monkeypatch.setattr(wt, "_bing_search_sync", _mk_bing(
        [{"title": "Bing结果", "url": "https://b", "content": ""}]))
    results, engine, _ = asyncio.run(wt._do_search("查询", max_results=5))
    assert engine == "AnySearch" and results[0]["title"] == "来自AnySearch"


def test_do_search_falls_back_on_anysearch_error(anysearch_on, monkeypatch):
    """AnySearch 失败（信封错）→ 回退 Bing；认证失败同样回退。"""
    monkeypatch.setattr(wt, "anysearch_available", lambda: True)

    def _fail(path, payload):
        raise RuntimeError("anysearch error_code=boom")

    monkeypatch.setattr(acs, "_http_post_json", _fail)
    monkeypatch.setattr(wt, "anysearch_search_sync", acs.anysearch_search_sync)
    monkeypatch.setattr(wt, "_bing_search_sync", _mk_bing(
        [{"title": "Bing兜底", "url": "https://b", "content": "c"}]))
    results, engine, _ = asyncio.run(wt._do_search("查询", max_results=5))
    assert engine == "Bing" and results[0]["title"] == "Bing兜底"


def test_do_search_skips_anysearch_when_unavailable(monkeypatch):
    monkeypatch.delenv("ANYSEARCH_API_KEY", raising=False)
    monkeypatch.delenv("ANYSEARCH_ENABLED", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(wt, "_tavily_available", lambda: False)
    monkeypatch.setattr(wt, "anysearch_available", acs.anysearch_available)
    called = []
    monkeypatch.setattr(wt, "anysearch_search_sync",
                        lambda q, m=8: called.append(q) or ([], ""))
    monkeypatch.setattr(wt, "_bing_search_sync", _mk_bing(
        [{"title": "B", "url": "https://b", "content": "c"}]))
    _, engine, _ = asyncio.run(wt._do_search("查询", max_results=5))
    assert engine == "Bing" and called == []  # 未开启时零调用


# ---------------------------------------------------------------- 批量工具
def _prime_cache_safe_bing(monkeypatch, titles=("T",)):
    wt._search_cache.clear()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(wt, "_tavily_available", lambda: False)
    monkeypatch.setattr(wt, "anysearch_available", lambda: False)
    monkeypatch.setattr(wt, "_bing_search_sync", _mk_bing(
        [{"title": t, "url": f"https://b/{i}", "content": "c"}
         for i, t in enumerate(titles)]))


def test_batch_parallel_queries(monkeypatch):
    _prime_cache_safe_bing(monkeypatch)
    out = asyncio.run(wt.web_search_batch(["苹果 价格", "华为 价格", "小米 价格"]))
    assert out.success
    text = out.data
    assert "并行搜索 3 个意图" in text
    for q in ("苹果", "华为", "小米"):
        assert q in text


def test_batch_validation(monkeypatch):
    _prime_cache_safe_bing(monkeypatch)
    assert not asyncio.run(wt.web_search_batch(["只有一个"])).success
    assert not asyncio.run(wt.web_search_batch(
        [f"q{i}" for i in range(6)])).success
    assert not asyncio.run(wt.web_search_batch("不是数组")).success


def test_batch_partial_failure(monkeypatch):
    wt._search_cache.clear()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setattr(wt, "_tavily_available", lambda: False)
    monkeypatch.setattr(wt, "anysearch_available", lambda: False)

    def _bing(query, max_results=8):
        return [{"title": "ok", "url": "https://ok", "content": "c"}] \
            if query == "能命中" else []

    monkeypatch.setattr(wt, "_bing_search_sync", _bing)
    out = asyncio.run(wt.web_search_batch(["能命中", "不能命中"]))
    assert out.success  # 部分成功仍返回
    assert "【查询】能命中" in out.data and "（失败）" in out.data


def test_web_search_single_still_works(monkeypatch):
    _prime_cache_safe_bing(monkeypatch, titles=("单查",))
    out = asyncio.run(wt.web_search("单查关键词"))
    assert out.success and "单查" in out.data
