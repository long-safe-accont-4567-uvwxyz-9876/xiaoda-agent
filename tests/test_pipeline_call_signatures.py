"""跨层调用签名守卫：防止 mock 单测掩盖 kwarg 名漂移。

根因案例：fallback 分支以 is_raw_filter= 调用 _hybrid_fts_search_scoped，
而 MemoryManager 形参名为 is_raw——裸 MagicMock 单测无法暴露，
仅在真实生产签名下 TypeError。本守卫用 AST 静态比对：

1. kwarg 名：受守卫方法在调用方文件与检索包内的关键字调用，
   必须存在于定义它的全部宿主（MemoryManager / RetrievalEngine）签名；
2. 位置参数个数：不超过宿主可位置形参数量（扣除绑定 self，
   含 *args 的签名跳过该校验）；
3. 清单活性：每个受守卫方法至少命中 1 个真实调用点，防清单腐化；
4. 门面↔引擎镜像：双侧均定义的受守卫方法形参名集合必须一致，
   单侧独有形参会在跨层转发时 TypeError 或静默丢参。

已知盲区（记录不阻塞）：动态 getattr 调用、**kwargs 展开、
清单外文件的调用点。
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

# 显式跨层调用方 + 检索包全量生产模块
EXPLICIT_SOURCES = (
    "web/routers/retrieval.py",
    "agent_core/mixins/main_path.py",
    "tools/memory_tool.py",
)


def _source_files() -> list[Path]:
    files = [ROOT / rel for rel in EXPLICIT_SOURCES]
    files.extend(sorted((ROOT / "memory" / "retrieval").glob("*.py")))
    return [f for f in files if f.is_file()]


GUARDED_METHODS = (
    "_hybrid_fts_search_scoped",
    "_hybrid_vec_search",
    "_hybrid_rerank",
    "retrieve_memories",
    "retrieve_memories_hybrid",
    "_recall_kg_v2",
)

_HOSTS = (MemoryManager, RetrievalEngine)


def _find_hosts(method: str) -> list[type]:
    return [host for host in _HOSTS if hasattr(host, method)]


def _max_positional_args(host: type, method: str) -> int | None:
    """实例调用的位置参数上限；签名含 *args 时返回 None（跳过校验）。"""
    sig = inspect.signature(getattr(host, method))
    for p in sig.parameters.values():
        if p.kind is inspect.Parameter.VAR_POSITIONAL:
            return None
    return sum(
        1 for p in sig.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ) - 1  # 扣除运行时绑定的 self


def _collect_problems() -> tuple[list[str], int, set[str]]:
    problems: list[str] = []
    checked = 0
    seen: set[str] = set()
    for src in _source_files():
        rel = src.relative_to(ROOT)
        tree = ast.parse(src.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            method = func.attr
            if method not in GUARDED_METHODS:
                continue
            seen.add(method)
            hosts = _find_hosts(method)
            if not hosts:
                problems.append(f"{rel}:{node.lineno} {method}: 宿主缺失")
                continue
            for host in hosts:
                params = inspect.signature(getattr(host, method)).parameters
                for kw_node in node.keywords:
                    if kw_node.arg is None:  # **kwargs 展开，静态不可判
                        continue
                    checked += 1
                    if kw_node.arg not in params:
                        problems.append(
                            f"{rel}:{node.lineno} {host.__name__}.{method}"
                            f"({kw_node.arg}=...) 不存在，签名 {sorted(params)}"
                        )
                cap = _max_positional_args(host, method)
                if cap is not None and len(node.args) > cap:
                    problems.append(
                        f"{rel}:{node.lineno} {host.__name__}.{method} "
                        f"位置参数 {len(node.args)} 个 > 上限 {cap}"
                    )
    return problems, checked, seen


def test_guarded_methods_exist_on_real_hosts():
    for name in GUARDED_METHODS:
        assert _find_hosts(name), \
            f"{name} 在 MemoryManager/RetrievalEngine 上均不存在"


def test_facade_engine_signatures_not_diverged():
    """双侧均定义的受守卫方法，形参名集合必须一致——防转发丢参。"""
    for name in GUARDED_METHODS:
        hosts = _find_hosts(name)
        if len(hosts) < 2:
            continue  # 单宿主独有方法（如引擎内部件）不适用镜像约束
        sig_sets = {
            h.__name__: frozenset(inspect.signature(getattr(h, name)).parameters)
            for h in hosts
        }
        values = list(sig_sets.values())
        assert values[0] == values[1], (
            f"{name}: 门面/引擎签名形参分叉 {sig_sets}"
            "——跨层转发将 TypeError 或静默丢参"
        )


def test_cross_layer_calls_match_real_signatures():
    sources = _source_files()
    assert len(sources) >= 5, f"扫描源不足: {[str(s) for s in sources]}"
    problems, checked, seen = _collect_problems()

    dead = sorted(set(GUARDED_METHODS) - seen)
    assert not dead, (
        f"受守卫方法零调用点 {dead}——"
        "方法已删除/改名或 SOURCE_FILES/GUARDED_METHODS 腐化，请同步修订"
    )
    assert checked >= 10, (
        f"守卫覆盖不足: 仅 {checked} 个 kwarg 被检查——"
        "AST 未匹配到受守卫方法调用，检查 SOURCE_FILES/GUARDED_METHODS"
    )
    assert not problems, (
        "跨层调用与真实签名不一致:\n" + "\n".join(problems)
    )
