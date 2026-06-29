# Debug: qq-greeting-leak

**Status**: [FIXED]
**Session ID**: qq-greeting-leak
**Date**: 2026-06-29

## Symptom

自动问候的内部推理文本泄漏到 QQ 平台，用户看到的是：
> 问候主题提示是"早安"，并且时间是清晨08:52。所以，问候应该围绕早安展开。关键指令：直接输出最终回复，不要思考过程。这意味着我不能在回复中解释我的思考，只能给出问候语。

期望：只看到一句简短的纳西妲风格问候（如"爸爸早安呀～今天也要加油哦 🌱"）

## Hypotheses

| # | Hypothesis | Status |
|---|-----------|--------|
| H1 | `_REASONING_INDICATORS` 正则 `问候主题[是：]` 不匹配 `问候主题提示是`（中间多了"提示"二字） | ✅ Confirmed (static) |
| H2 | `_THINK_PREFIX_PATTERNS` 缺少 `问候主题提示` / `关键指令` / `这意味着` 等前缀模式 | ✅ Confirmed (static) |
| H3 | `nudge_engine.py` 存在完全相同的 `_strip_thinking` 副本，同样的 bug | ✅ Confirmed (code read) |
| H4 | `chat_flash` 路由使用推理模型，输出思维链且复述 prompt 内容 | ✅ Confirmed (user evidence) |

## Evidence

- **用户报告**：QQ 平台上看到上述泄漏文本
- **静态分析**：
  - `_REASONING_INDICATORS` regex: `问候主题[是：]` — 不匹配 `问候主题提示是`
  - `_REASONING_INDICATORS` 不含 `关键指令|这意味着|并且时间是|直接输出最终回复`
  - `_THINK_PREFIX_PATTERNS` 不含 `问候主题` 前缀
  - 因此 `_REASONING_INDICATORS.search(text)` 返回 None，函数原样返回泄漏文本

## Root Cause

`_strip_thinking` 的正则模式过于具体，无法匹配推理模型的实际输出格式。两个文件存在代码重复：
1. `web/greeting_scheduler.py` L23-58
2. `emotion/nudge_engine.py` L13-45

## Fix Plan

1. 提取 `_strip_thinking` 到公共模块 `utils/llm_cleanup.py`
2. 扩展正则模式覆盖实际推理输出
3. 两处文件改为 import 公共模块
4. 添加 raw LLM output 日志记录
