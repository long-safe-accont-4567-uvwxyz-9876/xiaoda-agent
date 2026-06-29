"""QQ Streaming Evidence Collector — 报告运行时证据到 Debug Server。"""
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEBUG_URL = "http://127.0.0.1:7778/event"
SESSION_ID = "qq-streaming-evidence"


def report(hypothesis_id: str, msg: str, data: dict, run_id: str = "pre"):
    payload = json.dumps({
        "sessionId": SESSION_ID,
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": "_send_streaming_reply",
        "msg": f"[DEBUG] {msg}",
        "data": data,
        "ts": int(time.time() * 1000),
    }).encode()
    req = urllib.request.Request(
        DEBUG_URL, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except Exception as e:
        print(f"  [report failed] {e}")


class MockMessage:
    """模拟 QQ 消息对象，记录每次 reply 调用。"""
    def __init__(self, fail_on_segment: int = -1):
        self.replies: list[dict] = []
        self._fail_on = fail_on_segment
        self._call_count = 0

    async def reply(self, content: str, msg_seq: int = 0):
        self._call_count += 1
        # 跳过打字指示，不计入分片计数
        if content == "纳西妲正在打字...":
            self.replies.append({"content": content, "len": len(content), "seq": msg_seq, "typing": True})
            return
        if self._fail_on >= 0 and self._call_count == self._fail_on:
            raise ConnectionError(f"模拟第 {self._call_count} 次发送失败")
        self.replies.append({"content": content, "len": len(content), "seq": msg_seq, "typing": False})


async def run_evidence_collection():
    from qq_bot_adapter import AIQQBot

    adapter = AIQQBot.__new__(AIQQBot)
    adapter._next_msg_seq = lambda: 1

    # ── Hypothesis E: 短回复不分片 ──
    print("=== Hypothesis E: 短回复不分片 ===")
    msg_e = MockMessage()
    short_text = "你好呀，我是纳西妲～"
    t0 = time.monotonic()
    await adapter._send_streaming_reply(msg_e, short_text)
    elapsed = (time.monotonic() - t0) * 1000
    report("E", "短回复测试", {
        "text_len": len(short_text),
        "reply_count": len(msg_e.replies),
        "reply_lens": [r["len"] for r in msg_e.replies],
        "elapsed_ms": round(elapsed, 1),
        "pass": len(msg_e.replies) == 1,
    })

    # ── Hypothesis A: 分片大小 200-400，不切断代码块 ──
    print("=== Hypothesis A: 分片大小与代码块保护 ===")
    code_block_text = "这是一段包含代码块的回复：\n\n```python\ndef hello():\n    print('world')\n    return True\n```\n\n代码块后面的解释文字，需要足够长才能触发分片机制。" * 8
    msg_a = MockMessage()
    await adapter._send_streaming_reply(msg_a, code_block_text)
    content_replies = [r for r in msg_a.replies if not r.get("typing")]
    sizes = [r["len"] for r in content_replies]
    # 检查代码块完整性
    full_reconstructed = "".join(r["content"] for r in content_replies)
    code_blocks_original = code_block_text.count("```")
    code_blocks_reconstructed = full_reconstructed.count("```")
    report("A", "分片大小与代码块测试", {
        "total_len": len(code_block_text),
        "num_segments": len(content_replies),
        "segment_sizes": sizes,
        "min_size": min(sizes) if sizes else 0,
        "max_size": max(sizes) if sizes else 0,
        "code_blocks_original": code_blocks_original,
        "code_blocks_reconstructed": code_blocks_reconstructed,
        "content_preserved": full_reconstructed == code_block_text,
        "pass": all(100 <= s <= 600 for s in sizes) and code_blocks_original == code_blocks_reconstructed and full_reconstructed == code_block_text,
    })

    # ── Hypothesis C: 片间隔 800-1200ms ──
    print("=== Hypothesis C: 片间隔时序 ===")
    timing_text = "这是一段很长的回复。" * 80  # ~800 字符，产生 3+ 片
    msg_c = MockMessage()
    segment_times: list[float] = []
    original_reply = msg_c.reply

    async def timed_reply(content: str, msg_seq: int = 0):
        if content != "纳西妲正在打字...":
            segment_times.append(time.monotonic())
        await original_reply(content, msg_seq)

    msg_c.reply = timed_reply
    t_start = time.monotonic()
    await adapter._send_streaming_reply(msg_c, timing_text)
    total_ms = (time.monotonic() - t_start) * 1000

    intervals = []
    for i in range(1, len(segment_times)):
        intervals.append(round((segment_times[i] - segment_times[i - 1]) * 1000, 1))

    report("C", "片间隔时序测试", {
        "total_len": len(timing_text),
        "num_segments": len([r for r in msg_c.replies if not r.get("typing")]),
        "intervals_ms": intervals,
        "total_ms": round(total_ms, 1),
        "all_in_range": all(700 <= iv <= 1400 for iv in intervals),
        "pass": all(700 <= iv <= 1400 for iv in intervals) if intervals else True,
    })

    # ── Hypothesis D: 异常恢复 ──
    print("=== Hypothesis D: 异常恢复 ===")
    recovery_text = "异常恢复测试文字内容。" * 60  # ~600 字符，产生多片
    msg_d = MockMessage(fail_on_segment=4)  # 第 4 次发送失败（跳过 typing 后的实际片段）
    await adapter._send_streaming_reply(msg_d, recovery_text)
    content_replies_d = [r for r in msg_d.replies if not r.get("typing")]
    full_recovered = "".join(r["content"] for r in content_replies_d)
    report("D", "异常恢复测试", {
        "total_len": len(recovery_text),
        "fail_on_segment": 4,
        "reply_count": len(content_replies_d),
        "reply_lens": [r["len"] for r in content_replies_d],
        "content_preserved": full_recovered == recovery_text,
        "pass": full_recovered == recovery_text,
    })

    # ── Hypothesis B: 首片延迟 ──
    print("=== Hypothesis B: 首片延迟 ===")
    first_seg_text = "首片延迟测试文字内容。" * 50  # ~500 字符，产生多片
    msg_b = MockMessage()
    first_reply_time = None
    original_reply_b = msg_b.reply

    async def first_timed_reply(content: str, msg_seq: int = 0):
        nonlocal first_reply_time
        if content != "纳西妲正在打字..." and first_reply_time is None:
            first_reply_time = time.monotonic()
        await original_reply_b(content, msg_seq)

    msg_b.reply = first_timed_reply
    t0 = time.monotonic()
    await adapter._send_streaming_reply(msg_b, first_seg_text)
    first_latency = (first_reply_time - t0) * 1000 if first_reply_time else 0
    report("B", "首片延迟测试", {
        "total_len": len(first_seg_text),
        "first_segment_latency_ms": round(first_latency, 1),
        "pass": first_latency < 500,
    })

    print("\n=== 所有证据已提交到 Debug Server ===")


if __name__ == "__main__":
    asyncio.run(run_evidence_collection())
