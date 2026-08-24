"""跨层调用签名守卫：防止 mock 单测掩盖 kwarg 名漂移。

根因案例：fallback 分支以 is_raw_filter= 调用 _hybrid_fts_search_scoped，
而 MemoryManager 形参名为 is_raw——裸 MagicMock 单测无法暴露，
仅在真实生产签名下 TypeError。本守卫用 AST 静态比对所有跨层调用方
（检索管线 / Web 评测路由 / 主路径 / 记忆工具）的关键字与真实宿主方法签名。
"""
from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from memory._retrieval_engine import RetrievalEngine
from memory.memory_manager import MemoryManager

ROOT = Path(__file__).resolve().parents[1]

SOURCE_FILES = (
    "memory/retrieval/pipeline.py",
    "web/routers/retrieval.py",
    "agent_core/mixins/main_path.py",
    "tools/memory_tool.py",
)

GUARDED_METHODS = (
    "_hybrid_fts_search_scoped",
    "_hybrid_vec_search",
    "_hybrid_rerank",
    "retrieve_memories",
    "retrieve_memories_hybrid",
    "_recall_kg_v2",
)


def _find_host(method: str):
    for host in (MemoryManager, RetrievalEngine):
        if hasattr(host, method):
            return host
    return None


def _collect_problems() -> tuple[list[str], int]:
    problems: list[str] = []
    checked = 0
    for rel in SOURCE_FILES:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            method = func.attr
            if method not in GUARDED_METHODS:
                continue
            host = _find_host(method)
            if host is None:
                problems.append(f"{rel}:{node.lineno} {method}: 宿主缺失")
                continue
            params = set(inspect.signature(getattr(host, method)).parameters)
            for kw in node.keywords:
                checked += 1
                if kw.arg is not None and kw.arg not in params:
                    problems.append(
                        f"{rel}:{node.lineno} {method}({kw.arg}=...) "
                        f"不存在于 {host.__name__} 签名 {sorted(params)}"
                    )
    return problems, checked


def test_guarded_methods_exist_on_real_hosts():
    for name in GUARDED_METHODS:
        assert _find_host(name) is not None, \
            f"{name} 在 MemoryManager/RetrievalEngine 上均不存在"


def test_cross_layer_kwargs_match_real_signatures():
    problems, checked = _collect_problems()
    assert checked >= 5, (
        f"守卫覆盖不足: 仅 {checked} 个 kwarg 被检查——"
        "AST 未匹配到受守卫方法调用，检查 SOURCE_FILES/GUARDED_METHODS"
    )
    assert not problems, "kwarg 名漂移:\n" + "\n".join(problems)
