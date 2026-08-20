"""config.py Phase 4（env 开关/常量表抽出）结构契约测试。

背景：config.py 的模块级 env 开关与常量表（API 密钥/端点、路由关键词表、
子代理任务映射、RAG/检索/流式/熔断/记忆蒸馏等运行开关与阈值、子 Agent 超时、
CHILD_CHUNK 结构常量、J-Space/情绪开关，以及 get_secret / _safe_positive_float
辅助）抽为 config_constants.py，逐字节搬移。

契约：
    1. config_constants 独立可导入（不 import config，无循环导入、
       不触发 config/web 依赖链）
    2. config 同名 re-export：from config import STREAM_TEXT_PUSH /
       RERANKER_ENABLED / get_secret 等既有用法不受影响（同对象）
    3. 行为契约：代表性开关/数值常量默认值、字面量表结构、get_secret 行为
"""
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# config.py Phase 4 re-export 清单（与 re-export 块一一对应）
REEXPORT_NAMES = [
    "get_secret", "_safe_positive_float",
    "TRUST_FORWARDED_FOR", "DEEPSEEK_API_KEY", "MIMO_API_KEY",
    "AGNES_API_KEY", "AGNES_BASE_URL", "AGNES_TEXT_MODEL",
    "AGNES_IMAGE_MODEL", "AGNES_VIDEO_MODEL",
    "ASR_API_KEY", "ASR_BASE_URL", "ASR_MODEL", "JINA_API_KEY",
    "AGENT_ROUTE_KEYWORDS", "AGENT_TASK_MAP",
    "RERANKER_API_KEY", "RERANKER_BASE_URL", "RERANKER_MODEL",
    "RERANKER_ENABLED", "RERANKER_OVERSAMPLE_RATIO",
    "QUERY_TRANSFORM_ENABLED", "QUERY_EXPAND_COUNT",
    "MEMORY_RETRIEVAL_DIFFUSION", "INTENT_LLM_CLASSIFY",
    "INTENT_CLASSIFY_TIMEOUT",
    "RETRIEVAL_SMART_SKIP", "RETRIEVAL_PARALLEL_TRANSFORM",
    "RETRIEVAL_PARALLEL_SEARCH",
    "QUERY_CACHE_ENABLED", "QUERY_CACHE_THRESHOLD",
    "QUERY_CACHE_MAX_SIZE", "QUERY_CACHE_TTL", "MEMORY_RETRIEVE_TIMEOUT",
    "PARENT_CHILD_CHUNK_ENABLED", "KG_V2_ENABLED",
    "CONTEXTUAL_RETRIEVAL_ENABLED",
    "CHILD_CHUNK_OVERLAP_CHARS", "CHILD_CHUNK_MAX_PER_PARENT",
    "CHILD_CHUNK_SEGMENT_MAX_LEN", "CHILD_VEC_TABLE", "CHILD_CHUNK_TYPES",
    "SUB_AGENT_API_TIMEOUT", "SUB_AGENT_TOTAL_TIMEOUT", "SUB_AGENT_API_RETRY",
    "TTS_ASYNC_MODE", "STREAM_STATUS_PUSH", "SIMPLE_CHAT_FASTPATH",
    "STREAM_TEXT_PUSH", "STREAM_TOOL_STATUS",
    "CIRCUIT_BREAKER_COOLDOWN", "CIRCUIT_BREAKER_HALF_OPEN_PROBES",
    "CIRCUIT_BREAKER_MAX_COOLDOWN",
    "ERROR_RULE_STRICT_MODE", "PROMPT_CACHING_ENABLED",
    "RAG_RERANK_WEIGHT", "RAG_KG_WEIGHT", "RAG_IMPORTANCE_WEIGHT",
    "RAG_RECALL_LIMIT", "RAG_RERANK_LIMIT", "RAG_MIN_FINAL_SCORE",
    "RAG_VEC_MAX_DISTANCE",
    "EMOTION_TRIGGER_THRESHOLD", "SCENE_STICKINESS_THRESHOLD",
    "MEMORY_COLD_MAX", "MEMORY_WARM_MAX", "MEMORY_WARM_VEC_WEIGHT",
    "MAX_EPISODIC_MEMORIES", "MEMORY_DISTILL_BATCH",
    "MEMORY_DISTILL_ENABLED", "MAX_EPISODIC_ROWS",
    "ENABLE_J_SPACE_HOOKS", "ENABLE_EMOTION_LLM",
    "DIRECTION_REGISTRY_PATH", "SIGNAL_STREAM_MAX_HISTORY",
    "INTERVENTION_DEFAULT_COOLDOWN",
]

# 代表性常量在环境变量清空时的默认值（{常量名: (环境变量名, 默认值)}）
_ENV_DEFAULTS = {
    # 布尔开关
    "TRUST_FORWARDED_FOR": ("TRUST_FORWARDED_FOR", False),
    "STREAM_TEXT_PUSH": ("STREAM_TEXT_PUSH", True),
    "STREAM_TOOL_STATUS": ("STREAM_TOOL_STATUS", True),
    "STREAM_STATUS_PUSH": ("STREAM_STATUS_PUSH", False),
    "TTS_ASYNC_MODE": ("TTS_ASYNC_MODE", True),
    "SIMPLE_CHAT_FASTPATH": ("SIMPLE_CHAT_FASTPATH", False),
    "PROMPT_CACHING_ENABLED": ("PROMPT_CACHING_ENABLED", False),
    "KG_V2_ENABLED": ("KG_V2_ENABLED", False),
    "RERANKER_ENABLED": ("RERANKER_ENABLED", True),
    "ERROR_RULE_STRICT_MODE": ("ERROR_RULE_STRICT_MODE", True),
    "MEMORY_DISTILL_ENABLED": ("MEMORY_DISTILL_ENABLED", False),
    "INTENT_LLM_CLASSIFY": ("INTENT_LLM_CLASSIFY", False),
    "ENABLE_J_SPACE_HOOKS": ("ENABLE_J_SPACE_HOOKS", True),
    "ENABLE_EMOTION_LLM": ("ENABLE_EMOTION_LLM", True),
    "QUERY_CACHE_ENABLED": ("QUERY_CACHE_ENABLED", True),
    "RETRIEVAL_SMART_SKIP": ("RETRIEVAL_SMART_SKIP", True),
    "RETRIEVAL_PARALLEL_TRANSFORM": ("RETRIEVAL_PARALLEL_TRANSFORM", True),
    "RETRIEVAL_PARALLEL_SEARCH": ("RETRIEVAL_PARALLEL_SEARCH", True),
    "PARENT_CHILD_CHUNK_ENABLED": ("PARENT_CHILD_CHUNK_ENABLED", True),
    "CONTEXTUAL_RETRIEVAL_ENABLED": ("CONTEXTUAL_RETRIEVAL_ENABLED", True),
    "QUERY_TRANSFORM_ENABLED": ("QUERY_TRANSFORM_ENABLED", True),
    "HYDE_ENABLED": ("HYDE_ENABLED", True),
    "MEMORY_RETRIEVAL_DIFFUSION": ("MEMORY_RETRIEVAL_DIFFUSION", True),
    # 数值/阈值（默认值）
    "RERANKER_OVERSAMPLE_RATIO": ("RERANKER_OVERSAMPLE_RATIO", 3),
    "QUERY_EXPAND_COUNT": ("QUERY_EXPAND_COUNT", 2),
    "INTENT_CLASSIFY_TIMEOUT": ("INTENT_CLASSIFY_TIMEOUT", 15.0),
    "QUERY_CACHE_THRESHOLD": ("QUERY_CACHE_THRESHOLD", 0.88),
    "QUERY_CACHE_MAX_SIZE": ("QUERY_CACHE_MAX_SIZE", 256),
    "QUERY_CACHE_TTL": ("QUERY_CACHE_TTL", 300),
    "MEMORY_RETRIEVE_TIMEOUT": ("MEMORY_RETRIEVE_TIMEOUT", 8.0),
    "CHILD_CHUNK_OVERLAP_CHARS": ("CHILD_CHUNK_OVERLAP_CHARS", 30),
    "CHILD_CHUNK_MAX_PER_PARENT": ("CHILD_CHUNK_MAX_PER_PARENT", 10),
    "CHILD_CHUNK_SEGMENT_MAX_LEN": ("CHILD_CHUNK_SEGMENT_MAX_LEN", 200),
    "SUB_AGENT_API_TIMEOUT": ("SUB_AGENT_API_TIMEOUT", 60),
    "SUB_AGENT_TOTAL_TIMEOUT": ("SUB_AGENT_TOTAL_TIMEOUT", 150),
    "SUB_AGENT_API_RETRY": ("SUB_AGENT_API_RETRY", 1),
    "CIRCUIT_BREAKER_COOLDOWN": ("CIRCUIT_BREAKER_COOLDOWN", 30),
    "CIRCUIT_BREAKER_HALF_OPEN_PROBES": ("CIRCUIT_BREAKER_HALF_OPEN_PROBES", 2),
    "CIRCUIT_BREAKER_MAX_COOLDOWN": ("CIRCUIT_BREAKER_MAX_COOLDOWN", 300),
    "RAG_RERANK_WEIGHT": ("RAG_RERANK_WEIGHT", 0.65),
    "RAG_KG_WEIGHT": ("RAG_KG_WEIGHT", 0.15),
    "RAG_IMPORTANCE_WEIGHT": ("RAG_IMPORTANCE_WEIGHT", 0.20),
    "RAG_RECALL_LIMIT": ("RAG_RECALL_LIMIT", 150),
    "RAG_RERANK_LIMIT": ("RAG_RERANK_LIMIT", 80),
    "RAG_MIN_FINAL_SCORE": ("RAG_MIN_FINAL_SCORE", 0.08),
    "RAG_VEC_MAX_DISTANCE": ("RAG_VEC_MAX_DISTANCE", 1.2),
    "EMOTION_TRIGGER_THRESHOLD": ("EMOTION_TRIGGER_THRESHOLD", 0.5),
    "SCENE_STICKINESS_THRESHOLD": ("SCENE_STICKINESS_THRESHOLD", 0.5),
    "MEMORY_COLD_MAX": ("MEMORY_COLD_MAX", 0),
    "MEMORY_WARM_MAX": ("MEMORY_WARM_MAX", 10),
    "MEMORY_WARM_VEC_WEIGHT": ("MEMORY_WARM_VEC_WEIGHT", 0.6),
    "MAX_EPISODIC_MEMORIES": ("MAX_EPISODIC_MEMORIES", 200),
    "MEMORY_DISTILL_BATCH": ("MEMORY_DISTILL_BATCH", 30),
    "MAX_EPISODIC_ROWS": ("MAX_EPISODIC_ROWS", 10000),
    "SIGNAL_STREAM_MAX_HISTORY": ("SIGNAL_STREAM_MAX_HISTORY", 1000),
    "INTERVENTION_DEFAULT_COOLDOWN": ("INTERVENTION_DEFAULT_COOLDOWN", 30.0),
}


# ── 1/2. 独立导入 + re-export 同对象 ─────────────────────────────

def test_config_constants_imports_standalone():
    import importlib
    mod = importlib.import_module("config_constants")
    for name in REEXPORT_NAMES:
        assert hasattr(mod, name), f"缺少符号 {name}"


def test_config_constants_standalone_import_chain():
    """干净子进程：import config_constants 不连带导入 config 或 web 依赖链。"""
    code = (
        "import sys\n"
        "import config_constants\n"
        "assert 'config' not in sys.modules, 'config 被连带导入'\n"
        "assert not any(m == 'web' or m.startswith('web.') for m in sys.modules), 'web 被连带导入'\n"
        "print('ok')\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True,
        cwd=str(Path(__file__).parent.parent),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


@pytest.mark.parametrize("name", REEXPORT_NAMES)
def test_config_reexports_same_objects(name):
    import config
    import config_constants
    assert hasattr(config, name), f"config 缺少兼容别名 {name}"
    assert getattr(config, name) is getattr(config_constants, name), name


def test_config_constants_does_not_import_config():
    import config_constants as mod
    assert "config" not in getattr(mod, "__dict__", {})


# ── 3. 行为契约 ─────────────────────────────────────────────────

def test_literal_table_structure():
    """字面量常量表结构不变（路由关键词/任务映射/CHILD_CHUNK 结构）。"""
    import config_constants as cc
    assert set(cc.AGENT_ROUTE_KEYWORDS) == {
        "xiaolian", "xiaolang", "xiaoke", "xiaoda", "parallel_trigger",
    }
    assert "搜索" in cc.AGENT_ROUTE_KEYWORDS["xiaolian"]
    assert "编程" in cc.AGENT_ROUTE_KEYWORDS["xiaolang"]
    assert "论文" in cc.AGENT_ROUTE_KEYWORDS["xiaoke"]
    assert "天气" in cc.AGENT_ROUTE_KEYWORDS["xiaoda"]
    assert "巡检" in cc.AGENT_ROUTE_KEYWORDS["parallel_trigger"]
    assert cc.AGENT_TASK_MAP == {
        "xiaolang": "debug",
        "xiaoke": "research",
        "xiaolian": "info_search",
        "xiaoda": "memory",
    }
    assert cc.CHILD_VEC_TABLE == "memories_child_vec"
    assert cc.CHILD_CHUNK_TYPES == ["segment", "entity", "decision", "topic"]


def test_env_switch_defaults():
    """清空环境变量后重载：开关/阈值默认值与搬移前一致。"""
    import importlib
    import os

    import config
    import config_constants as cc

    saved = {env_var: os.environ.get(env_var) for _, (env_var, _) in _ENV_DEFAULTS.items()}
    for env_var in saved:
        os.environ.pop(env_var, None)
    try:
        importlib.reload(cc)
        importlib.reload(config)
        for name, (env_var, expected) in _ENV_DEFAULTS.items():
            assert getattr(cc, name) == expected, f"{name} 默认值 {expected!r}"
            assert getattr(config, name) == expected, f"config.{name} re-export 默认值"
        assert config.STREAM_TEXT_PUSH is True
        assert config.KG_V2_ENABLED is False
    finally:
        for env_var, val in saved.items():
            if val is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = val
        importlib.reload(cc)
        importlib.reload(config)


def test_env_switch_explicit_value():
    """显式设置环境变量后重载：常量跟随环境变量（reload 语义等价）。"""
    import importlib
    import os

    import config
    import config_constants as cc

    saved = os.environ.get("STREAM_TEXT_PUSH")
    os.environ["STREAM_TEXT_PUSH"] = "false"
    try:
        importlib.reload(cc)
        importlib.reload(config)
        assert cc.STREAM_TEXT_PUSH is False
        assert config.STREAM_TEXT_PUSH is False
    finally:
        if saved is None:
            os.environ.pop("STREAM_TEXT_PUSH", None)
        else:
            os.environ["STREAM_TEXT_PUSH"] = saved
        importlib.reload(cc)
        importlib.reload(config)


def test_get_secret_plain_and_default(monkeypatch):
    """get_secret：明文透传、未设置回退 default。"""
    import config
    import config_constants as cc
    monkeypatch.setenv("__CC_PLAIN_SECRET", "plain-value")
    assert cc.get_secret("__CC_PLAIN_SECRET") == "plain-value"
    assert cc.get_secret("__CC_MISSING_SECRET_123", "dflt") == "dflt"
    # re-export 同对象：config.get_secret 与 config_constants.get_secret 一致
    assert config.get_secret is cc.get_secret


def test_safe_positive_float():
    """_safe_positive_float：0/负数/nan/inf/非法值回退 default，正值透传。"""
    import config_constants as cc
    assert cc._safe_positive_float(None, 8.0) == 8.0
    assert cc._safe_positive_float("12.5", 8.0) == 12.5
    assert cc._safe_positive_float("0", 8.0) == 8.0
    assert cc._safe_positive_float("-1", 8.0) == 8.0
    assert cc._safe_positive_float("nan", 8.0) == 8.0
    assert cc._safe_positive_float("inf", 8.0) == 8.0
    assert cc._safe_positive_float("abc", 8.0) == 8.0