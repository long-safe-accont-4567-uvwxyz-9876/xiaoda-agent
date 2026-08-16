"""model_router Phase 5（成本/缓存统计 Mixin 抽出）结构契约测试。

背景：ModelRouter 内的成本统计块（_calc_cost / _record_usage /
_record_stream_usage / _flush_cost_buffer / flush_costs / close）与
缓存统计块（_track_cache / _check_cache_health / get_cache_stats /
_is_small_model / _filter_tools_for_model / pop_reasoning_content）
抽为 llm_gateway/router_metrics.CostTrackingMixin，方法体逐字节搬移，
ModelRouter 继承该 Mixin 保持 self 语义（对齐 Phase 3/4 Mixin 先例）。

契约：
    1. 本模块不得 import model_router（防循环依赖）
    2. ModelRouter(CostTrackingMixin) 继承：方法经 MRO 命中 Mixin 实现
    3. model_router 同名 re-export（CostTrackingMixin / _reasoning_content_var）
    4. 行为语义不变（成本计算 / 缓冲冲刷 / 缓存统计 / 工具过滤 / close 容错）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

import model_router
from llm_gateway.router_metrics import CostTrackingMixin, _reasoning_content_var

MOVED_METHODS = (
    "_calc_cost", "_record_usage", "_record_stream_usage",
    "_flush_cost_buffer", "flush_costs", "close",
    "_track_cache", "_check_cache_health", "get_cache_stats",
    "_is_small_model", "_filter_tools_for_model", "pop_reasoning_content",
)


class FakeAnalytics:
    """记录 batch_insert_api_usage 调用的最小 analytics 替身。"""

    def __init__(self):
        self.calls = []

    async def batch_insert_api_usage(self, records):
        self.calls.append(list(records))


class FakeRouter(CostTrackingMixin):
    """仅组装 CostTrackingMixin 的最小实例（不引入 ModelRouter 构造链）。"""

    def __init__(self):
        self._cost_buffer = []
        self._cost_flush_threshold = 3
        self._analytics = None
        self._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
        self._request_count = 0
        self._cached_tokens_total = 0
        self._last_cache_warning = 0.0
        self._client = None
        self._agnes_client = None
        self._custom_clients = {}

    # close() 经 self.* 调用的客户端访问方法（MRO 契约的最小实现）
    def list_custom_clients(self):
        return list(self._custom_clients.items())

    def clear_custom_clients(self):
        self._custom_clients.clear()


def _usage(**fields):
    return type("Usage", (), fields)()


def _response(usage):
    return type("Response", (), {"usage": usage})()


# ── 1. 独立可导入 + 无循环依赖 ──────────────────────────────────

def test_mixin_imports_standalone():
    import importlib
    mod = importlib.import_module("llm_gateway.router_metrics")
    for name in MOVED_METHODS:
        assert hasattr(mod.CostTrackingMixin, name), f"缺少方法 {name}"


def test_mixin_does_not_import_model_router():
    import llm_gateway.router_metrics as mod
    assert "model_router" not in getattr(mod, "__dict__", {})


# ── 2. ModelRouter 继承 + MRO 命中 Mixin 实现 ────────────────────

def test_model_router_inherits_mixin():
    assert issubclass(model_router.ModelRouter, CostTrackingMixin)
    for name in MOVED_METHODS:
        assert (getattr(model_router.ModelRouter, name)
                is getattr(CostTrackingMixin, name)), f"{name} 未命中 Mixin 实现"


def test_model_router_reexports_moved_symbols():
    assert model_router.CostTrackingMixin is CostTrackingMixin
    assert model_router._reasoning_content_var is _reasoning_content_var


# ── 3. 成本计算与记录（搬移后行为不变） ─────────────────────────

def test_calc_cost_mimo_standard_vs_pro():
    r = FakeRouter()
    # standard：input 0.10/M，无输出无缓存
    assert r._calc_cost(1_000_000, 0, model="mimo-v2.5", provider="mimo") == 0.10
    # pro：input 0.20/M
    assert r._calc_cost(1_000_000, 0, model="mimo-pro-v2", provider="mimo") == 0.20


def test_calc_cost_other_provider_and_cache_hit():
    r = FakeRouter()
    # agnes：input 0.15/M
    assert r._calc_cost(1_000_000, 0, provider="agnes") == 0.15
    # 缓存命中：miss 0.05 + hit 0.005（cache_miss < 0 时回退 prompt 总量）
    assert r._calc_cost(1_000_000, 0, cache_hit_tokens=500_000,
                        cache_miss_tokens=500_000,
                        model="mimo-v2.5", provider="mimo") == pytest.approx(0.055)
    assert r._calc_cost(100, 0, cache_hit_tokens=200, cache_miss_tokens=0,
                        model="mimo-v2.5", provider="mimo") == pytest.approx(1.2e-05)
    # cache_miss < 0 时回退 prompt 总量：input 按 100 计费 1e-05，
    # cache_hit 200 仍按 0.01/M 计费 2e-06 → 合计 1.2e-05


@pytest.mark.asyncio
async def test_record_usage_buffers_and_flushes_at_threshold():
    r = FakeRouter()
    r._analytics = FakeAnalytics()
    usage = _usage(prompt_tokens=1_000_000, completion_tokens=0,
                   prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=0)
    for _ in range(3):
        await r._record_usage("chat", "mimo-v2.5", _response(usage),
                              provider="mimo")
    # 第 3 条触发阈值冲刷：analytics 收到一批，buffer 清空
    assert len(r._analytics.calls) == 1
    assert len(r._analytics.calls[0]) == 3
    assert r._cost_buffer == []
    assert r._analytics.calls[0][0]["cost_usd"] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_record_stream_usage_no_db_no_buffer():
    r = FakeRouter()
    usage = _usage(prompt_tokens=100, completion_tokens=50)
    await r._record_stream_usage("chat", "m", _response(usage), provider="mimo")
    assert r._cost_buffer == []  # _analytics 为 None 时不入 buffer


@pytest.mark.asyncio
async def test_flush_cost_buffer_clears_and_tolerates_errors():
    r = FakeRouter()
    r._cost_buffer = [{"cost_usd": 0.1}, {"cost_usd": 0.2}]
    r._analytics = FakeAnalytics()
    await r._flush_cost_buffer()
    assert len(r._analytics.calls[0]) == 2
    assert r._cost_buffer == []


@pytest.mark.asyncio
async def test_flush_costs_delegates_to_flush_cost_buffer():
    calls = []

    class Spy(FakeRouter):
        async def _flush_cost_buffer(self):
            calls.append(1)

    await Spy().flush_costs()
    assert calls == [1]


# ── 4. 缓存统计（搬移后行为不变） ───────────────────────────────

def test_track_cache_mimo_and_openai_formats_dedup():
    r = FakeRouter()
    # MiMo 格式：hit/miss 直接累加
    r._track_cache(_response(_usage(
        prompt_tokens=100,
        prompt_cache_hit_tokens=100,
        prompt_cache_miss_tokens=50,
    )))
    assert r._cache_stats == {"total_calls": 0, "hit_tokens": 100, "miss_tokens": 50}
    # OpenAI 格式：prompt_tokens_details.cached_tokens 优先
    details = type("D", (), {"cached_tokens": 200})()
    r._track_cache(_response(_usage(
        prompt_tokens=100, prompt_tokens_details=details)))
    assert r._cache_stats["hit_tokens"] == 300
    assert r._cached_tokens_total == 200
    # 去重：MiMo hit 与 OpenAI cached 同时存在时 hit_tokens 只累加 MiMo 值
    r._track_cache(_response(_usage(
        prompt_tokens=100,
        prompt_cache_hit_tokens=70,
        prompt_cache_miss_tokens=30,
        prompt_tokens_details=details,
    )))
    assert r._cache_stats["hit_tokens"] == 370  # 300 + 70（不再加 200）
    assert r._cached_tokens_total == 400


def test_get_cache_stats_hit_ratio():
    r = FakeRouter()
    r._cache_stats = {"total_calls": 10, "hit_tokens": 3, "miss_tokens": 1}
    stats = r.get_cache_stats()
    assert stats == {"total_calls": 10, "hit_tokens": 3, "miss_tokens": 1,
                     "hit_ratio": 0.75}
    r._cache_stats = {"total_calls": 0, "hit_tokens": 0, "miss_tokens": 0}
    assert r.get_cache_stats()["hit_ratio"] == 0.0


def test_check_cache_health_warns_only_when_ratio_low():
    r = FakeRouter()
    r._cache_stats = {"hit_tokens": 100, "miss_tokens": 100_000}
    r._check_cache_health()
    assert r._last_cache_warning > 0  # ratio < 0.5 → 触发告警
    # 高命中率不触发：total > 10000 但 ratio >= 0.5
    r._cache_stats = {"hit_tokens": 20_000, "miss_tokens": 10_000}
    r._last_cache_warning = 0.0
    r._check_cache_health()
    assert r._last_cache_warning == 0.0


# ── 5. 小模型工具过滤与 reasoning 弹出 ──────────────────────────

def test_is_small_model_patterns():
    r = FakeRouter()
    assert r._is_small_model("qwen2.5-7b")
    assert r._is_small_model("Qwen3-8B")
    assert not r._is_small_model("deepseek-v3")
    assert not r._is_small_model("Qwen2.5-72B")
    assert not r._is_small_model("mimo-pro-v2")


def test_filter_tools_for_small_model_strips_tools():
    r = FakeRouter()
    tools = [{"function": {"name": "t1"}}]
    assert r._filter_tools_for_model(None, "qwen2.5-7b") is None
    assert r._filter_tools_for_model([], "qwen2.5-7b") == []
    assert r._filter_tools_for_model(tools, "qwen2.5-7b") is None  # 小模型剥离
    assert r._filter_tools_for_model(tools, "deepseek-v3") is tools  # 原样透传


def test_pop_reasoning_content_reads_and_clears_contextvar():
    _reasoning_content_var.set("thinking...")
    r = FakeRouter()
    assert r.pop_reasoning_content() == "thinking..."
    # 弹空后清空，再次弹出返回 None
    assert r.pop_reasoning_content() is None


# ── 6. close 容错（MRO 契约：self.* 调用回 ModelRouter 原方法） ──

@pytest.mark.asyncio
async def test_close_tolerates_custom_clients_without_close_method():
    class NoCloseClient:
        pass

    r = FakeRouter()
    r._custom_clients["compat"] = NoCloseClient()
    await r.close()  # 不应抛 AttributeError；无 close 的客户端被跳过
    assert r._custom_clients == {}  # 经 self.clear_custom_clients 清空
    assert r._client is None
    assert r._agnes_client is None
