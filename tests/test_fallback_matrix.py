"""兜底矩阵与降级标记回归测试（严重-2 + 轻微-15，2026-11-08）。

覆盖：
  1. 矩阵从 config/provider_metadata.json + ROUTE_TABLE 派生后的取值
  2. 同家兜底档命中（主模型不是 agnes 时也走同家档，不直接掉免费车道）
  3. 同家档不可用才进免费 9B 车道，且一定带降级标记 kind=free_lane
  4. 跨家白名单开关：默认关闭 / 打开两种行为
  5. 现网 agnes/agnes-2.5-flash 兜底行为不变（chat → chat_agnes，单次）
  6. 反向证明：等同家档缺失（旧实现）时同一路径直接掉免费车道且无同家档调用
  7. model_used 落库值带降级标记（未降级时保持旧语义）
"""
import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import llm_gateway.fallback_chain as fb_mod  # noqa: E402
from llm_gateway.fallback_chain import FallbackChainMixin  # noqa: E402


class _FakeClassifier:
    def classify(self, error):
        return SimpleNamespace(
            reason=SimpleNamespace(value="unknown"),
            action=SimpleNamespace(value="backoff_retry"),
        )


class _FakeRouter(FallbackChainMixin):
    """最小可跑的降级链宿主：registry / 客户端可用性 / 路由调用全部可控。"""

    def __init__(self, *, provider="agnes", model="agnes-2.5-flash",
                 unavailable=(), results=None, custom_clients=()):
        self._error_classifier = _FakeClassifier()
        self._unavailable = set(unavailable)
        self._results = dict(results or {})
        self._custom_clients = list(custom_clients)
        self.calls = []
        self._orig = {"client": provider, "model": model, "max_tokens": 32768}
        self._snapshots = {
            "chat_agnes": {"client": "agnes", "model": "agnes-2.5-flash", "max_tokens": 65535},
        }
        self._registry = SimpleNamespace(
            get_task=lambda _task: dict(self._orig),
            snapshot_task=lambda task: dict(self._snapshots.get(task, self._orig)),
        )

    def _is_client_configured(self, provider):
        return provider not in self._unavailable

    def _filter_tools_for_model(self, tools, _model):
        return tools

    def list_custom_clients(self):
        return list(self._custom_clients)

    def _get_custom_provider_default_model(self, _provider):
        return ""

    def get_current_chat_model(self):
        return {"provider": self._orig["client"], "model_id": self._orig["model"]}

    async def _route_with_retry(self, task_type, config, *args, **kwargs):
        self.calls.append(task_type)
        if task_type in self._results:
            return self._results[task_type]
        raise RuntimeError(f"no result for {task_type}")


async def _run(router):
    return await router._try_fallback_chain(
        RuntimeError("primary failed"), "chat", [], 0.7, False,
        None, None, 30, "u", "s", None,
    )


# ── 1. 矩阵派生 ───────────────────────────────────────────────────
def test_matrix_derivation_from_metadata():
    from model_router_config import build_fallback_matrix

    route_table = {
        "chat": {"client": "agnes", "model": "agnes-2.5-flash", "max_tokens": 65535},
        "chat_agnes": {"client": "agnes", "model": "agnes-2.5-flash", "max_tokens": 65535},
        "emotion_analysis": {"client": "agnes", "model": "agnes-2.5-flash"},
    }
    matrix = build_fallback_matrix(route_table, {"chat": "chat_agnes"})

    # 只为声明过降级链的 task 建矩阵
    assert set(matrix) == {"chat"}
    # agnes 档取路由表里的同家 task（现网行为逐字保留）
    agnes_tier = matrix["chat"]["agnes"]
    assert agnes_tier["task"] == "chat_agnes"
    assert agnes_tier["source"] == "route_table"
    assert agnes_tier["max_tokens"] == 65535
    # 其它 provider 从 provider_metadata.json 的 default_model 派生
    assert matrix["chat"]["deepseek"]["model"] == "deepseek-chat"
    assert matrix["chat"]["deepseek"]["source"] == "provider_metadata"
    assert matrix["chat"]["mimo"]["model"] == "mimo-v2.5"


def test_live_matrix_is_injected_and_covers_current_providers():
    import model_router  # noqa: F401  触发注入
    from model_router_config import get_fallback_matrix, get_fallback_tier

    matrix = get_fallback_matrix()
    assert "chat" in matrix, "model_router 导入后应已注入兜底矩阵"
    assert matrix["chat"]["agnes"]["task"] == "chat_agnes"
    tier = get_fallback_tier("chat", "deepseek")
    assert tier and tier["model"] == "deepseek-chat"
    # 未声明降级链的 task 不进矩阵（继续走旧 task 链，行为不变）
    assert get_fallback_tier("emotion_analysis", "agnes") is None


def test_matrix_does_not_break_fallback_chain_import_contract():
    """矩阵所在模块不能反过来让 fallback_chain 违反「不得 import model_router」契约。"""
    source = inspect.getsource(fb_mod)
    assert "import model_router\n" not in source
    assert "from model_router import" not in source


# ── 2. 同家兜底档命中 ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_same_provider_tier_hit_for_non_agnes_primary():
    router = _FakeRouter(provider="mimo", model="mimo-v2.5",
                         results={"chat_mimo": "tier-ok"})
    assert await _run(router) == "tier-ok"
    assert router.calls == ["chat_mimo"], "非 agnes 主模型应先走自己的同家档"
    degrade = router.get_last_degrade()
    assert degrade is not None and degrade["degraded"] is True
    assert degrade["kind"] == "same_provider_tier"
    assert (degrade["to_provider"], degrade["to_model"]) == ("mimo", "mimo-v2.5")
    assert degrade["source"] == "provider_metadata"


@pytest.mark.asyncio
async def test_agnes_existing_behaviour_unchanged():
    """现网 agnes/agnes-2.5-flash：chat 失败 → chat_agnes，且只尝试一次。"""
    router = _FakeRouter(provider="agnes", model="agnes-2.5-flash",
                         results={"chat_agnes": "agnes-ok"})
    assert await _run(router) == "agnes-ok"
    assert router.calls == ["chat_agnes"]
    degrade = router.get_last_degrade()
    assert degrade["kind"] == "same_provider_tier"
    assert degrade["source"] == "route_table"


# ── 3. 免费车道收紧 + 标记 ────────────────────────────────────────
@pytest.mark.asyncio
async def test_free_lane_only_after_tier_unavailable_and_marked():
    router = _FakeRouter(provider="deepseek", model="deepseek-chat",
                         unavailable={"deepseek"}, results={"chat_free": "free-ok"})
    assert await _run(router) == "free-ok"
    assert router.calls == ["chat_free"]
    degrade = router.get_last_degrade()
    assert degrade["kind"] == "free_lane"
    assert degrade["to_provider"] == "siliconflow"
    assert degrade["source"] == "free_emergency_fallback"


@pytest.mark.asyncio
async def test_free_lane_not_entered_when_tier_available():
    router = _FakeRouter(provider="deepseek", model="deepseek-chat",
                         results={"chat_deepseek": "tier-ok", "chat_free": "free-ok"})
    assert await _run(router) == "tier-ok"
    assert "chat_free" not in router.calls


@pytest.mark.asyncio
async def test_free_lane_directly_after_tier_failure():
    """同家档存在但调用失败 → 仍进免费车道，但标记是免费的 kind。"""
    router = _FakeRouter(provider="deepseek", model="deepseek-chat",
                         results={"chat_free": "free-ok"})
    assert await _run(router) == "free-ok"
    assert router.calls == ["chat_deepseek", "chat_free"]
    assert router.get_last_degrade()["kind"] == "free_lane"


# ── 4. 跨家白名单开关 ─────────────────────────────────────────────
def test_cross_provider_policy_defaults_off(monkeypatch):
    from model_router_config import load_cross_provider_fallback_policy

    monkeypatch.delenv("CROSS_PROVIDER_FALLBACK_POLICY", raising=False)
    assert load_cross_provider_fallback_policy() is False
    monkeypatch.setenv("CROSS_PROVIDER_FALLBACK_POLICY", "1")
    assert load_cross_provider_fallback_policy() is True
    monkeypatch.setenv("CROSS_PROVIDER_FALLBACK_POLICY", "off")
    assert load_cross_provider_fallback_policy() is False


@pytest.mark.asyncio
async def test_cross_provider_disabled_by_default(monkeypatch):
    monkeypatch.setattr(fb_mod, "CROSS_PROVIDER_FALLBACK_POLICY", False)
    router = _FakeRouter(provider="agnes", model="agnes-2.5-flash",
                         unavailable={"agnes"}, results={"chat_free": "free-ok"})
    assert await _run(router) == "free-ok"
    assert "chat_mimo" not in router.calls
    assert router.calls == ["chat_free"]


@pytest.mark.asyncio
async def test_cross_provider_enabled_uses_whitelist(monkeypatch):
    monkeypatch.setattr(fb_mod, "CROSS_PROVIDER_FALLBACK_POLICY", True)
    router = _FakeRouter(provider="agnes", model="agnes-2.5-flash",
                         unavailable={"agnes"}, results={"chat_mimo": "cross-ok"})
    assert await _run(router) == "cross-ok"
    assert router.calls == ["chat_mimo"]
    degrade = router.get_last_degrade()
    assert degrade["kind"] == "cross_provider_whitelist"
    assert (degrade["to_provider"], degrade["to_model"]) == ("mimo", "mimo-v2.5")


# ── 5. 反向证明：旧实现（没有同家档）会直接掉免费车道 ─────────────
@pytest.mark.asyncio
async def test_reverse_proof_without_matrix_falls_into_free_lane(monkeypatch):
    """把同家档查表退回旧实现（查不到）后，同一路径只剩免费车道。"""
    monkeypatch.setattr(fb_mod, "get_fallback_tier", lambda _task, _provider: None)
    router = _FakeRouter(provider="mimo", model="mimo-v2.5",
                         results={"chat_mimo": "tier-ok", "chat_free": "free-ok"})
    assert await _run(router) == "free-ok"
    assert router.calls == ["chat_free"], "旧实现下不应存在同家档调用"


# ── 6. model_used 落库值 ─────────────────────────────────────────
def test_model_used_keeps_legacy_semantics_without_degrade():
    router = _FakeRouter(provider="deepseek", model="deepseek-chat")
    assert router.get_model_used_for_log() == "deepseek-chat"


def test_model_used_carries_degrade_marker_once():
    from utils.free_model_backend import DEFAULT_FREE_MODEL

    router = _FakeRouter(provider="deepseek", model="deepseek-chat")
    router._mark_degraded(
        "free_lane", original_provider="deepseek", original_model="deepseek-chat",
        to_provider="siliconflow", to_model=DEFAULT_FREE_MODEL,
        source="free_emergency_fallback")
    assert router.get_model_used_for_log() == (
        f"siliconflow/{DEFAULT_FREE_MODEL} (degraded:free_lane)")
    # 一次性消费：下一轮未降级回复必须回落到旧语义
    assert router.get_model_used_for_log() == "deepseek-chat"


@pytest.mark.asyncio
async def test_chain_failure_returns_none_and_no_marker():
    router = _FakeRouter(provider="agnes", model="agnes-2.5-flash",
                         unavailable={"agnes", "siliconflow"})
    assert await _run(router) is None
    assert router.get_last_degrade() is None


def test_main_path_uses_router_degrade_resolver():
    """落库链路必须走 get_model_used_for_log（否则降级标记进不了 model_used）。"""
    from agent_core.mixins.main_path import _resolve_model_used_for_log

    assert _resolve_model_used_for_log(_FakeRouter(provider="deepseek",
                                                   model="deepseek-chat")) == "deepseek-chat"
    assert _resolve_model_used_for_log(SimpleNamespace()) == ""
