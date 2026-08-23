"""config.py 的 env 开关/常量表 — 自 config.py 拆分（上帝文件 Phase 4）。

内容：模块级环境变量开关与常量表（import 期 os.getenv 求值 + 纯字面量）：
API 密钥/端点（DEEPSEEK/MIMO/AGNES/ASR/Jina/Reranker，经 get_secret 解密）、
子代理任务映射 AGENT_TASK_MAP、
RAG/检索/流式/熔断/记忆蒸馏等运行开关与阈值、子 Agent 超时、
CHILD_CHUNK 结构常量、J-Space/情绪分析开关，以及仅被这些常量求值使用的
辅助工具 get_secret / _safe_positive_float（safe_int/safe_float 从
utils.common 复用）。函数体自 config.py 逐字节搬移。

兼容契约（tests/test_config_constants_module.py）：
    - 本模块不得 import config（防循环依赖；仅依赖 config_paths /
      config_providers / utils.common / utils.encrypted_credential / security）
    - config 同名 re-export，from config import STREAM_TEXT_PUSH /
      RERANKER_ENABLED / get_secret 等既有用法不受影响

留在 config.py 的相邻内容（非静态常量，本块不搬）：
    - DEFAULT_PROVIDER / set_default_provider / MODEL_NAME 等 provider 可变状态
      （依赖 config.py 模块级可变 DEFAULT_PROVIDER，见 config_providers docstring）
    - get_temperature / get_frequency_penalty / get_presence_penalty
      （运行时读 web.config_service，属动态配置而非静态常量）
    - load_agent_config / AGENT_CONFIG（JSON5 配置加载，依赖 _strip_json5_comments）
    - _resolve_command / MCP_SERVERS（MCP 命令解析表，非 env 开关）
"""
from __future__ import annotations

import os

from loguru import logger

from config_paths import DATA_DIR
from config_providers import get_base_url_for_provider, get_default_model_for_provider
from security import credential_vault
from utils.common import safe_float as _safe_float
from utils.common import safe_int as _safe_int
from utils.encrypted_credential import protect_credential


def get_secret(name: str, default: str = "") -> str:
    """读取敏感环境变量并自动解密 enc:v1: 格式的密文

    非 enc:v1: 前缀的值视为明文直接返回（向后兼容）。
    解密失败（如机器不匹配、HMAC 验证失败）返回空字符串，避免明文泄漏。
    仅用于 API Key / Token / Secret 类敏感配置，普通配置仍使用 os.getenv。
    """
    value = os.getenv(name)
    if value is None:
        return default
    if not value:
        return value
    try:
        return credential_vault.decrypt(value)
    except credential_vault.DecryptionError as e:
        logger.warning("config.decrypt_failed: {} ({})", name, e)
        return default


# ── 密钥类常量懒解密（2026-08-22 config import 副作用瘦身）──────────
# 原本 7 处 get_secret() 在模块顶层执行——任何 import 都触发凭证库解密 IO。
# 现改为 PEP 562 模块级 __getattr__ 首次访问时求值并缓存到 globals()
# （与 config.py 转发 prompt_builder 的既有模式一致）。
# 语义保持：from config import DEEPSEEK_API_KEY 仍在消费方 import 时快照，
# 与原顶层赋值的绑定时机等价；纯路径/开关类 import 不再触碰凭证库。
_SECRET_CONSTANTS = {
    "DEEPSEEK_API_KEY": lambda: get_secret("DEEPSEEK_API_KEY"),
    # MIMO_API_KEY：先 get_secret 解密 enc:v1: 密文，再 protect_credential 内存态保护
    "MIMO_API_KEY": lambda: protect_credential(get_secret("MIMO_API_KEY", "")),
    "AGNES_API_KEY": lambda: get_secret("AGNES_API_KEY", ""),
    # ASR 主 key 缺失时回退硅基流动（同一凭证库）
    "ASR_API_KEY": lambda: get_secret("ASR_API_KEY", "") or get_secret("SILICONFLOW_API_KEY", ""),
    "JINA_API_KEY": lambda: get_secret("JINA_API_KEY", ""),
    "RERANKER_API_KEY": lambda: get_secret("RERANKER_API_KEY", ""),
}


def __getattr__(name: str):
    factory = _SECRET_CONSTANTS.get(name)
    if factory is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = factory()
    globals()[name] = value  # 缓存，后续直接命中
    return value


# ── 反代客户端 IP 解析 ──
# 默认 False：使用 TCP 对端 request.client.host（最安全）。
# 设为 True 时从 X-Forwarded-For 末尾取真实 IP，仅在你确信部署在可信反代
# （如 nginx/Caddy）后才启用，否则攻击者可伪造 XFF 绕过登录限流/白名单。
TRUST_FORWARDED_FOR = os.getenv("TRUST_FORWARDED_FOR", "").strip().lower() in ("1", "true", "yes", "on")


# Agnes AI 配置（在 get_provider_config 之前定义，避免前向引用）
AGNES_BASE_URL = get_base_url_for_provider("agnes")
AGNES_TEXT_MODEL = get_default_model_for_provider("agnes")
AGNES_IMAGE_MODEL = os.getenv("AGNES_IMAGE_MODEL", "agnes-image-2.1-flash")
AGNES_VIDEO_MODEL = os.getenv("AGNES_VIDEO_MODEL", "agnes-video-v2.0")


# ── ASR 语音识别配置 ──
ASR_BASE_URL = os.getenv("ASR_BASE_URL", "https://api.siliconflow.cn/v1")
ASR_MODEL = os.getenv("ASR_MODEL", "FunAudioLLM/SenseVoiceSmall")


# ── 路由关键词常量 ──────────────────────────────────────────────
# P0 修复（用户明确要求"取消对话通道分类机制"）：
# 已移除 SIMPLE_TASK_KEYWORDS 和 PRO_TASK_KEYWORDS —— 通道分类性价比太低，
# 误判会导致工具被错误过滤或模型错误升级。所有消息统一走主路径，由 LLM 自行决定。
# 调用点（_is_simple_task / _is_simple_chat / _should_escalate_to_pro 关键词分支）
# 已从 message_processor.py 中删除。

# ── 子代理任务类型映射（EnhancedBeliefRouter 使用） ──
AGENT_TASK_MAP = {
    "xiaolang": "debug",
    "xiaoke": "research",
    "xiaolian": "info_search",
    "xiaoda": "memory",
}

# ── RAG 优化配置（SiliconFlow 免费常驻） ──
RERANKER_BASE_URL = os.getenv("RERANKER_BASE_URL", "https://api.siliconflow.cn/v1")
RERANKER_MODEL = os.getenv("RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
RERANKER_ENABLED = os.getenv("RERANKER_ENABLED", "true").lower() in ("1", "true", "yes")


def _safe_positive_float(env_val: str | None, default: float) -> float:
    """解析正有限浮点数；0/负数/nan/inf/解析失败均回退到 default.

    用于超时类配置：非正值会导致立即超时，非有限值不是合法运营超时。
    """
    if env_val is None:
        return default
    try:
        v = float(env_val)
    except (ValueError, TypeError):
        return default
    import math
    if math.isfinite(v) and v > 0:
        return v
    return default


RERANKER_OVERSAMPLE_RATIO = _safe_int(os.getenv("RERANKER_OVERSAMPLE_RATIO"), 3)

# RRF rank_penalty：排名惩罚指数（解决 bge-large 下语义近邻与干扰项 L2 距离极接近、
# 线性 RRF 无法区分导致语义近邻被挤出 top-k 的问题）。p=1.0 为标准 RRF（向后兼容）。
# p>1 放大头部 rank 优势。默认 1.0：经 bench 量化后再决定是否上调。
RAG_RRF_RANK_PENALTY = _safe_float(os.getenv("RAG_RRF_RANK_PENALTY"), 1.0)

# FTS CJK 单字降噪：开启后，长查询（含 len>=2 多字词）丢弃 CJK 单字 token。
# 诊断证实单字噪声真实存在（"我"OR 匹配几乎所有记忆），但 A/B 量化证明降噪
# 严重负向（Recall 78.1%->25.0%）：语义改写查询（"我的联系方式是多少"）记忆
# 字面不含多字词（"手机号"），单字匹配是其 FTS 通道唯一命中兜底，降噪即摧毁。
# 默认 false 且不建议开启；保留开关供未来配合"查询重写"（rewrite_query 已有）
# 场景下再量化。
FTS_DROP_CJK_SINGLE = os.getenv("FTS_DROP_CJK_SINGLE", "false").lower() in ("1", "true", "yes")

# FTS CJK 停用词过滤：比 FTS_DROP_CJK_SINGLE 更精准的降噪策略。
# 只过滤已知高频无意义单字（我/的/了/是/在/有/和/不 等），保留有区分度的单字
# （叫/吃/写/喝 等）。FTS_DROP_CJK_SINGLE 删所有单字导致 Recall 78.1%→25.0%，
# 停用词方案避免此问题。
# 实测（bge-large + reranker, k=8）：停用词过滤 Recall 85.4% vs 无过滤 87.5%，
# 轻微负向（-2.1%），因"我的证件号码"等查询中"我"被过滤后 FTS 信号减弱。
# 配合查询改写（rewrite_query）后停用词过滤可能更安全——改写后查询已含具体关键词，
# 不再依赖"我"做 FTS 兜底。默认 false，留开关供后续配合改写再量化。
FTS_CJK_STOP_WORDS_FILTER = os.getenv("FTS_CJK_STOP_WORDS_FILTER", "false").lower() in ("1", "true", "yes")

# Query Transform
QUERY_TRANSFORM_ENABLED = os.getenv("QUERY_TRANSFORM_ENABLED", "true").lower() in ("1", "true", "yes")
QUERY_EXPAND_COUNT = _safe_int(os.getenv("QUERY_EXPAND_COUNT"), 0)  # 默认关闭多查询扩展（实测不好用），rewrite_query 仍保留
# HyDE（假设文档嵌入）：开启时生成假设答案文档，与原查询向量混合检索。
# 默认关闭（HYDE_ENABLED=false）：远程实测 Recall@5 从 78.1% 降至 53.1%（-25%），
# 假设文档噪声大于收益，与多查询扩展结论一致。
# 环境变量 HYDE_ENABLED=true 可重新启用，后续换更贴合数据/调参(alpha)可再量化。
HYDE_ENABLED = os.getenv("HYDE_ENABLED", "false").lower() in ("1", "true", "yes")
# 检索扩散开关：False=精准检索（搜什么就是什么，跳过 expand_query 和 _spreading_recall）
# True=扩散检索（向后兼容，生成额外查询目标 + 概念图扩散）
# 默认开启：配合 Reranker 精排兜底，扩散召回的结果可被交叉编码器过滤，
# 仅当 Reranker 不可用时扩散结果才以最低优先级进入最终输出（权重 0.4，RAG_MIN_FINAL_SCORE 兜底）。
MEMORY_RETRIEVAL_DIFFUSION = os.getenv("MEMORY_RETRIEVAL_DIFFUSION", "true").lower() in ("1", "true", "yes")
# 意图分类 LLM 调用：默认开启（GLM-Z1-9B-0414 推理质量高，速度可接受）
# 设置 INTENT_LLM_CLASSIFY=false 可关闭 LLM 分类，仅用规则匹配（更快）
INTENT_LLM_CLASSIFY = os.getenv("INTENT_LLM_CLASSIFY", "false").lower() in ("1", "true", "yes")
# 意图分类 LLM 调用超时（秒），默认 5.0s（从 2.0s 提升，避免误超时）
INTENT_CLASSIFY_TIMEOUT = _safe_float(os.getenv("INTENT_CLASSIFY_TIMEOUT"), 15.0)

# Retrieval Optimization (A1/A2/A3)
RETRIEVAL_SMART_SKIP = os.getenv("RETRIEVAL_SMART_SKIP", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_TRANSFORM = os.getenv("RETRIEVAL_PARALLEL_TRANSFORM", "true").lower() in ("1", "true", "yes")
RETRIEVAL_PARALLEL_SEARCH = os.getenv("RETRIEVAL_PARALLEL_SEARCH", "true").lower() in ("1", "true", "yes")
# 查询语义缓存开关：命中缓存时跳过完整检索流水线
QUERY_CACHE_ENABLED = os.getenv("QUERY_CACHE_ENABLED", "true").lower() in ("1", "true", "yes")
# P3-9: 查询缓存参数配置化（之前硬编码在 QueryCache 默认参数中，无法运行时调节）
# threshold: 余弦相似度阈值，>= 此值视为命中（0.88 严格匹配，避免误命中返回无关记忆）
# max_size: LRU 最大条目数（256 足够覆盖活跃话题，过大占用内存）
# ttl: 缓存过期时间秒（300s = 5 分钟，与 kg query_entity_cache 对齐）
QUERY_CACHE_THRESHOLD = _safe_float(os.getenv("QUERY_CACHE_THRESHOLD", "0.88"), 0.88)
QUERY_CACHE_MAX_SIZE = _safe_int(os.getenv("QUERY_CACHE_MAX_SIZE"), 256)
QUERY_CACHE_TTL = _safe_int(os.getenv("QUERY_CACHE_TTL"), 300)
# 单次记忆检索超时（秒）。主路径记忆检索在 LLM 调用前被 await，属串行瓶颈；
# 过低会误砍仍在进行的 embed/rerank（USB 盘慢时 5s 常超，导致记忆注入为空、回复短），
# 过高则拖慢整体回复。默认 8s：给予慢速存储足够余量，同时控制最坏延迟。
MEMORY_RETRIEVE_TIMEOUT = _safe_positive_float(os.getenv("MEMORY_RETRIEVE_TIMEOUT"), 8.0)

# Rust 热点下沉 PoC 开关：True 时扩散激活直接命中通道走 rust_core 扩展
# （PyO3 常驻 NodeIndex），扩展缺失/调用失败自动回退纯 Python，业务无感。
# 默认关闭——需先通过 tests/test_rust_hybrid_poc.py 等价性验证再开启。
RUST_HYBRID_ENABLED = os.getenv("RUST_HYBRID_ENABLED", "false").lower() in ("1", "true", "yes")

# ── 父子Chunk RAG 优化 ──
PARENT_CHILD_CHUNK_ENABLED = os.getenv("PARENT_CHILD_CHUNK_ENABLED", "true").lower() in ("1", "true", "yes")
# ── KG v2 知识图谱优化 ──
KG_V2_ENABLED = os.getenv("KG_V2_ENABLED", "false").lower() in ("1", "true", "yes")
CONTEXTUAL_RETRIEVAL_ENABLED = os.getenv("CONTEXTUAL_RETRIEVAL_ENABLED", "true").lower() in ("1", "true", "yes")
CHILD_CHUNK_OVERLAP_CHARS = _safe_int(os.getenv("CHILD_CHUNK_OVERLAP_CHARS"), 30)
CHILD_CHUNK_MAX_PER_PARENT = _safe_int(os.getenv("CHILD_CHUNK_MAX_PER_PARENT"), 10)
CHILD_CHUNK_SEGMENT_MAX_LEN = _safe_int(os.getenv("CHILD_CHUNK_SEGMENT_MAX_LEN"), 200)
CHILD_VEC_TABLE = "memories_child_vec"
CHILD_CHUNK_TYPES = ["segment", "entity", "decision", "topic"]

# ── 子Agent LLM调用超时配置 ──
# 单次LLM API调用超时(秒); 网络抖动时会重试一次(用半超时值)
SUB_AGENT_API_TIMEOUT = _safe_int(os.getenv("SUB_AGENT_API_TIMEOUT"), 60)
# 整个对话循环(多轮工具调用)总超时(秒)
SUB_AGENT_TOTAL_TIMEOUT = _safe_int(os.getenv("SUB_AGENT_TOTAL_TIMEOUT"), 150)
# LLM调用超时后重试次数(0=不重试, 1=重试1次用半超时)
SUB_AGENT_API_RETRY = _safe_int(os.getenv("SUB_AGENT_API_RETRY"), 1)

# ── 性能优化开关 ──────────────────────────────────────────────
# Task 6: TTS 异步化（方案 B）—— 开启后 TTS 在后台合成，先返回文字回复
TTS_ASYNC_MODE = os.getenv("TTS_ASYNC_MODE", "true").lower() in ("1", "true", "yes")
# Task 7: 流式中间状态推送（方案 C1）—— 开启后推送细粒度思考状态
STREAM_STATUS_PUSH = os.getenv("STREAM_STATUS_PUSH", "false").lower() in ("1", "true", "yes")
# Task 9: 简单对话快速路径（方案 E）—— 开启后简单闲聊跳过记忆检索
# P0：fastpath 机制已彻底取消（用户要求"取消fastpath机制，通道分类性价比太低了"）
# 环境变量保留读取仅为向后兼容（仍默认 false），但所有调用点已删除，
# 即使设为 true 也不会触发任何 fastpath 逻辑。
SIMPLE_CHAT_FASTPATH = os.getenv("SIMPLE_CHAT_FASTPATH", "false").lower() in ("1", "true", "yes")

# P0: WebSocket 流式文本推送 —— LLM 流式调用 + 逐 token 推送
STREAM_TEXT_PUSH = os.getenv("STREAM_TEXT_PUSH", "true").lower() in ("1", "true", "yes")
# P0: 工具调用中间状态推送（started/completed/failed）
STREAM_TOOL_STATUS = os.getenv("STREAM_TOOL_STATUS", "true").lower() in ("1", "true", "yes")

# Task 12: 熔断器智能恢复配置（P2）
# COOLDOWN 从 60→30：熔断后恢复更快，避免长时间快速失败拖累用户体验
CIRCUIT_BREAKER_COOLDOWN = _safe_int(os.getenv("CIRCUIT_BREAKER_COOLDOWN"), 30)
CIRCUIT_BREAKER_HALF_OPEN_PROBES = _safe_int(os.getenv("CIRCUIT_BREAKER_HALF_OPEN_PROBES"), 2)
CIRCUIT_BREAKER_MAX_COOLDOWN = _safe_int(os.getenv("CIRCUIT_BREAKER_MAX_COOLDOWN"), 300)

# P5: 失败经验→规则闭环 —— 命中规则时是否拒绝调用（true=拒绝，false=仅记录警告日志）
ERROR_RULE_STRICT_MODE = os.getenv("ERROR_RULE_STRICT_MODE", "true").lower() in ("1", "true", "yes")

# P6: 增量上下文构建与 Prompt Caching —— 开启后拆分系统提示稳定段/动态段并标记缓存
PROMPT_CACHING_ENABLED = os.getenv("PROMPT_CACHING_ENABLED", "false").lower() in ("1", "true", "yes")

# RAG Fusion Weights（与 _compute_final_scores 评分公式对齐）
# bench_memory_recall_vec 实测最优：rerank=0.60 主导排序，importance=0.10 仅微调
# 原配置 rerank=0.65/importance=0.20 导致高重要性但不相关的记忆挤掉相关记忆
RAG_RERANK_WEIGHT = _safe_float(os.getenv("RAG_RERANK_WEIGHT"), 0.60)
RAG_KG_WEIGHT = _safe_float(os.getenv("RAG_KG_WEIGHT"), 0.10)
RAG_IMPORTANCE_WEIGHT = _safe_float(os.getenv("RAG_IMPORTANCE_WEIGHT"), 0.10)

# RAG 候选集大小（每路召回 Top-N，RRF 融合后送 Reranker 的数量）
# 120: 七路并行每路120条=最多840条候选，多数路返回不到120条，足够覆盖有效候选
# 过大(150)浪费排序开销，RRF融合后截到rerank_limit送Reranker
RAG_RECALL_LIMIT = _safe_int(os.getenv("RAG_RECALL_LIMIT"), 120)
RAG_RERANK_LIMIT = _safe_int(os.getenv("RAG_RERANK_LIMIT"), 60)

# RAG 最低相关分过滤：final_score 低于此值的结果被视为噪声丢弃
# 根因（bench_rag_e2e 实测）：技术型 query 在向量库无精确命中时，RRF 融合会
# 返回 score 0.007-0.07 的完全无关结果（如 Python query 返回亲密内容），
# 污染上下文。闲聊型 query 天然宽松不过滤，非闲聊型按此阈值过滤。
RAG_MIN_FINAL_SCORE = _safe_float(os.getenv("RAG_MIN_FINAL_SCORE"), 0.08)

# RAG 向量召回绝对距离阈值（治本：源头过滤不相关向量）
# 根因（TDD test_rag_quality_root_fix 诊断）：原 _hybrid_vec_search 用相对归一化
# (1 - distance/max_dist) 美化距离，即使最远的向量也接近 1.0 高分，导致
# Python query 召回亲密内容。改用绝对 L2 距离阈值，distance > 此值的向量
# 软降权保留（乘 RAG_VEC_SOFT_PENALTY），不直接丢弃，给 Reranker 更多候选空间。
# bge-large-zh-v1.5 输出已 L2 归一化，distance 范围 0~2：
#   < 0.8 = 相关, 0.8-1.0 = 弱相关, 1.0-1.2 = 语义相关但词汇不同, > 1.2 = 基本无关
# 诊断（bge-large + reranker）："饮食偏好"→"不吃香菜" dist=1.19,
# "后端代码"→"Docker/JWT" dist=1.01-1.09，这些语义相关但词汇不同的记忆
# 在阈值 1.0 下被软降权到 0.3，分数过低无法被 Reranker 捞回。
# 默认 1.15：让 1.0-1.15 区间的弱相关向量不被降权，1.15+ 的仍降权。
# 降权后 score 仍会被 RAG_MIN_FINAL_SCORE 过滤（0.08），不影响最终输出质量。
RAG_VEC_MAX_DISTANCE = _safe_float(os.getenv("RAG_VEC_MAX_DISTANCE"), 1.15)
# P0-2: 超阈值降权系数。0.3 过低导致语义相关但词汇不同的记忆被过度压制。
# 诊断："饮食偏好"→"不吃香菜" dist=1.19, 降权后 sim=(1-1.19)*0.3=-0.057→0，
# Reranker 完全无法捞回。提高到 0.5：sim=(1-1.19)*0.5=-0.095→0，仍为 0。
# 但 1.15-1.19 区间的记忆（如"川菜馆" dist=1.193）在阈值 1.15 下降权：
# sim=(1-1.193)*0.5=-0.097→0，仍不够。需配合 RAG_VEC_MAX_DISTANCE=1.15 使用，
# 让 dist<1.15 的不被降权，dist>=1.15 的降权系数 0.5 比 0.3 更温和。
RAG_VEC_SOFT_PENALTY = _safe_float(os.getenv("RAG_VEC_SOFT_PENALTY"), 0.6)

# ── 记忆/情绪阈值 (可环境变量覆盖) ──
# 情绪触发安慰记忆检索的强度阈值 (0.0~1.0)
EMOTION_TRIGGER_THRESHOLD = _safe_float(os.getenv("EMOTION_TRIGGER_THRESHOLD"), 0.5)
# B 级场景粘性阈值: 低于此权重时不重排, 防止低质量闲聊触发重排
SCENE_STICKINESS_THRESHOLD = _safe_float(os.getenv("SCENE_STICKINESS_THRESHOLD"), 0.5)

# ── 冷启动路由配置 (环境变量覆盖) ──
# 私有记忆条数: < COLD_MAX 为冷用户(纯FTS), COLD_MAX~WARM_MAX 为温用户(向量低权重), >= WARM_MAX 为热用户(均衡混合)
MEMORY_COLD_MAX = _safe_int(os.getenv("MEMORY_COLD_MAX"), 0)
MEMORY_WARM_MAX = _safe_int(os.getenv("MEMORY_WARM_MAX"), 10)
# 温用户向量融合权重 (0.0~1.0): 冷=0.0, 温=0.2, 热=0.5(均衡)
MEMORY_WARM_VEC_WEIGHT = _safe_float(os.getenv("MEMORY_WARM_VEC_WEIGHT"), 0.6)

# ── P3 记忆蒸馏压缩配置 ──
MAX_EPISODIC_MEMORIES = _safe_int(os.getenv("MAX_EPISODIC_MEMORIES"), 200)
MEMORY_DISTILL_BATCH = _safe_int(os.getenv("MEMORY_DISTILL_BATCH"), 30)
MEMORY_DISTILL_ENABLED = os.getenv("MEMORY_DISTILL_ENABLED", "false").lower() in ("1", "true", "yes")

# ── H1 情景记忆行数上限 (episodic_limiter) ──
MAX_EPISODIC_ROWS = _safe_int(os.getenv("MAX_EPISODIC_ROWS"), 10000)


# ── J-Space 架构优化配置 ──────────────────────────────────────
ENABLE_J_SPACE_HOOKS = os.getenv("ENABLE_J_SPACE_HOOKS", "true").lower() == "true"

# ── emotion_llm 深度情绪分析开关 ──────────────────────────────
# LLM 深度情绪分析已在 fire-and-forget 模式下运行（不阻塞主路径），
# 结果异步持久化到 mental_state（primary + PAD + needs）供下次请求使用。
ENABLE_EMOTION_LLM = os.getenv("ENABLE_EMOTION_LLM", "true").lower() in ("1", "true", "yes")
DIRECTION_REGISTRY_PATH = os.getenv("DIRECTION_REGISTRY_PATH", str(DATA_DIR / "direction_registry.json"))
SIGNAL_STREAM_MAX_HISTORY = _safe_int(os.getenv("SIGNAL_STREAM_MAX_HISTORY"), 1000)
INTERVENTION_DEFAULT_COOLDOWN = _safe_float(os.getenv("INTERVENTION_DEFAULT_COOLDOWN"), 30.0)
