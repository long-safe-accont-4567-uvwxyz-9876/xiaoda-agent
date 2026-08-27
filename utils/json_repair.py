"""LLM JSON 输出清洗/修复 — 纯文本工具。

从 memory/knowledge_graph.py 提取（2026-08-27 H1 分层下沉专项）：
web.prompt_ab 原本 import 这两个 helper，导致 core_runtime 下沉后出现
memory → core_runtime → web.prompt_ab → memory 的 import 环。
两个函数均为零状态纯文本处理，提取到 utils 层供各方共享。
knowledge_graph 保留同名 re-export 兼容既有引用面。
"""
from __future__ import annotations

import re


def clean_json_response(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    brace_start = text.find('{')
    if brace_start > 0:
        text = text[brace_start:]
    brace_end = text.rfind('}')
    if brace_end >= 0 and brace_end < len(text) - 1:
        text = text[:brace_end + 1]
    return text


def repair_json(text: str) -> str:
    """修复 LLM 输出中常见的 JSON 语法错误。"""
    # 修复双花括号: 只删外层一对 {{ }}，保留嵌套与字符串值内的 {{}}
    # CodeRabbit 修复：全局 {{ → { 会破坏字符串值内的 {{template}}（如 observations）。
    # LLM 复制 prompt 示例时只会把整个 JSON 用 {{}} 包裹（外层一对），嵌套不会出现 {{}}，
    # 因此只删外层一对足够处理 139 次失败根因，且不破坏字符串内容。
    text = re.sub(r'^(\s*)\{\{', r'\1{', text, count=1)
    text = re.sub(r'\}\}(\s*)$', r'}\1', text, count=1)
    # 修复多余逗号: },, → },
    text = re.sub(r'},\s*,', '},', text)
    text = re.sub(r',\s*,', ',', text)
    # 修复尾逗号: ,} → } 和 ,] → ]
    text = re.sub(r',\s*}', '}', text)
    text = re.sub(r',\s*]', ']', text)
    # 修复缺少逗号: "key":"val" "key2" → "key":"val","key2"
    text = re.sub(r'"\s+(")', r',\1', text)
    # 修复 } 后面缺少逗号直接跟 { : }{ → },{
    text = re.sub(r'}\s*{', '},{', text)
    # 修复 ] 后面缺少逗号直接跟 { : ]{ → ],{
    return re.sub(r'\]\s*{', '],{', text)
