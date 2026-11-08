"""流式回复降级标记（ModelStreamEvent.degraded）契约测试。

背景（2026-11-08）：降级此前只能从三处看到——router.degraded 日志、
conversation_logs.model_used 的 "(degraded:kind)" 后缀、/models/routes 矩阵。
逐条流式回复本身不带信息，用户看不出哪条回答是降级产生的。

本组用例锁住三件事：
1. 未降级时事件与 final 帧都不带标记（旧契约零变化）；
2. 降级时标记复用 fallback_chain 的 kind 与真实模型，语义与 model_used 同源；
3. 旧格式事件（不含 degraded 键）解析不报错，WS 帧透传保持向后兼容。
"""
from __future__ import annotations

from agent_core._shared import ProcessResult
from llm_gateway.fallback_chain import FallbackChainMixin
from llm_gateway.stream_protocol import ModelStreamEvent, StreamTurnResult
from web.ws_hub import ConnectionManager, serialize_result


def _degrade(kind: str = "free_lane") -> dict:
    """用真实的 _mark_degraded 产出标记，避免另造一套语义。"""
    return FallbackChainMixin()._mark_degraded(
        kind,
        original_provider="agnes",
        original_model="agnes-2.5-flash",
        to_provider="siliconflow",
        to_model="THUDM/GLM-4-9B-0414",
        source="test",
        detail="unit",
    )


def test_non_degraded_event_and_final_frame_carry_no_marker():
    event = ModelStreamEvent(
        kind="turn_end", turn=0, provider="agnes", model="agnes-2.5-flash",
    )
    assert event.degraded is None

    result = StreamTurnResult(
        text="hi",
        tool_calls=(),
        finish_reason="stop",
        reasoning="",
        provider="agnes",
        model="agnes-2.5-flash",
        used_fallback=False,
    )
    assert result.degraded is None
    # 未降级：final 帧保持旧结构，一个键都不多
    assert "degraded" not in serialize_result(ProcessResult(reply="hi"))


def test_degraded_event_carries_fallback_chain_kind():
    marker = _degrade("free_lane")
    event = ModelStreamEvent(kind="turn_end", turn=0, degraded=marker)
    assert event.degraded is not None
    assert event.degraded["degraded"] is True
    assert event.degraded["kind"] == "free_lane"
    assert event.degraded["to_model"] == "THUDM/GLM-4-9B-0414"

    result = StreamTurnResult(
        text="hi",
        tool_calls=(),
        finish_reason="stop",
        reasoning="",
        provider="siliconflow",
        model="THUDM/GLM-4-9B-0414",
        used_fallback=True,
        degraded=event.degraded,
    )
    data = serialize_result(ProcessResult(reply="hi", degraded=result.degraded))
    assert data["degraded"]["kind"] == "free_lane"

    # 与 model_used 落库标记同源：provider/model (degraded:kind)
    chain = FallbackChainMixin()
    chain._last_degrade = marker
    assert chain.get_model_used_for_log() == (
        "siliconflow/THUDM/GLM-4-9B-0414 (degraded:free_lane)"
    )


def test_legacy_event_without_degraded_key_parses():
    # 旧后端产出的 final 帧不带 degraded 键：透传不报错、不新增键
    manager = ConnectionManager()
    legacy = {"type": "final", "msg_id": "m1", "reply": "hi", "emotion": "neutral"}
    frames = manager._stream_frames("c1", dict(legacy))
    assert frames
    assert frames[0]["type"] == "final"
    assert "degraded" not in frames[0]

    # 旧构造方式（不传 degraded）同样合法，默认 None
    assert ModelStreamEvent(kind="text_delta", turn=0, text_delta="x").degraded is None


def test_ws_frames_forward_degraded_marker():
    marker = _degrade("same_family")
    manager = ConnectionManager()
    frames = manager._stream_frames(
        "c1", {"type": "final", "msg_id": "m1", "reply": "hi", "degraded": marker},
    )
    assert frames
    assert frames[0]["degraded"]["kind"] == "same_family"
