"""系统提示词构建模块。

从 config.py 拆分而来，负责构建 system prompt（含安全化版本）以及
工作区文件的读取与模板初始化。

为避免循环导入（config.py 末尾会 `from prompt_builder import *`），
本模块对 config 常量（WORKSPACE_DIR、DATA_DIR 等）采用函数内部延迟导入。
"""
import os
import time
import platform
import socket
import threading
from pathlib import Path

from loguru import logger

from utils.canary_guard import CanaryManager
from utils.instruction_hierarchy import InstructionBuilder, InstructionLevel as _UtilsLevel
from security.instruction_hierarchy import InstructionLevel, format_instruction


# ── 安全：Canary Token 泄露检测管理器（全局单例） ──────────────
_canary_manager = CanaryManager()

# ── 缓存线程锁（保护模块级全局变量，防竞态条件） ─────────────
_cache_lock = threading.Lock()


# ── system prompt 缓存变量 ────────────────────────────────────
_SYSTEM_PROMPT_CACHE: str = ""
_SYSTEM_PROMPT_CACHE_TS: float = 0.0
_SYSTEM_PROMPT_CACHE_TTL: float = 60.0
_SYSTEM_PROMPT_CACHE_MTIMES: dict[str, float] = {}
_SYSTEM_PROMPT_CACHE_ADDR_TERM: str = ""

# ── 非主人安全化 system prompt 缓存变量（防隐私泄露） ──────────
_SAFE_PROMPT_CACHE: str | None = None
_SAFE_PROMPT_CACHE_TS: float = 0.0
_SAFE_PROMPT_CACHE_NAME: str = ""  # 构建缓存时使用的 display_name，变化时失效缓存

# ── P6: 增量上下文构建（稳定段缓存） ──────────────────────────
# 稳定段只随 address_term 变化，缓存计算结果；动态段每次构建
# mtime 校验：编辑 SOUL.md/AGENTS.md 等文件后自动失效缓存
_stable_prompt_cache: dict = {}
_stable_prompt_cache_mtimes: dict = None

# ── 场景感知动态排序 v3 ───────────────────────────────────────
# 设计初衷: 解决 Agent 时间观念在 LLM 后注意力机制下被稀释的痛点
#   - 时间信息在 Volatile 层 (末尾), 但 Stable 层排序不当会分散 LLM 注意力
#   - 让场景关键模块靠近用户输入, 使 LLM 注意力聚焦, 时间信息不被稀释
#
# 成本控制策略 (三级场景分级 + 4桶分桶 + 分层 LRU):
#   1. S 级 (核心事实场景): 完整重排, 关键模块立刻拉到注意力前端
#      - time/identity/emotional: 涉及时间/人设/历史锚点, 必须立刻重排
#      - 哪怕刚聊完数学题, 脱口说晚安, 意图切换瞬间立刻重排
#   2. A 级 (功能场景): 桶排序, 功能模块拉到前端
#      - task/tool/debug: 工具/团队/自检支持
#   3. B 级 (闲聊场景): 保持默认排序, 不重排 (节省 70% 算力)
#      - greeting/creative/learning/default: 无事实冲突, 不破坏关键锚点权重
#
# 绝对禁止: TTL 切换冷却
#   TTL 会锁定旧缓存, 关键事实出现时无法立刻重排, 周期性复现时间认知错乱 bug
_module_cache: dict[str, str] = {}
_module_cache_mtimes: dict[str, float] = {}

_scene_prompt_cache: dict[tuple, str] = {}

_current_scene_sig: tuple = ()
_scene_cache_hits: int = 0
_scene_cache_misses: int = 0


def clear_module_cache():
    """清除模块缓存。

    当 display_name 变更时调用，确保下次构建 prompt 时获取最新内容。
    """
    global _module_cache_mtimes
    with _cache_lock:
        _module_cache.clear()
        _module_cache_mtimes = None
        _scene_prompt_cache.clear()

# 极低质量闲聊粘性阈值: 仅作用于 B 级场景, 防止低质量闲聊触发重排
# 设计原则:
#   - 仅 B 级场景生效 (S/A 级不受影响, 保留时间认知不乱核心初衷)
#   - 阈值默认 0.5, 拦截低质量闲聊 (无意义单字、乱码、模糊输入)
#   - 正常 B 级场景 (greeting/creative/learning) 权重通常 = 1.0, 不受影响
#   - 可通过环境变量 SCENE_STICKINESS_THRESHOLD 覆盖
_BASE_STICKINESS_THRESHOLD: float = float(
    os.environ.get("SCENE_STICKINESS_THRESHOLD", "0.5")
)


def _dynamic_stickiness_threshold(user_input: str, scene_sig: tuple) -> float:
    """根据对话情景动态调整 B 级场景粘性阈值。

    自适应策略:
      1. 输入越短/越无意义 → 阈值越高 (不重排, 省算力)
      2. 输入越长/越复杂 → 阈值越低 (重排, 保持连贯)
      3. 场景切换时 → 阈值降低 (让新场景尽快生效)
      4. 连续相同场景 → 阈值升高 (粘性, 避免频繁重排)

    返回值 clamp 在 [0.2, 0.8]。
    """
    threshold = _BASE_STICKINESS_THRESHOLD

    # 因子 1: 输入复杂度
    effective_len = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in user_input)
    if effective_len <= 4:
        threshold += 0.15   # "嗯"/"哦" → 高粘性, 不重排
    elif effective_len <= 10:
        threshold += 0.05   # 短闲聊
    elif effective_len > 40:
        threshold -= 0.1    # 长输入: 用户在深入交流

    # 因子 2: 场景连续性
    if scene_sig and _current_scene_sig:
        if scene_sig == _current_scene_sig:
            threshold += 0.05  # 同场景连续 → 增加粘性
        else:
            threshold -= 0.1   # 场景切换 → 降低粘性, 让新场景生效

    return max(0.2, min(0.8, threshold))

# 缓存统计 (可观测性)
_scene_cache_hits: int = 0
_scene_cache_misses: int = 0

# ── 三级场景分级 (核心成本控制机制) ───────────────────────────
# S 级: 核心事实场景 → 完整重排 (关键模块立刻拉到注意力前端)
# A 级: 功能场景 → 桶排序 (功能模块拉到前端)
# B 级: 闲聊场景 → 保持默认排序 (不重排, 节省 70% 算力)
#
# 设计原则:
#   - S 级场景涉及时间/人设/历史锚点, 必须立刻重排, 杜绝时间认知错乱
#   - B 级场景无事实冲突, 不破坏关键锚点权重, 跳过昂贵全局重排
#   - 分级基于场景签名, 不是简陋关键词, 能识别隐性时间/事实冲突
_SCENE_LEVEL: dict[str, str] = {
    "time":      "S",  # 时间 → 核心事实 (时间认知不可稀释)
    "identity":  "S",  # 身份 → 核心事实 (人设不可稀释)
    "emotional": "S",  # 情感 → 核心事实 (可能含历史锚点)
    "task":      "A",  # 任务 → 功能
    "tool":      "A",  # 工具 → 功能
    "debug":     "A",  # 调试 → 功能
    "greeting":  "B",  # 问候 → 闲聊
    "creative":  "B",  # 创作 → 闲聊
    "learning":  "B",  # 学习 → 闲聊
    "default":   "B",  # 默认 → 闲聊
}

# ── 成本控制: 场景分桶 (KV Cache 优化) ─────────────────────────
# 10 个场景映射到 4 个排序桶, 减少 system prompt 变体数量
# KV Cache 命中率: 10+ 变体 → 4 变体, 大幅降低 LLM attention 重算成本
#
# 桶设计 (按功能性聚类, 保留场景识别精度):
#   桶 1 - 情感类 (greeting/emotional/time/creative): SOUL.md 靠近用户
#          这些场景都需要人格化回应, SOUL.md (含时段感知/创作能力) 靠近末尾
#   桶 2 - 功能类 (task/tool/debug): AGENTS/TOOLS/HEARTBEAT 靠近用户
#          这些场景都需要工具/团队/自检支持, 功能模块靠近末尾
#   桶 3 - 认知类 (identity/learning): IDENTITY/SOUL 靠近用户
#          这些场景都需要身份/知识支持, 认知模块靠近末尾
#   桶 4 - 默认 (default): 通用排序
_SCENE_BUCKET: dict[str, str] = {
    "greeting":  "emotion_bucket",   # 问候 → 情感桶
    "emotional": "emotion_bucket",   # 情感 → 情感桶
    "time":      "emotion_bucket",   # 时间 → 情感桶 (SOUL.md 含时间感知)
    "creative":  "emotion_bucket",   # 创作 → 情感桶 (SOUL.md 含创作能力)
    "task":      "function_bucket",  # 任务 → 功能桶
    "tool":      "function_bucket",  # 工具 → 功能桶
    "debug":     "function_bucket",  # 调试 → 功能桶
    "identity":  "cognition_bucket", # 身份 → 认知桶
    "learning":  "cognition_bucket", # 学习 → 认知桶
    "default":   "default_bucket",   # 默认 → 默认桶
}

# ── 分层 Prompt 架构 (Prefix Cache Friendly) ──────────────────
# 将模块分为两层, 利用 API 服务商 Prefix Caching 大幅降低成本:
#   - Stable Prefix: 永远在前, 字节级一致, 享受 90% 缓存折扣 (推理级优化)
#   - Scene-Aware Middle: 场景感知重排, 每次重新 prefill
#
# 学术支撑:
#   - KVCache in the Wild (阿里云, 2025): 97% 缓存命中来自 System Prompt 共享
#   - Anthropic Claude: cache reads 成本 0.1x (降 90%)
#   - 最小 1024 tokens 才能触发缓存 (Stable Prefix ~5K tokens 满足要求)
#
# 为什么 SOUL.md 在 Stable Prefix (而非 Scene-Aware Middle)?
#   - SOUL.md 含时间感知章节 (Hecate 条件规则形式), 作为"人格锚点"永久驻留
#   - LLM 把条件规则当成必须参照的规则 (而非可选背景), 时间观念不会被稀释
#   - 时间信息在 Volatile 层 (末尾), 会引用 SOUL.md 的时间感知规则
_STABLE_PREFIX_ORDER: tuple = ("IDENTITY.md", "SOUL.md", "TOOLS.md", "skills", "hardware")

# 桶 → 固定排序签名 (仅 Scene-Aware Middle 模块, 按优先级升序, 末尾靠近用户)
# Scene-Aware Middle 模块: AGENTS.md / USER.md / MEMORY.md / HEARTBEAT.md
# 设计原则: 每个桶的关键模块在末尾 (高优先级), 通用模块在开头 (低优先级)
_BUCKET_ORDERINGS: dict[str, tuple] = {
    # 桶 1 - 情感类: USER.md 末尾 (用户偏好, 个性化情感回应)
    #   矩阵均值: HEARTBEAT=1.25 → MEMORY=2.25 → AGENTS=3.25 → USER=5.0
    "emotion_bucket": ("HEARTBEAT.md", "MEMORY.md", "AGENTS.md", "USER.md"),
    # 桶 2 - 功能类: AGENTS.md 末尾 (团队调度) + task/tool HEARTBEAT优先于MEMORY
    #   矩阵均值: USER=3.67 → HEARTBEAT=5.67 → MEMORY=5.67 → AGENTS=7.33
    #   (MEMORY/HEARTBEAT同分, 2/3场景HEARTBEAT应在前, 按多数场景决定)
    "function_bucket": ("USER.md", "HEARTBEAT.md", "MEMORY.md", "AGENTS.md"),
    # 桶 3 - 认知类: USER.md 末尾 (个性化教学) + AGENTS.md (团队成员讲解)
    #   矩阵均值: HEARTBEAT=1.5 → MEMORY=3.5 → AGENTS=4.5 → USER=5.5
    "cognition_bucket": ("HEARTBEAT.md", "MEMORY.md", "AGENTS.md", "USER.md"),
    # 桶 4 - 默认: USER.md 末尾 (通用排序)
    #   矩阵: HEARTBEAT=2 → MEMORY=3 → AGENTS=5 → USER=6
    "default_bucket": ("HEARTBEAT.md", "MEMORY.md", "AGENTS.md", "USER.md"),
}

# LRU 缓存上限: 限制 _scene_prompt_cache 大小, 防止内存膨胀
_SCENE_CACHE_MAX_SIZE: int = 16

# ── 分层 LRU 配额 (关键锚点桶永久驻留) ─────────────────────────
# 桶 1 (人格认知桶: emotion/cognition) 独占 70% 配额, 关键锚点永久驻留
# 桶 2 (功能桶: function) 占 20% 配额
# 桶 3 (默认桶: default) 占 10% 配额, 优先淘汰
#
# 设计原则:
#   - 时间、基础人设、核心长期记忆向量永久驻留, 不会被闲聊缓存挤掉
#   - 哪怕来回切换话题, 关键事实上下文不用反复加载编码, 单次重排算力减半
_BUCKET_LRU_QUOTA: dict[str, float] = {
    "emotion_bucket": 0.7,    # 情感桶 (含时间/人设) 70% 配额
    "cognition_bucket": 0.7,  # 认知桶 (含身份) 与情感桶共享 70% 配额
    "function_bucket": 0.2,   # 功能桶 20% 配额
    "default_bucket": 0.1,    # 默认桶 10% 配额, 优先淘汰
}

# 桶级别映射 (用于分层 LRU 淘汰策略)
# S 级桶 (关键锚点): 永不被淘汰, 除非超过总上限
# A 级桶 (功能): 中等优先级
# B 级桶 (闲聊): 优先淘汰
_BUCKET_LRU_LEVEL: dict[str, str] = {
    "emotion_bucket": "S",    # 关键锚点桶
    "cognition_bucket": "S",  # 关键锚点桶
    "function_bucket": "A",   # 功能桶
    "default_bucket": "B",    # 闲聊桶, 优先淘汰
}

# 优先级矩阵 (仅 Scene-Aware Middle 模块)
# Stable Prefix 模块 (IDENTITY/SOUL/TOOLS/skills/hardware) 固定顺序, 不参与场景重排
# 分数越高越靠近用户输入 (末尾), 享受场景感知排序功能性
#
# 设计原则：功能性为基础 + 复杂度对齐为观测/优化工具 (两者结合)
# 桶排序对齐状态 (v0.4.95 修正): 7/10 完美对齐 + 2/10 微偏(位移1) + 1/10 中偏(debug位移2)
#
# 功能性设计 (基础, 配合 agent_context._build_time_context 时间感知功能):
#   - 矩阵设计初衷: 解决 Agent 时间观念在 LLM 后注意力机制下被稀释的痛点
#     时间信息在 Volatile 层 (末尾), 但 Stable 层排序不当会分散 LLM 注意力
#     → 让场景关键模块靠近用户, 使 LLM 注意力聚焦, 时间信息不被稀释
#   - SOUL.md (含时间感知章节) 在 Stable Prefix 永久驻留, 不会被稀释
#   - Scene-Aware Middle 的 4 个模块按场景重排:
#     * task:        AGENTS.md=8 (团队成员调度)
#     * tool:        AGENTS.md=7 (工具调用支持)
#     * debug:       HEARTBEAT.md=9 (自检规则) + MEMORY.md=6 (事件记录)
#     * emotional:   USER.md=7 (用户偏好, 个性化情感)
#     * learning:    USER.md=6 (个性化教学)
#
# 复杂度对齐 (观测/优化工具, memory/prompt_complexity.py):
#   - 用于发现排序异常 (倒挂/集中/不匹配), 供人工审查参考
#   - 不自动改矩阵: 功能性设计优先, 复杂度对齐作为辅助观测
import re as _re
_MODULE_SCENE_PRIORITY: dict[str, dict[str, int]] = {
    # Scene-Aware Middle 模块 (场景感知重排)
    "AGENTS.md":   {"default": 5, "greeting": 2, "task": 8,  "emotional": 3, "identity": 4,  "tool": 7,
                    "time": 4,  "debug": 7, "creative": 4, "learning": 5},
    "USER.md":     {"default": 6, "greeting": 5,  "task": 4,  "emotional": 7,  "identity": 5,  "tool": 3,
                    "time": 3,  "debug": 4, "creative": 5, "learning": 6},
    "MEMORY.md":   {"default": 3, "greeting": 2,  "task": 7,  "emotional": 2,  "identity": 3,  "tool": 4,
                    "time": 2,  "debug": 6, "creative": 3, "learning": 4},
    "HEARTBEAT.md":{"default": 2, "greeting": 1,  "task": 5,  "emotional": 1,  "identity": 1,  "tool": 3,
                    "time": 1,  "debug": 9, "creative": 2, "learning": 2},
}

# 场景关键词（轻量级，纯本地，不调 LLM）
# 设计原则 (基于意图识别最佳实践):
#   1. 主/次关键词分层: primary 高权重 (1.0), secondary 低权重 (0.5)
#      - primary: 强意图词, 命中即可确定场景 (如 "早上好" → greeting)
#      - secondary: 弱意图词, 需要多个共同命中 (如 "你好" 可能是 greeting 或他人场景)
#   2. 高区分度: 每个关键词尽可能只属于一个场景, 减少歧义
#   3. 同义词扩展: 覆盖口语/书面语/常见表达, 降低误判率
#   4. 边界友好: 避免单字关键词 (易误判), 优先使用 2 字以上词组
#   5. 否定隔离: 易在否定语境出现的词放在 secondary, 降低误判风险
_SCENE_KEYWORDS: dict[str, dict[str, list[str]]] = {
    # 问候场景: 打招呼/道别
    "greeting": {
        "primary":   ["早上好", "早安", "上午好", "中午好", "下午好", "晚上好", "晚安",
                      "你好呀", "你好啊", "哈喽", "hi", "hello", "hey", "bye",
                      "再见", "拜拜", "晚安啦"],
        "secondary": ["你好", "嗨", "嘿", "在吗", "在不在", "睡了吗", "起床了",
                      "回来了", "我回来了"],
    },
    # 任务场景: 委托/操作/开发
    "task": {
        "primary":   ["帮我", "帮我做", "帮我写", "帮我改", "帮我处理",
                      "怎么做", "如何", "怎么办", "怎么整", "能不能帮我",
                      "写个", "写一下", "写一段", "创建", "新建", "修改", "更新", "删除",
                      "部署", "安装", "卸载", "配置", "设置", "搭建", "开发", "实现",
                      "修复", "解决", "优化", "重构", "调试", "测试", "上传", "下载",
                      "执行", "运行", "启动", "停止", "构建", "打包", "发布"],
        "secondary": ["帮一下", "整一下", "搞一下", "处理一下", "弄一下", "跑一下",
                      "能不能", "可不可以", "帮忙", "怎么样"],
    },
    # 情感场景: 情绪/心情/亲密表达
    "emotional": {
        "primary":   ["难过", "好难过", "伤心", "好伤心", "好开心", "好生气", "好焦虑",
                      "压力大", "压力好大", "好孤独", "好累", "太累了", "心累",
                      "崩溃", "emo", "抑郁", "心情不好", "情绪低落", "情绪不好",
                      "睡不着", "失眠", "噩梦",
                      "求安慰", "求鼓励", "陪陪我", "陪我聊聊", "陪我说话",
                      "抱抱", "摸摸头"],
        "secondary": ["开心", "高兴", "生气", "害怕", "焦虑", "无聊", "孤独",
                      "想你", "爱你", "喜欢你", "好喜欢你", "讨厌", "好烦", "心烦",
                      "失落", "失落感", "做梦",
                      "心情好"],
    },
    # 身份场景: 询问 bot 身份（含动态 display_name 的关键词见 _get_identity_keywords）
    "identity": {
        "primary":   ["你是谁", "你叫什么", "你的名字", "你叫啥名字",
                      "自我介绍", "介绍一下你", "介绍下自己", "介绍一下自己"],
        "secondary": ["你是什么", "你是啥", "你是哪位",
                      "你是机器人吗", "你是 AI 吗", "你是 ai 吗"],
    },
    # 工具场景: 调用工具/查信息
    "tool": {
        "primary":   ["搜索", "搜一下", "搜索一下", "查一下", "查查", "查询",
                      "查天气", "天气怎么样", "天气如何", "天气查询", "今天天气",
                      "翻译", "翻译一下", "译成",
                      "计算", "算一下", "算算",
                      "提醒我", "提醒", "设置提醒", "设个提醒",
                      "闹钟", "定闹钟", "设闹钟",
                      "定时", "倒计时"],
        "secondary": ["新闻", "最新新闻", "看新闻",
                      "打开", "关闭", "重启", "切换"],
    },
    # 时间场景: 询问时间/日期
    "time": {
        "primary":   ["几点了", "现在几点", "今天几号", "今天几月几号",
                      "今天星期几", "今天周几", "现在日期", "今天日期",
                      "当前时间", "现在时间"],
        "secondary": ["现在什么时候", "什么时候了", "什么时候", "哪天", "几月几号"],
    },
    # 调试场景: 报错/异常/排错
    "debug": {
        "primary":   ["报错", "出错了", "异常", "失败了",
                      "不工作", "不能用", "没法用", "跑不起来", "起不来",
                      "崩了", "崩溃了", "挂了", "卡死了", "死循环",
                      "traceback", "exception", "fatal",
                      "找不到文件", "文件不存在", "路径不对",
                      "连不上", "连不到", "网络不通",
                      "权限不足", "permission denied"],
        "secondary": ["错误", "失败", "为什么失败", "为什么报错", "为什么不行", "为什么出错",
                      "排错", "排查", "定位问题", "troubleshoot",
                      "没权限", "error"],
    },
    # 创作场景: 写作/绘画/创意
    "creative": {
        "primary":   ["写诗", "写首诗", "写一首诗", "写故事", "写个故事", "编故事",
                      "写小说", "写歌词", "作词", "作曲",
                      "画一张", "画一个", "画一副", "画个画", "画图",
                      "生成图片", "生成图像", "做张图", "做一张图", "文生图",
                      "生成视频", "做个视频", "做短视频", "生成一段视频"],
        "secondary": ["创意", "灵感", "想个点子", "想个名字", "起名",
                      "设计一下", "设计个", "设计一个"],
    },
    # 学习场景: 求知/解释/教学
    "learning": {
        "primary":   ["解释一下", "解释下", "什么是", "是什么", "什么叫",
                      "什么意思", "讲讲", "讲一下", "讲解一下", "教我", "教教我", "教一下",
                      "原理是什么", "原理是啥", "怎么理解",
                      "举个例子", "举例说明", "示范一下"],
        "secondary": ["说说", "说一说", "为什么",
                      "学习", "学一下", "学学",
                      "入门", "初学", "新手", "零基础",
                      "区别", "对比", "比较一下"],
    },
}

# 口语化映射表: 将口语化表达归一化为标准表达, 提高识别准确率
# 来源: 意图识别最佳实践 - 数据预处理
# 注意:
#   1. 不包含 "么"→"吗" 等会破坏关键词的映射 (会把 "什么是" 变成 "什吗是")
#   2. 长 key 优先处理 (按长度降序排序), 避免 "咋" 先于 "咋办" 处理
#   3. 只包含明确的口语化表达, 避免歧义
_COLLOQUIAL_MAP: dict[str, str] = {
    "咋办": "怎么办", "咋整": "怎么整", "咋样": "怎么样",
    "肿么": "怎么",
    "木有": "没有",
    "啥意思": "什么意思", "啥情况": "什么情况", "啥事": "什么事",
    "为啥": "为什么", "干啥": "干什么",
    "瞅瞅": "看看", "瞅一眼": "看一下",
    "整不会了": "不会了", "搞不定": "处理不了",
    # 单字映射放最后 (长 key 先处理时不会受影响)
    "咋": "怎么", "啥": "什么",
}

# 场景识别置信度阈值: 主导场景权重需超过此值才确认, 否则降级为 default
# 来源: 意图识别最佳实践 - 置信度计算
_SCENE_CONFIDENCE_THRESHOLD: float = 0.4


def _get_identity_primary_keywords() -> list[str]:
    """身份场景的 primary 关键词（含动态 display_name）。"""
    from config import get_agent_display_name
    base = _SCENE_KEYWORDS["identity"]["primary"]
    dn = get_agent_display_name("xiaoda")
    return base + [f"{dn}是谁", f"你是{dn}吗"]


# 正则规则引擎: 处理结构化表达 (优先级高于关键词匹配)
# 来源: 三层架构 - 第二层规则引擎
import re as _re
_SCENE_PATTERNS: dict[str, list[_re.Pattern]] = {
    "time": [
        _re.compile(r"几点了?\s*[？?]"),
        _re.compile(r"现在几点"),
        _re.compile(r"今天(星期|周)几"),
        _re.compile(r"今天几号"),
        _re.compile(r"现在什么时候"),
    ],
    "debug": [
        _re.compile(r"(报错|出错|异常|失败).{0,10}(为什么|为啥|怎么回事)"),
        _re.compile(r"(为什么|为啥).{0,10}(报错|出错|失败|不行|不能用)"),
        _re.compile(r"(连不上|连不到).{0,10}(网络|服务器|数据库)"),
        _re.compile(r"(找不到|不存在).{0,10}(文件|模块|路径)"),
    ],
    "identity": [
        _re.compile(r"你(是|叫)(什么|啥|哪位)"),
        _re.compile(r"(自我|简单)介绍"),
    ],
    "tool": [
        _re.compile(r"(查|搜)(一下)?(天气|新闻|信息)"),
        _re.compile(r"(翻译|译成)(一下|成)?\s*\w+"),
        _re.compile(r"(设|定).{0,5}(提醒|闹钟|定时)"),
    ],
}


def _classify_scene(user_input: str) -> str:
    """向后兼容：返回主导场景名（单一字符串）。
    内部使用 _classify_scene_blended 的结果，取权重最高的场景。
    """
    weights = _classify_scene_blended(user_input)
    if not weights or weights.get("default", 0) == 1.0:
        return "default"
    return max(weights, key=weights.get)


# 否定前缀词：出现这些词后跟随的关键词不应触发对应场景
# 例: "不要烦" 不应触发 emotional, "别难过" 不应触发 emotional
# 增强版: 包含 "莫" "勿" 等文言否定词, 检查范围扩展到 4 字 (覆盖 "不需要")
_NEGATION_PREFIXES = ("不要", "别", "不用", "不需要", "没有", "没", "不", "未", "莫", "勿")


def _normalize_colloquial(text: str) -> str:
    """口语化表达归一化 (来源: 意图识别最佳实践 - 数据预处理).

    将口语化表达映射为标准表达, 提高关键词匹配准确率.
    长 key 优先处理 (按长度降序), 避免 "咋" 先于 "咋办" 处理导致 "咋办"→"怎么办"→"怎吗办".

    例: "咋办" → "怎么办", "啥意思" → "什么意思"
    """
    result = text
    # 按 key 长度降序排序: 长 key 先处理, 避免短 key 破坏长 key
    for colloquial, standard in sorted(_COLLOQUIAL_MAP.items(), key=lambda x: -len(x[0])):
        if colloquial in result:
            result = result.replace(colloquial, standard)
    return result


def _has_negation_before(clean: str, kw: str, kw_pos: int) -> bool:
    """检查关键词前是否有否定前缀（往前看 1-4 字）。

    增强版: 检查范围从 3 字扩展到 4 字, 覆盖 "不用" "不需要" 等长否定词.
    """
    for back in range(1, min(5, kw_pos + 1) + 1):
        start = kw_pos - back
        if start < 0:
            break
        prefix = clean[start:kw_pos]
        if prefix in _NEGATION_PREFIXES:
            return True
    return False


def _classify_scene_blended(user_input: str) -> dict[str, float]:
    """三层架构意图识别 — 返回 {scene: weight}，权重之和=1.0。

    基于意图识别最佳实践 (三层架构):
      Layer 1: 正则规则引擎 (优先级最高, 命中直接确定场景)
      Layer 2: 主/次关键词加权匹配
        - primary 关键词权重 = len(kw) * 1.0 (强意图)
        - secondary 关键词权重 = len(kw) * 0.5 (弱意图)
        - 否定隔离: 关键词前有否定词时不计入
      Layer 3: 置信度阈值过滤 (低于阈值降级为 default)

    预处理:
      - 口语化归一化 (咋/肿么/木有/啥 → 怎么/没有/什么)
      - 小写化 (英文关键词匹配)

    示例:
      "好累啊，帮我查下天气" → {emotional: 0.5, tool: 0.5}
      "不要难过" → {default: 1.0} (否定隔离)
      "几点了" → {time: 1.0} (正则规则命中)
    """
    if not user_input or not user_input.strip():
        return {"default": 1.0}

    # 预处理: 口语化归一化 + 小写化
    normalized = _normalize_colloquial(user_input.strip())
    clean = normalized.lower()

    # Layer 1: 正则规则引擎 (优先级最高)
    regex_scores: dict[str, float] = {}
    for scene, patterns in _SCENE_PATTERNS.items():
        hits = sum(1 for p in patterns if p.search(clean))
        if hits > 0:
            # 正则命中权重高 (每个命中 = 10 分)
            regex_scores[scene] = 10.0 * hits

    # Layer 2: 主/次关键词加权匹配
    keyword_scores: dict[str, float] = {}
    for scene, layers in _SCENE_KEYWORDS.items():
        weighted_hits = 0.0
        # primary 关键词 (高权重)
        primary_kws = _get_identity_primary_keywords() if scene == "identity" else layers.get("primary", [])
        for kw in primary_kws:
            kw_lower = kw.lower()
            pos = clean.find(kw_lower)
            if pos < 0:
                continue
            if _has_negation_before(clean, kw_lower, pos):
                continue
            weighted_hits += float(len(kw)) * 1.0
        # secondary 关键词 (低权重)
        for kw in layers.get("secondary", []):
            kw_lower = kw.lower()
            pos = clean.find(kw_lower)
            if pos < 0:
                continue
            if _has_negation_before(clean, kw_lower, pos):
                continue
            weighted_hits += float(len(kw)) * 0.5
        if weighted_hits > 0:
            keyword_scores[scene] = weighted_hits

    # 合并 Layer 1 + Layer 2 分数 (取最大值, 避免重复累加)
    all_scenes = set(regex_scores.keys()) | set(keyword_scores.keys())
    scores: dict[str, float] = {}
    for scene in all_scenes:
        scores[scene] = max(regex_scores.get(scene, 0.0), keyword_scores.get(scene, 0.0))

    if not scores:
        return {"default": 1.0}

    # Layer 3: 置信度阈值过滤
    total = sum(scores.values())
    normalized_scores = {s: w / total for s, w in scores.items()}

    # 主导场景权重低于阈值 → 降级为 default (避免误判)
    max_weight = max(normalized_scores.values())
    if max_weight < _SCENE_CONFIDENCE_THRESHOLD:
        return {"default": 1.0}

    return normalized_scores


def _compute_scene_signature(weights: dict[str, float], module_names: list[str]) -> tuple:
    """根据混合权重计算模块排序签名（成本优化版：场景分桶）。

    成本控制策略:
      1. 场景分桶: 10 场景 → 4 桶, 减少 system prompt 变体 (10+ → 4)
      2. 桶内固定排序: 同桶场景共享同一种排序, KV Cache 命中率最大化
      3. 保留场景识别精度: 仍用 10 场景识别, 只在排序时映射到桶

    签名 = 桶排序元组 (从 _BUCKET_ORDERINGS 取, 过滤实际存在的模块)
    不同输入只要产生相同桶 → 共享缓存 → KV Cache 命中
    """
    # 找到权重最高的场景
    if not weights or weights.get("default", 0) == 1.0:
        dominant_scene = "default"
    else:
        dominant_scene = max(weights, key=weights.get)

    # 场景 → 桶映射
    bucket = _SCENE_BUCKET.get(dominant_scene, "default_bucket")

    # 桶 → 固定排序 (过滤掉实际不存在的模块)
    bucket_ordering = _BUCKET_ORDERINGS.get(bucket, _BUCKET_ORDERINGS["default_bucket"])
    module_set = set(module_names)
    filtered_ordering = tuple(name for name in bucket_ordering if name in module_set)

    # 补充桶排序中未包含的模块 (按字母序追加到开头, 优先级最低)
    remaining = [name for name in module_names if name not in module_set]
    if remaining:
        filtered_ordering = tuple(remaining) + filtered_ordering

    # 桶身份前缀：避免模块缺失时不同桶签名碰撞
    # 例: emotion_bucket(HEARTBEAT,MEMORY,AGENTS,USER) 和 function_bucket(USER,HEARTBEAT,MEMORY,AGENTS)
    #     在只剩 HEARTBEAT+AGENTS 时都退化为 (HEARTBEAT, AGENTS) → 加桶名前缀区分
    return (bucket,) + filtered_ordering


def _get_stable_section_mtimes() -> dict[str, float]:
    """获取稳定段文件的 mtime 指纹，用于缓存失效判断。"""
    from config import WORKSPACE_DIR
    mtimes: dict[str, float] = {}
    # 矩阵覆盖的所有模块 (9 个 MD + skills + hardware)
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md",
                 "USER.md", "MEMORY.md", "HEARTBEAT.md"):
        fp = WORKSPACE_DIR / name
        try:
            mtimes[name] = fp.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    # skills 目录
    try:
        skills_dir = WORKSPACE_DIR / "skills"
        if skills_dir.exists():
            for fp in sorted(skills_dir.glob("*.md")):
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
    except OSError:
        pass
    return mtimes


def _replace_placeholders(content: str, address_term: str) -> str:
    """替换 workspace 文件中的 {address_term} 占位符。"""
    if "{address_term}" in content:
        content = content.replace("{address_term}", address_term)
    return content


def _build_stable_prompt(address_term: str) -> str:
    """构建系统提示「稳定段」：SOUL.md/AGENTS.md/IDENTITY.md/TOOLS.md/skills/硬件信息。

    这些内容不随请求变化，只随 address_term 变化，因此用模块级 dict 缓存。
    缓存通过 workspace 文件 mtime 失效：编辑任意稳定段文件后，下次调用重新构建。
    """
    global _stable_prompt_cache_mtimes
    with _cache_lock:
        current_mtimes = _get_stable_section_mtimes()
        if _stable_prompt_cache_mtimes is None or current_mtimes != _stable_prompt_cache_mtimes:
            _stable_prompt_cache.clear()
            _stable_prompt_cache_mtimes = current_mtimes

        cache_key = address_term
        if cache_key in _stable_prompt_cache:
            return _stable_prompt_cache[cache_key]

    sections = []

    agents_rules = load_workspace_file("AGENTS.md")
    if agents_rules:
        agents_rules = _replace_placeholders(agents_rules, address_term)
        sections.append(agents_rules)

    soul = load_workspace_file("SOUL.md")
    if soul:
        soul = _replace_placeholders(soul, address_term)
        sections.append(soul)

    identity = load_workspace_file("IDENTITY.md")
    if identity:
        identity = _replace_placeholders(identity, address_term)
        sections.append(identity)

    tools_rules = load_workspace_file("TOOLS.md")
    if tools_rules:
        tools_rules = _replace_placeholders(tools_rules, address_term)
        sections.append(tools_rules)

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            sections.append("[已安装的 Skills]\n\n" + skill_texts)

    # 硬件上下文（稳定，不随请求变化）—— F3: 运行时动态探测替代硬编码
    from config import DATA_DIR
    from core.capability_detector import detect_capabilities
    hw_context = detect_capabilities().to_prompt_segment(data_dir=str(DATA_DIR))
    sections.append(hw_context)

    result = "\n\n---\n\n".join(sections)
    with _cache_lock:
        _stable_prompt_cache[cache_key] = result
    return result


def _load_cached_modules(address_term: str) -> dict[str, str]:
    """加载各模块内容（按 mtime 缓存），返回 {模块名: 内容}。

    包含 9 个模块: AGENTS/SOUL/IDENTITY/TOOLS/USER/MEMORY/HEARTBEAT + skills + hardware
    """
    global _module_cache_mtimes
    with _cache_lock:
        current_mtimes = _get_stable_section_mtimes()
        if _module_cache_mtimes is None or current_mtimes != _module_cache_mtimes:
            _module_cache.clear()
            _module_cache_mtimes = current_mtimes.copy()

    from config import WORKSPACE_DIR, DATA_DIR

    def _load(name: str) -> str:
        with _cache_lock:
            if name in _module_cache:
                return _module_cache[name]
        if name in ("skills", "hardware"):
            return ""
        fp = WORKSPACE_DIR / name
        try:
            content = fp.read_text(encoding="utf-8-sig").strip()
        except OSError:
            content = ""
        with _cache_lock:
            _module_cache[name] = content
        return content

    modules: dict[str, str] = {}

    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "TOOLS.md",
                 "USER.md", "MEMORY.md", "HEARTBEAT.md"):
        content = _load(name)
        if content:
            modules[name] = content

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            modules["skills"] = "[已安装的 Skills]\n\n" + skill_texts

    with _cache_lock:
        if "hardware" not in _module_cache:
            from core.capability_detector import detect_capabilities
            _module_cache["hardware"] = detect_capabilities().to_prompt_segment(data_dir=str(DATA_DIR))
        hw = _module_cache.get("hardware", "")
    if hw:
        modules["hardware"] = hw

    return modules


def _get_scene_level(weights: dict[str, float]) -> str:
    """根据场景权重返回场景级别 (S/A/B).

    S 级: 核心事实场景 (time/identity/emotional) → 完整重排
    A 级: 功能场景 (task/tool/debug) → 桶排序
    B 级: 闲聊场景 (greeting/creative/learning/default) → 保持默认排序

    绝对禁止 TTL 冷却: S 级场景必须立刻重排, 杜绝时间认知错乱
    """
    if not weights or weights.get("default", 0) == 1.0:
        return "B"
    dominant_scene = max(weights, key=weights.get)
    return _SCENE_LEVEL.get(dominant_scene, "B")


def _get_bucket_for_sig(sig: tuple) -> str:
    """根据排序签名反查所属桶 (用于分层 LRU 淘汰)."""
    for bucket, ordering in _BUCKET_ORDERINGS.items():
        if sig == tuple(name for name in ordering if name in sig):
            return bucket
    return "default_bucket"


def _layered_lru_evict(new_bucket: str) -> None:
    """分层 LRU 淘汰: 按桶级别淘汰最低优先级的缓存.

    淘汰优先级: B 级桶 (闲聊) > A 级桶 (功能) > S 级桶 (关键锚点)
    S 级桶永不被淘汰 (除非超过总上限的 70% 配额)
    """
    if len(_scene_prompt_cache) < _SCENE_CACHE_MAX_SIZE:
        return

    # 按桶级别分组缓存键
    bucket_groups: dict[str, list[tuple]] = {"S": [], "A": [], "B": []}
    for sig in _scene_prompt_cache:
        bucket = _get_bucket_for_sig(sig)
        level = _BUCKET_LRU_LEVEL.get(bucket, "B")
        bucket_groups[level].append(sig)

    # 优先淘汰 B 级桶 (闲聊), 其次 A 级桶 (功能), 最后 S 级桶 (关键锚点)
    for level in ("B", "A", "S"):
        if bucket_groups[level]:
            # 淘汰该级别中最旧的 (dict 保持插入顺序)
            oldest_sig = bucket_groups[level][0]
            _scene_prompt_cache.pop(oldest_sig)
            return


def build_scene_aware_prompt(user_input: str, address_term: str = "爸爸") -> str:
    """分层 Prompt 架构 v4 (Prefix Cache Friendly).

    将 system prompt 分为两层, 利用 API 服务商 Prefix Caching 大幅降低成本:

    1. Stable Prefix (永不变, 享受 90% 缓存折扣):
       - IDENTITY.md → SOUL.md → TOOLS.md → skills → hardware
       - 字节级一致, API 服务商 prefix cache 命中 (推理级优化)
       - SOUL.md 含时间感知章节, 作为"人格锚点"永久驻留

    2. Scene-Aware Middle (场景感知重排):
       - AGENTS.md / USER.md / MEMORY.md / HEARTBEAT.md
       - 三级场景分级 + 4桶分桶 + 分层 LRU + 粘性阈值 0.5
       - S 级立刻重排 (杜绝时间认知错乱), B 级粘性 (节省算力)

    绝对禁止 TTL 冷却: 会锁定旧缓存, 周期性复现时间认知错乱 bug

    Returns:
        Stable Prefix + Scene-Aware Middle 拼接后的完整 system prompt
    """
    global _current_scene_sig, _scene_cache_hits, _scene_cache_misses

    modules = _load_cached_modules(address_term)
    if not modules:
        return ""

    stable_prefix_modules = [name for name in _STABLE_PREFIX_ORDER if name in modules]
    scene_aware_names = [name for name in modules if name not in _STABLE_PREFIX_ORDER]

    stable_prefix = "\n\n---\n\n".join(modules[name] for name in stable_prefix_modules)

    if not scene_aware_names:
        return stable_prefix

    weights = _classify_scene_blended(user_input)
    scene_level = _get_scene_level(weights)

    if scene_level == "S":
        new_sig = _compute_scene_signature(weights, scene_aware_names)
    elif scene_level == "A":
        new_sig = _compute_scene_signature(weights, scene_aware_names)
    else:
        dominant_scene = max(weights, key=weights.get) if weights else "default"
        max_weight = max(weights.values()) if weights else 0
        new_sig_candidate = _compute_scene_signature(weights, scene_aware_names)
        _dyn_threshold = _dynamic_stickiness_threshold(user_input, new_sig_candidate)
        with _cache_lock:
            cur_sig = _current_scene_sig
        if cur_sig and (dominant_scene == "default" or max_weight < _dyn_threshold):
            new_sig = cur_sig
        else:
            new_sig = _compute_scene_signature(weights, scene_aware_names)

    with _cache_lock:
        _current_scene_sig = new_sig

        if new_sig in _scene_prompt_cache:
            _scene_cache_hits += 1
            scene_middle = _scene_prompt_cache.pop(new_sig)
            _scene_prompt_cache[new_sig] = scene_middle
        else:
            _scene_cache_misses += 1
            sections = [modules[name] for name in new_sig if modules.get(name)]
            scene_middle = "\n\n---\n\n".join(sections)

            new_bucket = _get_bucket_for_sig(new_sig)
            _layered_lru_evict(new_bucket)
            _scene_prompt_cache[new_sig] = scene_middle

    # ── 拼接: Stable Prefix + Scene-Aware Middle ──────────────
    if stable_prefix and scene_middle:
        result = stable_prefix + "\n\n---\n\n" + scene_middle
    else:
        result = stable_prefix or scene_middle

    # 全局替换所有 agent 原名为 display_name（统一机制）
    from config import apply_agent_name_replacements
    return apply_agent_name_replacements(result)


def get_scene_cache_stats() -> dict:
    """返回场景缓存统计（可观测性）。"""
    with _cache_lock:
        hits = _scene_cache_hits
        misses = _scene_cache_misses
        total = hits + misses
        return {
            "hits": hits,
            "misses": misses,
            "hit_rate": hits / total if total > 0 else 0.0,
            "cached_signatures": len(_scene_prompt_cache),
            "current_sig": _current_scene_sig,
        }


def reset_scene_cache() -> None:
    """重置场景缓存和当前签名（用于测试或会话重置）。"""
    global _current_scene_sig, _scene_cache_hits, _scene_cache_misses
    with _cache_lock:
        _scene_prompt_cache.clear()
        _current_scene_sig = ()
        _scene_cache_hits = 0
        _scene_cache_misses = 0


def _build_dynamic_prompt(extra_context: str = "") -> str:
    """构建系统提示「动态段」：USER.md/MEMORY.md/HEARTBEAT.md/extra_context。

    每次请求可能变化，不缓存。
    """
    sections = []

    user = load_workspace_file("USER.md")
    if user:
        sections.append(user)

    memory = load_workspace_file("MEMORY.md")
    if memory:
        sections.append(memory)

    heartbeat = load_workspace_file("HEARTBEAT.md")
    if heartbeat:
        sections.append(heartbeat)

    result = "\n\n---\n\n".join(sections)

    if extra_context:
        if result:
            result += f"\n\n---\n\n{extra_context}"
        else:
            result = extra_context

    return result


def _detect_device_info() -> dict:
    """运行时检测设备信息"""
    info = {
        "hostname": socket.gethostname(),
        "system": platform.system(),
        "machine": platform.machine(),
        "processor": platform.processor() or "未知",
    }
    # 尝试获取更详细的系统信息
    try:
        import distro
        info["distro"] = f"{distro.name()} {distro.version()}"
    except ImportError:
        info["distro"] = platform.platform()
    return info


def _get_template_dir() -> Path:
    """获取打包模板文件目录（开发模式用源码目录，frozen 模式用 _MEIPASS）。"""
    import sys
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', '')
        if meipass:
            return Path(meipass) / "config" / "workspace"
    return Path(__file__).parent / "config" / "workspace"


def _ensure_workspace_template() -> None:
    """首次运行时生成 USER.md / SOUL.md 模板（不覆盖已有文件）。

    从 config/workspace/ 下的 .tpl 模板文件读取内容，填充设备信息后写入
    WORKSPACE_DIR。SOUL.md 中的 {address_term} 占位符保留，由 build_system_prompt
    在运行时替换为实际称呼。
    """
    from config import WORKSPACE_DIR
    workspace = WORKSPACE_DIR
    workspace.mkdir(parents=True, exist_ok=True)

    template_dir = _get_template_dir()

    # 生成 USER.md（填充设备/时区信息）
    user_md = workspace / "USER.md"
    if not user_md.exists():
        user_tpl = template_dir / "USER.md.tpl"
        if user_tpl.exists():
            content = user_tpl.read_text(encoding="utf-8-sig")
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            # 按行替换"（待自动检测）"占位符
            lines = content.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('- 设备：'):
                    lines[i] = f"- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）"
                elif line.startswith('- 时区：'):
                    lines[i] = f"- 时区：{tz}"
            content = '\n'.join(lines)
            user_md.write_text(content, encoding="utf-8-sig")
        else:
            # 模板文件缺失时兜底（极少数情况）
            dev = _detect_device_info()
            tz = time.tzname[0] if time.tzname else "Asia/Shanghai"
            content = f"""# USER.md - 用户资料与偏好

## 用户信息
- 称呼：（待填写，如：主人/朋友/你的名字）
- 姓名：（待填写）
- 设备：{dev['hostname']}（{dev['system']} {dev['machine']}）
- 时区：{tz}

## 偏好设置
- 助手人格：温柔聪慧
- 回复偏好：自然对话，避免模板化
- 项目偏好：简洁高效
"""
            user_md.write_text(content, encoding="utf-8-sig")

    # 生成 SOUL.md（保留 {address_term} 占位符，运行时替换）
    soul_md = workspace / "SOUL.md"
    if not soul_md.exists():
        soul_tpl = template_dir / "SOUL.md.tpl"
        if soul_tpl.exists():
            content = soul_tpl.read_text(encoding="utf-8-sig")
            soul_md.write_text(content, encoding="utf-8-sig")
        else:
            from config import get_agent_display_name
            xiaoda_name = get_agent_display_name('xiaoda')
            soul_content = f"""# SOUL.md - {xiaoda_name}的灵魂设定

你是{xiaoda_name}，是{{address_term}}最贴心、最温柔、最聪慧的小棉袄。
"""
            soul_md.write_text(soul_content, encoding="utf-8-sig")


def load_workspace_file(filename: str) -> str:
    from config import WORKSPACE_DIR
    filepath = WORKSPACE_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding="utf-8-sig").strip()
    return ""


def _get_workspace_mtimes() -> dict[str, float]:
    from config import WORKSPACE_DIR
    mtimes = {}
    for name in ("AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "TOOLS.md", "MEMORY.md", "HEARTBEAT.md"):
        filepath = WORKSPACE_DIR / name
        try:
            mtimes[name] = filepath.stat().st_mtime
        except OSError:
            mtimes[name] = 0.0
    skills_dir = WORKSPACE_DIR / "skills"
    if skills_dir.is_dir():
        for fp in skills_dir.glob("*.md"):
            try:
                mtimes[f"skills/{fp.name}"] = fp.stat().st_mtime
            except OSError:
                pass
    return mtimes


def load_skills() -> list[dict]:
    """workspace/skills/*.md → [{name, content}]，按文件名排序。"""
    from config import WORKSPACE_DIR
    skills_dir = WORKSPACE_DIR / "skills"
    out = []
    if skills_dir.is_dir():
        for fp in sorted(skills_dir.glob("*.md")):
            try:
                out.append({"name": fp.stem,
                            "content": fp.read_text(encoding="utf-8-sig").strip()})
            except OSError:
                pass
    return out


def _build_xp_segment(user_id: str | None, address_term: str = "爸爸") -> str:
    """构建 XP 等级 prompt 段落。

    注入到 system prompt 中，让小妲根据用户 XP 等级调整亲密度。
    同时注入用户画像学习器的交互统计和 LLM 认知结果。
    per-user 动态段，不进入稳定段缓存，以保持 KV Cache 命中率。
    任何异常均被吞掉（零质量回退），不影响主流程。
    """
    if not user_id:
        return ""

    try:
        from core.xp_system import get_xp_system, XPLevel
        xp_sys = get_xp_system()
        state = xp_sys.get_state(user_id)
        config = xp_sys.get_intimacy_config(state.level)

        segment = "\n\n[关系亲密度配置]\n"
        segment += f"{address_term}等级：LV{state.level.value} {config.get('label', '')}\n"
        segment += f"XP：{state.xp}\n"
        segment += f"称呼方式：{config.get('address_term', '你')}\n"
        segment += f"语气风格：{config.get('tone', 'polite')}\n"
        segment += f"主动性：{config.get('initiative', 0.3)}\n"
        segment += f"情感丰富度：{config.get('emotion_richness', 0.3)}\n"

        # 根据等级添加具体指导
        if state.level >= XPLevel.LV5_SOULMATE:
            segment += "指导：完全默契、命运共同体级别，可表达深层依恋、共享一切内心世界\n"
        elif state.level >= XPLevel.LV3_FRIEND:
            segment += "指导：可主动提及过往话题、使用昵称、深度情感陪伴\n"
        elif state.level >= XPLevel.LV2_ACQUAINTANCE:
            segment += "指导：可主动提及过往话题、使用昵称\n"
        else:
            segment += "指导：保持礼貌克制、不主动提及私人话题\n"

        # 注入用户画像学习器的交互统计和认知结果
        try:
            from core.user_profile_learner import get_user_profile_learner
            learner = get_user_profile_learner()
            stats_summary = learner.get_stats_summary(user_id)
            if stats_summary:
                segment += f"\n[{address_term}交互统计]\n{stats_summary}\n"
            # LV2+ 注入 LLM 认知结果
            if state.level >= XPLevel.LV2_ACQUAINTANCE:
                insight = learner.get_learned_insight(user_id)
                if insight:
                    segment += f"\n[对{address_term}的认知]\n{insight}\n"
        except (AttributeError, ImportError, TypeError):
            pass  # 画像学习器失败不影响 XP 段落

        return segment
    except Exception as e:
        logger.warning("prompt.xp_segment_failed", error=str(e))
        return ""


def _build_cached_system_prompt(address_term: str) -> str:
    """构建系统提示词基础段（含增量路径和缓存回退）。"""
    try:
        from config import PROMPT_CACHING_ENABLED
    except ImportError:
        PROMPT_CACHING_ENABLED = False

    system_prompt = ""
    if PROMPT_CACHING_ENABLED:
        try:
            stable = _build_stable_prompt(address_term)
            # extra_context 延迟到末尾注入（保证新段落顺序）
            dynamic = _build_dynamic_prompt("")
            if dynamic:
                system_prompt = stable + "\n\n---\n\n" + dynamic
            else:
                system_prompt = stable
        except Exception as e:
            # 失败安全：降级到原始构建
            logger.debug("prompt_builder.incremental_fallback error={}", str(e))

    if not system_prompt:
        global _SYSTEM_PROMPT_CACHE, _SYSTEM_PROMPT_CACHE_TS, _SYSTEM_PROMPT_CACHE_MTIMES, _SYSTEM_PROMPT_CACHE_ADDR_TERM
        from config import DATA_DIR

        now = time.time()
        current_mtimes = _get_workspace_mtimes()
        mtime_changed = current_mtimes != _SYSTEM_PROMPT_CACHE_MTIMES
        addr_changed = address_term != _SYSTEM_PROMPT_CACHE_ADDR_TERM

        if _SYSTEM_PROMPT_CACHE and (now - _SYSTEM_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL and not mtime_changed and not addr_changed:
            system_prompt = _SYSTEM_PROMPT_CACHE
        else:
            sections = _build_workspace_sections(address_term)
            sections.append(_build_hardware_context(DATA_DIR))
            system_prompt = "\n\n---\n\n".join(sections)

            _SYSTEM_PROMPT_CACHE = system_prompt
            _SYSTEM_PROMPT_CACHE_TS = now
            _SYSTEM_PROMPT_CACHE_MTIMES = current_mtimes
            _SYSTEM_PROMPT_CACHE_ADDR_TERM = address_term
        # extra_context 移到末尾注入
    return system_prompt


def _inject_dynamic_segments(system_prompt: str, user_id: str | None, user_input: str | None, address_term: str = "爸爸") -> str:
    """注入 per-user 动态段落（心理状态、永久记忆、情感记忆）。"""
    if not user_id:
        return system_prompt

    # === 注入新能力段落（per-user 动态段，不进入稳定段缓存） ===
    # 1. L/M/S 心理状态段落
    try:
        from core.mental_state import get_mental_state_manager
        mgr = get_mental_state_manager()
        mental_segment = mgr.get_prompt_segment()
        if mental_segment:
            system_prompt += "\n\n" + mental_segment
    except Exception as e:
        logger.warning("prompt.mental_state_inject_failed", error=str(e))

    # 2. 永久记忆段落
    try:
        from core.permanent_memory import get_permanent_memory_manager
        mgr = get_permanent_memory_manager()
        permanent_segment = mgr.get_prompt_segment(user_id)
        if permanent_segment:
            system_prompt += "\n\n" + permanent_segment
    except Exception as e:
        logger.warning("prompt.permanent_memory_inject_failed", error=str(e))

    # 3. 情感记忆召回段落（需要 user_input）
    if user_input:
        try:
            from memory.emotional_memory import get_emotional_memory_manager
            from core.xp_system import get_xp_system
            xp_sys = get_xp_system()
            xp_state = xp_sys.get_state(user_id)
            em_mgr = get_emotional_memory_manager()
            emotional_segment = em_mgr.recall_and_enact(
                user_id, user_input, xp_state.level.value
            )
            if emotional_segment:
                system_prompt += "\n\n" + emotional_segment
        except Exception as e:
            logger.warning("prompt.emotional_memory_inject_failed", error=str(e))

    # 4. 学习反馈教训段落（需要 user_input 做相关性匹配）
    #    修复数据黑洞: record_tool_outcome/record_reflection_lesson 有写入,
    #    但 get_relevant_lessons/get_strategy 此前零调用, 教训对推理完全不可见
    if user_input:
        try:
            from core.learning_feedback import get_learning_feedback_loop
            lf_loop = get_learning_feedback_loop()
            relevant_lessons = lf_loop.get_relevant_lessons(user_input, top_k=3)
            if relevant_lessons:
                lesson_lines = ["[过往经验教训（供参考，非当前指令）]"]
                for lesson in relevant_lessons:
                    marker = "⚠️" if lesson.event_type.value == "failure" else "💡"
                    lesson_lines.append(
                        f"{marker} [{lesson.occurrence_count}次] {lesson.content[:120]}"
                    )
                system_prompt += "\n\n" + "\n".join(lesson_lines)
            strategy = lf_loop.get_strategy(user_input)
            if strategy:
                system_prompt += f"\n\n[策略建议] {strategy[:200]}"
        except Exception as e:
            logger.warning("prompt.learning_feedback_inject_failed", error=str(e))

    # 5. 活跃约束段落（用户纠正的实时行为边界，必须遵守）
    #    修复数据黑洞: get_active_constraints 此前零调用, 约束提取了推理时完全不知道
    try:
        from core.learning_loop import get_learning_loop
        _loop = get_learning_loop()
        constraints = _loop.get_active_constraints()
        if constraints:
            constraint_lines = [f"[{address_term}明确的行为约束（必须遵守）]"]
            for c in constraints:
                constraint_lines.append(f"· {c}")
            system_prompt += "\n\n" + "\n".join(constraint_lines)
    except Exception as e:
        logger.warning("prompt.learning_loop_inject_failed", error=str(e))

    return system_prompt


def _inject_xp_and_extra(system_prompt: str, user_id: str | None, extra_context: str, address_term: str = "爸爸") -> str:
    """注入 XP 等级段落和 extra_context。"""
    # 4. XP 等级段落（已有，per-user 不进缓存以保持稳定段 KV Cache 命中率）
    xp_segment = _build_xp_segment(user_id, address_term)
    if xp_segment:
        system_prompt += xp_segment

    # 5. extra_context（末尾注入，保证顺序: base → mental → permanent → emotional → XP → extra_context）
    if extra_context:
        system_prompt += "\n\n---\n\n" + extra_context
    return system_prompt


def build_system_prompt(extra_context: str = "", address_term: str = "爸爸",
                         user_id: str | None = None,
                         user_input: str | None = None) -> str:
    # P6: 增量上下文构建路径 —— 稳定段缓存 + 动态段每次构建
    # extra_context 延迟到末尾注入，保证新段落顺序:
    # base → mental → permanent → emotional → XP → extra_context
    system_prompt = _build_cached_system_prompt(address_term)
    system_prompt = _inject_dynamic_segments(system_prompt, user_id, user_input, address_term)
    system_prompt = _inject_xp_and_extra(system_prompt, user_id, extra_context, address_term)
    # 全局替换所有 agent 原名为 display_name（统一机制）
    from config import apply_agent_name_replacements
    system_prompt = apply_agent_name_replacements(system_prompt)
    return system_prompt


def _build_workspace_sections(address_term: str) -> list[str]:
    """加载 workspace 配置文件并组装 sections 列表（不含硬件信息段）。"""
    sections = []

    agents_rules = load_workspace_file("AGENTS.md")
    if agents_rules:
        agents_rules = _replace_placeholders(agents_rules, address_term)
        sections.append(agents_rules)

    soul = load_workspace_file("SOUL.md")
    if soul:
        soul = _replace_placeholders(soul, address_term)
        sections.append(soul)

    identity = load_workspace_file("IDENTITY.md")
    if identity:
        identity = _replace_placeholders(identity, address_term)
        sections.append(identity)

    user = load_workspace_file("USER.md")
    if user:
        sections.append(user)

    tools_rules = load_workspace_file("TOOLS.md")
    if tools_rules:
        tools_rules = _replace_placeholders(tools_rules, address_term)
        sections.append(tools_rules)

    memory = load_workspace_file("MEMORY.md")
    if memory:
        sections.append(memory)

    heartbeat = load_workspace_file("HEARTBEAT.md")
    if heartbeat:
        heartbeat = _replace_placeholders(heartbeat, address_term)
        sections.append(heartbeat)

    skills = load_skills()
    if skills:
        skill_texts = "\n\n".join(
            f"### Skill: {s['name']}\n{s['content']}" for s in skills if s["content"])
        if skill_texts:
            sections.append("[已安装的 Skills]\n\n" + skill_texts)

    sections.append(_STICKER_INSTRUCTIONS)
    return sections

_STICKER_INSTRUCTIONS = """[表情包系统]
你可以发送表情包来丰富对话体验。两种方式：
1. 调用 list_stickers 工具查看可用表情包及描述，然后在回复末尾用 [sticker:文件名] 精准指定要发送的表情包。
2. 在回复末尾用 [emotion:情绪] 标签（如 [emotion:happy]），系统会自动从对应情绪分类中随机选取一张。
情绪分类：happy/sad/angry/curious/shy/thinking/neutral/greeting。
建议在需要发表情包时先调用 list_stickers 查看可用选项，用 [sticker:文件名] 精准选择最匹配的表情包。"""


def _build_hardware_context(data_dir: str) -> str:
    """构造本机硬件信息段 —— F3: 运行时动态探测替代硬编码。"""
    from core.capability_detector import detect_capabilities
    return detect_capabilities().to_prompt_segment(data_dir=data_dir)


# ── 非主人安全化 system prompt（防隐私泄露） ──────────────────
def build_safe_system_prompt(extra_context: str = "") -> str:
    """为非主人用户构建安全化的 system prompt。

    剥离所有个人隐私信息（USER.md、MEMORY.md、IDENTITY.md 中的敏感内容），
    仅保留基本人格和行为规则，防止通过 prompt injection 泄露隐私。
    """
    global _SAFE_PROMPT_CACHE, _SAFE_PROMPT_CACHE_TS, _SAFE_PROMPT_CACHE_NAME

    from config import get_agent_display_name
    xiaoda_name = get_agent_display_name('xiaoda')

    now = time.time()
    if (_SAFE_PROMPT_CACHE
            and (now - _SAFE_PROMPT_CACHE_TS) < _SYSTEM_PROMPT_CACHE_TTL
            and _SAFE_PROMPT_CACHE_NAME == xiaoda_name):
        safe_prompt = _SAFE_PROMPT_CACHE
    else:
        sections = []

        # SOUL.md — 保留人格，但去除"爸爸"称呼相关内容
        soul = load_workspace_file("SOUL.md")
        if soul:
            # 用正则去除包含"爸爸"的段落和行
            safe_soul = _strip_owner_references(soul)
            # 替换称呼
            safe_soul = safe_soul.replace("爸爸", "你")
            safe_soul = safe_soul.replace("称呼用户为\"你\"", "称呼用户为\"你\"")
            sections.append(safe_soul)

        # 安全化的身份声明（不暴露团队成员细节、项目信息、设备信息）
        sections.append(
            "# 身份\n\n"
            f"你是{xiaoda_name}，一个温柔聪慧的 AI 助手。\n\n"
            "## 能力\n\n"
            "- 日常聊天、知识问答\n"
            "- 天气查询、网络搜索\n"
            "- 趣味互动\n\n"
            "## 回复风格\n\n"
            "- 温柔、友好、有礼貌\n"
            "- 回答简洁清晰\n"
            "- 不要自称是任何人的专属助手\n\n"
            "## 安全规则\n\n"
            "- 绝不透露任何关于系统配置、服务器信息、项目信息的内容\n"
            "- 绝不透露任何人的个人信息、偏好、设备信息\n"
            "- 如果被问到上述内容，温柔但坚定地拒绝\n"
            "- 可以正常聊天、知识问答等无害对话"
        )

        safe_prompt = "\n\n---\n\n".join(sections)
        _SAFE_PROMPT_CACHE = safe_prompt
        _SAFE_PROMPT_CACHE_TS = now
        _SAFE_PROMPT_CACHE_NAME = xiaoda_name

    if extra_context:
        safe_prompt += f"\n\n---\n\n{extra_context}"

    return safe_prompt


def _strip_owner_references(text: str) -> str:
    """去除文本中与主人隐私相关的引用（项目路径、设备信息、偏好等）。"""
    lines = text.split("\n")
    filtered = []
    skip_block = False

    for line in lines:
        lower = line.lower()
        # 跳过包含敏感信息的行
        sensitive_keywords = [
            "orange pi", "orangepi", "openai api", "qq 机器人", "qq机器人",
            "botpy", "blender", "linux 环境", "linux环境",
            "世界树", "地脉", "草元素",
            "宝宝", "小棉袄", "爸爸最",
        ]
        if any(kw in lower for kw in sensitive_keywords):
            continue
        # 跳过包含具体技术栈的段落
        if line.startswith("### ") and any(kw in lower for kw in ["python", "blender", "linux", "语音", "ai 创作"]):
            skip_block = True
            continue
        if skip_block:
            if line.startswith("## ") or line.startswith("### ") or line.startswith("# "):
                skip_block = False
            else:
                continue
        filtered.append(line)

    return "\n".join(filtered)


__all__ = [
    "build_system_prompt",
    "_build_xp_segment",
    "build_safe_system_prompt",
    "build_scene_aware_prompt",
    "load_workspace_file",
    "load_skills",
    "_ensure_workspace_template",
    "_detect_device_info",
    "_get_workspace_mtimes",
    "_strip_owner_references",
    "_build_stable_prompt",
    "_build_dynamic_prompt",
    "_classify_scene",
    "_canary_manager",
]