"""C1 回归防护测试：验证所有 assistant-prefill 续写站点传 assume_tail=True。

背景（CodeRabbit 复审 C1）：
N8 修复新增 merge_continuation(..., assume_tail=True) 用于截断续写去重。
但首次实现时，prefill 站点全部漏传 assume_tail=True，导致 prefill
成功返回无字符重叠的纯尾巴时被当作"较短的重生成"丢弃，回复保持截断——
这比原事故（重复内容）更糟（用户连完整内容都看不到）。

本测试用静态分析（正则）检查所有 merge_continuation 调用站点：
1. prefill 站点（verification_length_retry/verification_no_finish_retry）
   必须传 assume_tail=True
2. after_tools 站点必须保持 assume_tail=False（默认，因为保留 user 消息
   主动要求更多内容，非截断续写）

CodeRabbit #3 同步更新：
原测试期望 5 个 prefill 站点（router_truncate_retry/verification_incomplete_retry/
simple_fast_path_retry/fast_path_incomplete_retry），但实际代码重构后只剩
2 个 prefill 站点 + 1 个 after_tools 站点。新增 verification_no_finish_retry
（处理 finish_reason=None 流式截断的续写重试路径）。

这是"调用约定测试"：不验证函数行为，只验证调用方传参正确。
防止未来重构时再次漏传 assume_tail=True。
"""
import re
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent

# prefill 站点的 context 标识（必须传 assume_tail=True）
# CodeRabbit #3 更新：移除已废弃的 router_truncate_retry/verification_incomplete_retry/
# simple_fast_path_retry/fast_path_incomplete_retry，新增 verification_no_finish_retry
PREFILL_CONTEXTS = {
    "verification_length_retry",      # finish_reason="length" 截断续写
    "verification_no_finish_retry",   # finish_reason=None 流式截断续写（CodeRabbit #3 激活）
}

# after_tools 站点（保留 assume_tail=False，因为保留 user 消息）
AFTER_TOOLS_CONTEXTS = {"after_tools_retry"}

# 所有可能包含 _retry_continuation 调用的源文件
# Phase 2 拆分：_retry_continuation 定义及全部调用站点随验收循环迁至
# agent_core/mixins/verification.py，此处跟随迁移（message_processor.py 已无调用）。
SOURCE_FILES = ["agent_core/mixins/verification.py"]


def _extract_retry_continuation_calls(source: str) -> list[dict]:
    """从源码中提取所有 _retry_continuation 调用及其参数。

    _retry_continuation 是 message_processor 重构后收敛续写重试的统一入口，
    内部负责把 context/assume_tail 透传给 merge_continuation。

    返回 [{"context": str, "assume_tail": bool|None, "line": int}, ...]
    assume_tail=None 表示未传该参数（使用默认值 False）。
    """
    calls = []
    # 匹配 _retry_continuation( ... ) 调用块（支持多行），
    # 用 (?<!def ) 排除方法定义行，只保留调用站点。
    pattern = re.compile(r'(?<!def )_retry_continuation\s*\(', re.MULTILINE)
    for m in pattern.finditer(source):
        start = m.end()  # ( 之后
        depth = 1
        pos = start
        while pos < len(source) and depth > 0:
            if source[pos] == '(':
                depth += 1
            elif source[pos] == ')':
                depth -= 1
            pos += 1
        if depth != 0:
            continue
        call_body = source[start:pos - 1]
        # 提取 context 参数
        ctx_match = re.search(r'context\s*=\s*"([^"]+)"', call_body)
        context = ctx_match.group(1) if ctx_match else None
        # 提取 assume_tail 参数
        tail_match = re.search(r'assume_tail\s*=\s*(True|False)', call_body)
        assume_tail = tail_match.group(1) == "True" if tail_match else None
        # 行号
        line = source[:m.start()].count('\n') + 1
        calls.append({
            "context": context,
            "assume_tail": assume_tail,
            "line": line,
        })
    return calls


def _load_source(rel_path: str) -> str:
    return (PROJ_ROOT / rel_path).read_text(encoding="utf-8")


class TestPrefillSitesPassAssumeTail:
    """验证 prefill 站点传 assume_tail=True。"""

    def test_message_processor_prefill_sites_pass_assume_tail(self):
        """verification.py（原 message_processor.py，Phase 2 拆分）的 2 个 prefill 站点必须传 assume_tail=True。

        覆盖：
        - verification_length_retry：finish_reason="length" 截断续写
        - verification_no_finish_retry：finish_reason=None 流式截断续写
          （CodeRabbit #3 修复激活，原 is_reply_likely_complete 无条件信任
           导致此路径为死代码，现已通过分档判定激活）
        """
        source = _load_source(SOURCE_FILES[0])
        calls = _extract_retry_continuation_calls(source)
        for ctx in PREFILL_CONTEXTS:
            site_calls = [c for c in calls if c["context"] == ctx]
            assert len(site_calls) == 1, \
                f"应只有 1 个 {ctx} 调用，实际：{len(site_calls)}"
            assert site_calls[0]["assume_tail"] is True, \
                f"{ctx} 必须传 assume_tail=True（line {site_calls[0]['line']}）\n" \
                "C1 根因：漏传 assume_tail=True 会导致 prefill 成功时尾巴被丢弃"


class TestAfterToolsSiteKeepsDefault:
    """验证 after_tools 站点保持 assume_tail=False（默认）。"""

    def test_after_tools_site_does_not_pass_assume_tail_true(self):
        """after_tools_retry 站点保留 user 消息主动要求更多内容，非截断续写。

        该站点应保持 assume_tail=False（默认），因为：
        - 保留 user 消息 "请继续给出具体内容"
        - LLM 重新生成完整回复（非 prefill 尾巴）
        - 需要走"重生成"判定（discarded/replaced），而非 appended
        """
        source = _load_source(SOURCE_FILES[0])
        calls = _extract_retry_continuation_calls(source)
        after_tools_calls = [c for c in calls if c["context"] == "after_tools_retry"]
        assert len(after_tools_calls) == 1, \
            f"应只有 1 个 after_tools_retry 调用，实际：{len(after_tools_calls)}"
        # assume_tail 应为 None（未传，使用默认 False）或显式 False
        assert after_tools_calls[0]["assume_tail"] is not True, \
            f"after_tools_retry 不应传 assume_tail=True（line {after_tools_calls[0]['line']}）"


class TestNoUnexpectedMergeContinuationCalls:
    """验证没有意外的 merge_continuation 调用（未识别的 context）。"""

    def test_all_calls_have_known_context(self):
        """所有 merge_continuation 调用的 context 必须在已知集合中。

        防止新增续写站点时漏传 assume_tail。新增站点必须在
        PREFILL_CONTEXTS 或 AFTER_TOOLS_CONTEXTS 中注册。

        CodeRabbit #3 更新：原测试同时检查 model_router.py，但该文件
        重构后已无 merge_continuation 调用。Phase 2 拆分后，_retry_continuation
        调用站点随验收循环迁至 agent_core/mixins/verification.py，
        故仅检查该文件（见 SOURCE_FILES）。
        """
        known_contexts = PREFILL_CONTEXTS | AFTER_TOOLS_CONTEXTS
        for rel_path in SOURCE_FILES:
            source = _load_source(rel_path)
            calls = _extract_retry_continuation_calls(source)
            assert calls, f"{rel_path} 应至少有 1 个 merge_continuation 调用"
            for call in calls:
                ctx = call["context"]
                assert ctx in known_contexts, \
                    f"{rel_path}:{call['line']} 未知 context: {ctx}\n" \
                    "新增续写站点必须在 PREFILL_CONTEXTS 或 AFTER_TOOLS_CONTEXTS 注册"
