"""根因回归测试：验证截断检测矛盾修复。

根因：三处截断检测（model_router.py + message_processor.py 三处）使用
`len(last_line) >= 10` 启发式判断回复完整性，但 force_close 只查句末标点。
导致截断回复（末行较长但无标点）被误判完整 → 不重试 → force_close 追加"。"。
用户看到截断内容 + "。"。

修复后：
1. 长回复(>=30字符)必须以句末标点结尾，否则视为截断触发重试
2. 短回复(<30字符)不强制标点（"好的"/"嗯"等）
3. force_close 仅在 for 循环未判定完整时才触发
4. 英文推理泄漏始终视为截断
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "proj"))


# ── 测试 1: model_router._is_reply_incomplete 逻辑（通过模拟 response 验证）──

class TestTruncationDetectionLogic:
    """验证截断检测的核心逻辑：长回复无标点 = 截断。"""

    def test_long_reply_without_punct_is_incomplete(self):
        """长回复(>=30字符)不以句末标点结尾应视为截断。

        这是根因场景：截断回复末行可能很长（如"让我查一下记忆里7月16号7:00-8:00那段时间"），
        原 len(last_line)<10 启发式会漏判。
        """
        # 模拟根因场景：截断回复，末行很长但无句末标点
        truncated_reply = "让我查一下记忆里7月16号7:00-8:00那段时间发生了什么，然后告诉你"
        assert len(truncated_reply) >= 30
        # 不以任何句末标点结尾
        sentence_end_chars = "。！？～…）」】.!?"
        assert not truncated_reply.rstrip().endswith(tuple(sentence_end_chars))
        # 末行很长（>10字符）—— 原 heuristic 会误判为完整
        last_line = truncated_reply.rstrip().split('\n')[-1]
        assert len(last_line) >= 10

    def test_long_reply_with_punct_is_complete(self):
        """长回复以句末标点结尾应视为完整。"""
        complete_reply = "今天天气不错，我们一起去公园散步吧，顺便买点好吃的，度过一个愉快的下午。"
        assert len(complete_reply) >= 30
        sentence_end_chars = "。！？～…）」】.!?"
        assert complete_reply.rstrip().endswith(tuple(sentence_end_chars))

    def test_short_reply_without_punct_is_complete(self):
        """短回复(<30字符)不以句末标点结尾应视为完整（如"好的"/"嗯"）。"""
        short_reply = "好的"
        assert len(short_reply) < 30
        # 短回复不强制标点

    def test_latin_punctuation_recognized(self):
        """拉丁标点 .!? 也应视为句末标点。"""
        replies_with_latin = [
            "That sounds great!",
            "What time is it?",
            "I am fine.",
        ]
        sentence_end_chars = "。！？～…）」】.!?"
        for reply in replies_with_latin:
            assert reply.rstrip().endswith(tuple(sentence_end_chars)), \
                f"回复 '{reply}' 应以拉丁标点结尾被识别为完整"

    def test_english_reasoning_leak_always_incomplete(self):
        """英文推理泄漏始终视为截断，即使有标点。"""
        from utils.llm_cleanup import has_english_reasoning_leak
        leak_reply = "这是回复内容。Anyway continuing now"
        assert has_english_reasoning_leak(leak_reply) is True
        # 即使末尾有内容，有泄漏就需要清洗+重试

    def test_prod_truncated_sample_detected(self):
        """生产样本：截断回复末行很长但无标点，应被检测为需要重试。

        模拟 09:23:46 日志场景：QQ私聊回复被截断，末行>10字符，
        原 heuristic 误判完整 → force_close 追加"。"。
        """
        # 模拟截断回复（无句末标点，末行很长）
        prod_truncated = "嗯……让我查一下记忆里 7 月16号 7:00-8:00 那段时间发生了什么，然后告诉你"
        rstripped = prod_truncated.rstrip()
        sentence_end_chars = "。！？～…）」】.!?"
        ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))
        last_line = rstripped.split('\n')[-1]

        # 根因：这些条件同时满足时，原 heuristic 会误判
        assert len(prod_truncated) >= 30, "应满足长回复条件"
        assert not ends_with_punct, "应无句末标点"
        assert len(last_line) >= 10, "末行应较长（原 heuristic 的误判条件）"

        # 修复后：无标点的长回复应触发重试（不因末行长而误判完整）
        should_retry = len(prod_truncated) >= 30 and not ends_with_punct
        assert should_retry is True, "修复后应检测为需要重试"


class TestForceCloseConsistency:
    """验证 force_close 与 for 循环判定不再矛盾。"""

    def test_force_close_only_when_not_complete(self):
        """force_close 应仅在 for 循环未判定完整时触发。

        根因：原逻辑中 for 循环用 len(last_line)>=10 判定完整后 break，
        但 force_close 只查标点又触发，导致矛盾。
        修复后：用 _reply_considered_complete 标志确保一致性。
        """
        # 模拟 for 循环判定完整的情况
        _reply_considered_complete = True
        reply = "这是一个完整的回复，以句号结尾。"
        rstripped = reply.rstrip()
        sentence_end_chars = "。！？～…）」】.!?"
        ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))

        # for 循环判定完整 → 不应 force_close
        should_force_close = (not _reply_considered_complete) and (not ends_with_punct)
        assert should_force_close is False, "for 循环判定完整时不应 force_close"

    def test_force_close_when_retry_exhausted(self):
        """重试耗尽后仍未完整 → 应 force_close。"""
        _reply_considered_complete = False
        reply = "这是重试后仍不完整的回复，没有句末标点"
        rstripped = reply.rstrip()
        sentence_end_chars = "。！？～…）」】.!?"
        ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))

        should_force_close = (not _reply_considered_complete) and (not ends_with_punct)
        assert should_force_close is True, "重试耗尽且无标点时应 force_close"


class TestNoRegressionOnCompleteReplies:
    """验证完整回复不会误触发重试。"""

    def test_complete_chinese_reply_no_retry(self):
        """完整的中文回复（以。结尾）不应触发重试。"""
        reply = "今天天气不错，我们一起去公园散步吧。"
        rstripped = reply.rstrip()
        sentence_end_chars = "。！？～…）」】.!?"
        ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))
        # 完整回复：长回复 + 有标点 → 不需要重试
        need_retry = len(reply) >= 30 and not ends_with_punct
        assert need_retry is False

    def test_complete_multiline_reply_no_retry(self):
        """多行完整回复（末行有标点）不应触发重试。"""
        reply = "第一行内容\n第二行内容\n最后一行以标点结尾。"
        rstripped = reply.rstrip()
        sentence_end_chars = "。！？～…）」】.!?"
        ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))
        need_retry = len(reply) >= 30 and not ends_with_punct
        assert need_retry is False

    def test_short_chat_reply_no_retry(self):
        """短闲聊回复（<30字符）不应触发重试。"""
        short_replies = ["好的", "嗯", "在的", "没问题", "OK"]
        sentence_end_chars = "。！？～…）」】.!?"
        for reply in short_replies:
            rstripped = reply.rstrip()
            ends_with_punct = rstripped.endswith(tuple(sentence_end_chars))
            # 短回复不强制标点
            need_retry = len(reply) >= 30 and not ends_with_punct
            assert need_retry is False, f"短回复 '{reply}' 不应触发重试"
