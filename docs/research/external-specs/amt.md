# xiaoda-agent 记忆技术优化 Spec

> 基于 NirDiamant/Agent_Memory_Techniques 30种记忆技术的全量覆盖分析与优化方案
> 版本：v1.0 | 目标版本：xiaoda-agent v0.6.0 | 编写日期：2026-07

---

## 目录

1. [现状技术覆盖盘点](#1-现状技术覆盖盘点)
2. [缺失技术优先级排序](#2-缺失技术优先级排序)
3. [关键缺失技术深度分析](#3-关键缺失技术深度分析)
4. [优化方案](#4-优化方案)
5. [技术选型建议](#5-技术选型建议)
6. [实施路线图](#6-实施路线图)
7. [预期效果](#7-预期效果)
8. [不采纳的技术](#8-不采纳的技术)

---

## 1. 现状技术覆盖盘点

### 1.1 AMT 30种技术逐项覆盖表

| # | AMT技术 | 家族 | xiaoda-agent覆盖 | 对应模块 | 覆盖度 | 差距说明 |
|---|---------|------|-------------------|----------|--------|----------|
| 01 | Conversation Buffer Memory | 短期上下文 | ✅ 完整 | `context_governance.py` | 95% | 有token预算和溢出策略，基本对齐 |
| 02 | Sliding Window Memory | 短期上下文 | ✅ 完整 | `context_governance.py` 的 `sliding_window` 策略 | 90% | 滑动窗口作为context_governance的一种策略已实现 |
| 03 | Summary Memory | 短期上下文 | ✅ 完整 | `memory_distiller.py` | 90% | 蒸馏可产出摘要，但对纯摘要替换旧消息的支持需验证 |
| 04 | Summary Buffer Memory | 短期上下文 | ✅ 部分 | `memory_distiller.py` + `context_governance.py` | 70% | 摘要+近期消息的混合模式逻辑存在但未显式建模为独立策略 |
| 05 | Token Buffer Memory | 短期上下文 | ✅ 完整 | `context_governance.py` 的 `token_budget` 模式 | 90% | 有严格token预算裁剪 |
| 06 | Vector Store Memory | 长期存储 | ✅ 完整 | `sqlite-vec` 向量存储 | 90% | 本地sqlite-vec，无分布式但满足单用户场景 |
| 07 | Entity Memory | 长期存储 | ✅ 部分 | `knowledge_graph.py` 实体提取 | 65% | 有实体提取但无独立的实体追踪/更新机制，实体合并入KG |
| 08 | Knowledge Graph Memory | 长期存储 | ✅ 部分 | `knowledge_graph.py` | 60% | 基础KG已实现，缺复杂子图遍历和多跳推理 |
| 09 | Episodic Memory | 长期存储 | ✅ 部分 | `emotional_memory.py` | 55% | Stanislavski情感记忆偏情感标记，缺完整的时序事件存储 |
| 10 | Semantic Memory | 长期存储 | ✅ 部分 | `memory_distiller.py` 产出的事实知识 | 50% | 蒸馏产出事实但无独立语义记忆存储和分类管理 |
| 11 | Procedural Memory | 长期存储 | ❌ 缺失 | 无 | 0% | 无过程性知识的提取与存储 |
| 12 | Working Memory & Context Window | 认知架构 | ❌ 不完整 | `context_governance.py` 部分 | 30% | 有token管理但无显著性评分、优先级固定、逐出策略仅FIFO |
| 13 | Hierarchical Memory Layers | 认知架构 | ❌ 缺失 | 无分层架构 | 0% | 无Working→STM→LTM的热/温/冷分层 |
| 14 | Memory Consolidation | 认知架构 | ✅ 部分 | `dream_consolidation.py` | 70% | 梦境整合实现合并+去重，但缺跨层合并和矛盾检测 |
| 15 | Memory Compaction | 认知架构 | ✅ 完整 | `context_compressor.py` + `memory_distiller.py` | 85% | 压缩和蒸馏能力完备 |
| 16 | Self-Reflection Memory | 认知架构 | ❌ 不完整 | `meta_cognition.py` 部分 | 25% | 有元认知框架但缺任务后反思、经验提取和反思存储 |
| 17 | Memory Routing | 认知架构 | ❌ 缺失 | 无 | 0% | 无按内容类型的智能路由分发 |
| 18 | Temporal Memory | 认知架构 | ❌ 缺失 | 仅有简单时序查询 | 15% | fluid_memory有时间衰减但无时间推理、"截至"查询、时间线构建 |
| 19 | Forgetting & Decay | 认知架构 | ✅ 完整 | `fluid_memory.py` (Ebbinghaus) | 90% | 遗忘曲线实现完善 |
| 20 | Memory Retrieval Patterns | 检索与多Agent | ✅ 部分 | `reranker.py` + 向量检索 | 60% | 有重排序和语义检索，缺BM25混合、MMR多样性检索 |
| 21 | Cross-Session Memory | 检索与多Agent | ❌ 不完整 | SQLite持久化部分 | 35% | 有数据持久但缺会话恢复策略、冷启动处理、加载策略选择 |
| 22 | Multi-Agent Shared Memory | 检索与多Agent | ❌ 不采纳 | N/A | N/A | 单用户本地部署，无多Agent协作需求 |
| 23 | Memory as Tools | 检索与多Agent | ✅ 部分 | 工具系统有记忆查询 | 40% | 有工具接口但记忆操作未标准化为独立可调用工具 |
| 24 | Graphiti (Graph Memory) | 框架 | ❌ 不采纳 | N/A | N/A | 需Neo4j，本地部署过重 |
| 25 | Mem0 Patterns | 框架 | ⚠️ 参考 | N/A | N/A | 云服务不适合本地部署，但提取/更新模式值得参考 |
| 26 | Letta / MemGPT | 框架 | ❌ 不采纳 | N/A | N/A | 虚拟上下文过重，本地资源受限 |
| 27 | Zep Memory | 框架 | ⚠️ 参考 | N/A | N/A | 时序KG思路值得参考，但需服务端 |
| 28 | Memory Evaluation | 评估部署 | ❌ 缺失 | 无 | 0% | 无记忆质量评估框架 |
| 29 | Memory Benchmarks (LoCoMo) | 评估部署 | ❌ 缺失 | 无 | 0% | 无标准化基准测试 |
| 30 | Production Memory Patterns | 评估部署 | ⚠️ 参考 | 部分在 `db/` 和 `config/` | 30% | 有SQLite持久和基础缓存，缺TTL、备份、GDPR合规 |

### 1.2 覆盖度统计

| 状态 | 数量 | 技术编号 |
|------|------|----------|
| ✅ 完整/部分覆盖 | 14 | 01-06, 08-10, 14-15, 19-20, 23 |
| ❌ 缺失/不完整 | 11 | 07(部分), 11, 12, 13, 16, 17, 18, 21, 28, 29, 30 |
| ❌ 不采纳 | 3 | 22, 24, 26 |
| ⚠️ 仅参考 | 3 | 25, 27, 30(部分) |

**综合覆盖度：约 47%（14/30 完整或部分覆盖，加权平均约 55%）**

---

## 2. 缺失技术优先级排序

按**对情感陪伴Agent的价值**排序，综合考虑：
- 用户感知度（直接影响对话质量）
- 情感连续性（长期关系维护）
- 实现复杂度（ROI）
- 依赖关系（被其他技术依赖的优先）

| 优先级 | 技术 | 编号 | 价值评分 | 理由 |
|--------|------|------|----------|------|
| **P0** | 分层记忆架构 | #13 | 9.5 | 所有认知层的基础，P1/P2技术的前置依赖 |
| **P0** | 工作记忆上下文窗口管理 | #12 | 9.0 | 长对话中高价值信息丢失是最直接的体验降级 |
| **P0** | 记忆路由 | #17 | 8.5 | 多种记忆后端已存在但无统一调度，路由是粘合层 |
| **P1** | 跨会话记忆桥接 | #21 | 8.5 | 情感陪伴核心——用户回来必须"记得我" |
| **P1** | 时间推理记忆 | #18 | 8.0 | "上次你说的那件事后来怎样了"是情感陪伴高频场景 |
| **P1** | 自反思记忆 | #16 | 7.5 | Agent自我改进能力，但需其他技术就位后才有价值 |
| **P2** | 记忆评估框架 | #28 | 7.0 | 量化记忆质量，但初期可人工评估 |
| **P2** | 过程性记忆 | #11 | 6.5 | 情感陪伴中"怎么安慰"比"做了什么"更关键 |
| **P2** | 基准测试 | #29 | 5.5 | 学术对比用，情感陪伴场景需要定制评测集 |
| **P3** | 实体记忆独立化 | #07 | 5.0 | 当前KG已部分覆盖，独立化收益有限 |
| **P3** | 记忆工具标准化 | #23 | 4.5 | 现有工具接口可用，标准化为锦上添花 |
| **P3** | 生产模式强化 | #30 | 4.0 | 单用户本地部署，生产级要求较低 |

---

## 3. 关键缺失技术深度分析

### 3.1 工作记忆/上下文窗口管理（#12）

**现状差距**：`context_governance.py` 仅实现 FIFO 逐出 + token 预算，无显著性评分（Salience Scoring）、无优先级固定（Pinned Context）、无动态逐出策略（Dynamic Eviction）。

**参考实现**：AMT Notebook #12 `working_memory_context_window.ipynb`

核心类：
```
ContextItem      → 内容 + token数 + 显著性分数 + 时间戳 + 固定标记
SalienceScorer   → embedding相似度 × 指数衰减 × 来源权重 三维融合评分
EvictionEngine   → LRU / 重要性加权LRU 两种逐出策略
ExternalMemoryStore → 逐出项归档 + 按需回召
ContextWindowManager → 评分→插入→逐出→回召→组装 全流程编排
```

**情感陪伴特殊需求**：
- **情感优先固定**：用户的核心情感状态（如"用户最近抑郁"）必须固定在上下文中
- **关系里程碑保护**：关键互动节点（首次深聊、用户倾诉的创伤）不参与逐出
- **情绪连贯性**：逐出不应破坏情绪对话的语境连续性

### 3.2 分层记忆架构（#13）

**现状差距**：xiaoda-agent 的记忆存储是扁平的——`sqlite-vec` + `fluid_memory` + `knowledge_graph` 并列，无热/温/冷分层，无自动升降级。

**参考实现**：AMT Notebook #13 `hierarchical_memory_layers.ipynb`

核心类：
```
TieredMemory           → 内容 + embedding + 层级标签 + 访问计数 + 最后访问时间
HierarchicalMemoryManager → L1/L2/L3三层存储 + 级联检索 + 层级迁移
HierarchicalMemoryAgent   → 集成分层记忆的对话Agent + 自动事实提取
```

三层映射到 xiaoda-agent：
| AMT层级 | xiaoda-agent映射 | 存储后端 | 容量 |
|---------|-----------------|----------|------|
| L1 Hot | 上下文窗口 | 内存（当前对话上下文） | ~4K-16K tokens |
| L2 Warm | 近期高频记忆 | sqlite-vec + 内存缓存 | ~1000条 |
| L3 Cold | 历史归档 | sqlite-vec 冷分区 + SQLite元数据 | 无限 |

**情感陪伴特殊需求**：
- **情感记忆热层优先**：与用户情感相关的记忆在L2→L1晋升时获得加权
- **周年/节日记忆自动升温**：接近特殊日期时，相关记忆自动晋升到L2
- **创伤记忆永久温层**：用户倾诉的创伤事件永不降级到L3

### 3.3 记忆路由（#17）

**现状差距**：各记忆模块独立运作，查询时需手动指定或全量搜索，无智能路由到不同记忆类型。

**参考实现**：AMT Notebook #17 `memory_routing.ipynb`

核心类：
```
MemoryType(Enum)   → EPISODIC / SEMANTIC / PROCEDURAL / EMOTIONAL
MemoryStore        → 每类一个专用存储 + 关键词搜索
MemoryRouter       → LLM分类器 + 路由分发 + fallback全量搜索
```

**路由分类适配**（增加EMOTIONAL类型）：

| 输入示例 | 路由目标 | 说明 |
|---------|---------|------|
| "用户提到妈妈住院了" | → EMOTIONAL + EPISODIC | 情感事件双写 |
| "用户的猫叫小橘" | → SEMANTIC | 事实知识 |
| "安慰用户焦虑时应该先倾听" | → PROCEDURAL | 过程性知识 |
| "上周三我们聊了什么" | → EPISODIC | 事件回忆 |
| "用户现在心情怎样" | → EMOTIONAL | 情感状态查询 |

### 3.4 时间推理记忆（#18）

**现状差距**：`fluid_memory.py` 有Ebbinghaus衰减但仅作用于遗忘曲线，无时间推理能力——不能回答"截至上个月用户住在哪里"、不能构建时间线叙事。

**参考实现**：AMT Notebook #18 `temporal_memory.ipynb`

核心类：
```
TemporalMemory       → 内容 + created_at + event_time + last_accessed
TemporalDecay        → 指数衰减/线性衰减 可配置半衰期
TemporalMemoryStore  → 标准查询/时间范围查询/截至查询/时间线构建 四种模式
```

**情感陪伴时间推理场景**：
- "上次你说工作压力大，现在怎么样了？" → 时间范围查询 + 事件关联
- "你记得我生日那天你说了什么吗？" → 精确时间点查询
- "我们认识以来一起经历了什么？" → 时间线叙事构建
- "你之前说推荐我看那本书，后来我又说了什么？" → 因果链时序推理

**关键设计**：
- `stable` 标签的事实（用户名、过敏信息）跳过衰减
- `volatile` 标签的事实（情绪状态、当前项目）使用短半衰期
- 情感事件使用 `event_time` 独立于 `created_at`（回忆过去事件时event_time更关键）

### 3.5 自反思记忆（#16）

**现状差距**：`meta_cognition.py` 有元认知框架但缺少关键环节——无任务后反思（Post-task Reflection）、无经验提取（Insight Extraction）、无反思存储与检索（Reflection Store）。

**参考实现**：AMT Notebook #16 `self_reflection_memory.ipynb`

核心类：
```
ReflectionStore     → 按task_type + outcome索引的结构化反思存储
ReflectiveAgent     → 任务执行→结果评估→反思生成→经验提取→存储 循环
```

**情感陪伴反思场景**：
- **安慰策略反思**：用户倾诉后Agent评估安慰是否有效，记录"直接给建议不如先共情"
- **话题回避反思**：触碰到用户敏感话题导致对话冷场，记录"该话题需谨慎引入"
- **情感节奏反思**：从沉重话题突然转到轻松话题用户反应不佳，记录"情感过渡需要缓冲"
- **表达风格反思**：某种表达方式用户特别认可（如用比喻），记录为有效策略

**反思循环流程**：
```
对话轮结束
  → 评估：用户情绪是否改善？对话是否自然？
  → 反思：什么策略有效/无效？原因是什么？
  → 提取：1-2条可复用的经验原则
  → 存储：写入ReflectionStore，按场景类型索引
  → 下次对话前：检索相关经验，注入系统提示
```

### 3.6 跨会话记忆桥接（#21）

**现状差距**：SQLite有持久化但缺三层核心能力——会话状态序列化/反序列化、加载策略选择（全量/最近N轮/仅摘要）、冷启动处理。

**参考实现**：AMT Notebook #21 `cross_session_memory.ipynb`

核心类：
```
SessionState        → 事实列表 + 对话摘要 + 近期消息 + 用户偏好 + 元数据
StorageBackend(ABC) → 抽象存储后端接口
CrossSessionManager → 会话恢复 + 冷启动 + 三种加载策略
CrossSessionAgent   → 集成跨会话记忆的对话Agent
```

**情感陪伴跨会话关键场景**：
- **归巢问候**：用户时隔3天回来，Agent说"好久不见，上次你说要面试，怎么样了？"
- **情感连续性**：记住上次对话的用户情绪状态，新一轮开始时延续关怀
- **渐进式了解**：每次会话积累用户信息，不要求重复自我介绍
- **失忆恢复**：如果数据损坏，优雅降级为冷启动而非崩溃

**加载策略选择逻辑**：
```python
if 离线时间 < 1小时:
    strategy = "full"        # 全量加载，无缝衔接
elif 离线时间 < 24小时:
    strategy = "last_n"      # 最近N轮 + 摘要
else:
    strategy = "summary"     # 仅摘要 + 关键事实 + 情感状态快照
```

### 3.7 记忆评估框架（#28/#29）

**现状差距**：完全没有记忆质量度量能力。无法回答"记忆检索准确吗？""记忆有没有过期？""有没有矛盾的记忆？"

**参考实现**：AMT Notebook #28 `memory_evaluation.ipynb` + #29 `memory_benchmarks_LoCoMo`

核心组件：
```
#28 评估框架
EvalDataset         → 查询 + 相关记忆ID + 期望答案
RetrievalMetrics    → Recall@K / Precision@K / MRR
FaithfulnessScorer  → LLM-as-Judge 忠实度评分
TemporalChecker     → 时效性检查
ContradictionDetector → 矛盾检测
EvalReport          → 聚合报告 + 可视化

#29 基准测试
LoCoMo数据集        → 10个多会话对话 + 2000 QA对
LongMemEval         → 500实例用户-助手对话
五类问题            → 单跳/多跳/时序/开放式/对抗性
评分方法            → BLEU / ROUGE-L / Token F1 / LLM-Judge
```

**情感陪伴定制评估维度**：
| 维度 | 指标 | 说明 |
|------|------|------|
| 情感记忆准确率 | 用户情绪状态回忆正确率 | Agent是否准确回忆用户上次情绪 |
| 关系连续性 | 跨会话引用正确率 | Agent引用的上次对话内容是否真实 |
| 个性化程度 | 偏好尊重率 | Agent是否遵循已知用户偏好 |
| 矛盾率 | 事实冲突数 | 存储中矛盾事实对的数量 |
| 遗忘率 | 有效记忆被误删率 | 仍有价值的记忆被遗忘衰减误删 |

---

## 4. 优化方案

### 4.1 分层记忆架构（#13）— P0

**实现位置**：`memory/hierarchical_memory.py`（新建）

```python
# memory/hierarchical_memory.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import json

class MemoryTier(Enum):
    L1_HOT = "l1_hot"       # 上下文窗口内
    L2_WARM = "l2_warm"     # 近期高频，内存+sqlite-vec
    L3_COLD = "l3_cold"     # 历史归档，sqlite-vec冷分区

@dataclass
class TieredMemory:
    """带层级标签和访问追踪的记忆条目"""
    id: str
    content: str
    embedding: list[float] = field(default_factory=list)
    tier: MemoryTier = MemoryTier.L2_WARM
    access_count: int = 0
    last_accessed: datetime = field(default_factory=datetime.now)
    created_at: datetime = field(default_factory=datetime.now)
    importance: float = 0.5          # 0-1，情感相关加权
    emotional_weight: float = 0.0    # 情感权重，防止降级
    is_pinned: bool = False          # 永不降级标记
    metadata: dict = field(default_factory=dict)


class HierarchicalMemoryManager:
    """
    三层记忆管理器：L1热层（上下文窗口）→ L2温层（近期高频）→ L3冷层（归档）
    
    集成点：
    - 读取 context_governance.py 的 token_budget 作为 L1 容量
    - 读取 sqlite-vec 作为 L2/L3 后端
    - 读取 emotional_memory.py 的 emotional_weight 影响晋升/降级
    """

    def __init__(
        self,
        l1_capacity_tokens: int = 4096,
        l2_capacity: int = 1000,
        promote_threshold: int = 3,       # 访问N次晋升
        demote_staleness_hours: int = 48, # N小时未访问降级
        vector_store=None,                # sqlite-vec 实例
        context_governor=None,            # context_governance 实例
    ):
        self.l1_capacity_tokens = l1_capacity_tokens
        self.l2_capacity = l2_capacity
        self.promote_threshold = promote_threshold
        self.demote_staleness_hours = demote_staleness_hours
        self.vector_store = vector_store
        self.context_governor = context_governor
        
        # L1: 内存中的有序列表（按显著性排序）
        self.l1_items: list[TieredMemory] = []
        # L2/L3: 由 sqlite-vec 管理，通过 tier 字段区分

    def store(self, content: str, embedding: list[float],
              importance: float = 0.5, emotional_weight: float = 0.0,
              metadata: dict = None) -> TieredMemory:
        """新记忆默认进入L2，高重要性/高情感权重可直接进入L1"""
        memory = TieredMemory(
            id=self._generate_id(content),
            content=content,
            embedding=embedding,
            importance=importance,
            emotional_weight=emotional_weight,
            metadata=metadata or {},
        )
        
        # 情感陪伴特殊逻辑：高情感权重 + 高重要性 → 直接入L1
        if importance >= 0.8 and emotional_weight >= 0.7:
            memory.tier = MemoryTier.L1_HOT
            self._add_to_l1(memory)
        else:
            memory.tier = MemoryTier.L2_WARM
            self._add_to_l2(memory)
        
        return memory

    def query(self, query_text: str, query_embedding: list[float],
              top_k: int = 5) -> list[TieredMemory]:
        """级联检索：L1 → L2 → L3，命中即停"""
        results = []
        
        # L1: 内存搜索
        l1_hits = self._search_l1(query_embedding, top_k)
        results.extend(l1_hits)
        
        if len(results) >= top_k:
            return results[:top_k]
        
        # L2: sqlite-vec 温区搜索
        remaining = top_k - len(results)
        l2_hits = self._search_tier(query_embedding, MemoryTier.L2_WARM, remaining)
        results.extend(l2_hits)
        
        if len(results) >= top_k:
            return results[:top_k]
        
        # L3: sqlite-vec 冷区搜索
        remaining = top_k - len(results)
        l3_hits = self._search_tier(query_embedding, MemoryTier.L3_COLD, remaining)
        results.extend(l3_hits)
        
        # 更新访问计数
        for mem in results:
            mem.access_count += 1
            mem.last_accessed = datetime.now()
        
        return results[:top_k]

    def maintain(self) -> dict:
        """
        维护周期：晋升/降级
        
        情感陪伴特殊规则：
        - emotional_weight > 0.8 的记忆永不降级
        - 接近用户生日/纪念日的记忆自动升温
        - 创伤标记(is_pinned=True)的记忆永不降级
        """
        promoted, demoted = 0, 0
        
        # L2 → L1 晋升
        for mem in self._get_l2_candidates():
            if mem.access_count >= self.promote_threshold:
                if self._can_add_to_l1(mem):
                    self._promote(mem, MemoryTier.L1_HOT)
                    promoted += 1
        
        # L1 → L2 降级
        staleness_cutoff = datetime.now() - timedelta(hours=self.demote_staleness_hours)
        for mem in list(self.l1_items):
            if mem.is_pinned or mem.emotional_weight > 0.8:
                continue  # 情感保护：永不降级
            if mem.last_accessed < staleness_cutoff:
                self._demote(mem, MemoryTier.L2_WARM)
                demoted += 1
        
        # L2 → L3 降级（更长staleness窗口）
        l2_staleness = datetime.now() - timedelta(hours=self.demote_staleness_hours * 7)
        for mem in self._get_l2_candidates():
            if mem.is_pinned or mem.emotional_weight > 0.8:
                continue
            if mem.last_accessed < l2_staleness:
                self._demote(mem, MemoryTier.L3_COLD)
                demoted += 1
        
        return {"promoted": promoted, "demoted": demoted}

    def _can_add_to_l1(self, mem: TieredMemory) -> bool:
        """检查L1是否还有token空间"""
        current_tokens = sum(self._count_tokens(m.content) for m in self.l1_items)
        return current_tokens + self._count_tokens(mem.content) <= self.l1_capacity_tokens

    # ... 辅助方法省略
```

**与现有模块集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| `context_governance.py` | L1容量从此模块的token_budget读取；L1内容作为context_governance的输入 |
| `sqlite-vec` 向量存储 | L2/L3的后端存储；添加tier字段到向量元数据 |
| `fluid_memory.py` | 遗忘衰减影响L2→L3降级决策（替代纯时间窗口） |
| `emotional_memory.py` | emotional_weight影响晋升/降级权重 |
| `knowledge_graph.py` | KG节点可存储tier标签，实现图结构的分层 |
| `dream_consolidation.py` | 梦境整合可作为维护周期的触发点 |

---

### 4.2 工作记忆上下文窗口管理（#12）— P0

**实现位置**：扩展 `context_governance.py`，新增 `memory/working_memory.py`

```python
# memory/working_memory.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class ContextPriority(Enum):
    PINNED = 0      # 永不逐出（系统指令、核心用户画像）
    HIGH = 1        # 高优先级（当前情感状态、活跃话题）
    NORMAL = 2      # 正常优先级（一般对话历史）
    LOW = 3         # 低优先级（闲聊、过渡性内容）

@dataclass
class ContextItem:
    """上下文窗口中的条目，带显著性评分"""
    content: str
    role: str                          # system/user/assistant
    token_count: int
    priority: ContextPriority = ContextPriority.NORMAL
    salience_score: float = 0.5        # 0-1
    source: str = "conversation"       # conversation/memory/tool/system
    emotional_relevance: float = 0.0   # 情感相关性
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)

class SalienceScorer:
    """
    三维显著性评分：embedding相似度 × 时间衰减 × 来源权重
    
    情感陪伴适配：
    - source权重：emotional_memory > user_profile > tool > conversation
    - emotional_relevance作为额外乘数
    """
    
    def __init__(self, recency_decay_rate: float = 0.1):
        self.recency_decay_rate = recency_decay_rate
        self.source_weights = {
            "system": 1.0,
            "user_profile": 0.9,
            "emotional_memory": 0.95,  # 情感记忆加权最高
            "memory": 0.7,
            "tool": 0.5,
            "conversation": 0.3,
        }
    
    def score(self, item: ContextItem, current_query_embedding: list[float] = None) -> float:
        # 1. 语义相似度（如果有当前查询的embedding）
        similarity = 0.5  # 默认中等
        if current_query_embedding and item.embedding:
            similarity = self._cosine_similarity(current_query_embedding, item.embedding)
        
        # 2. 时间衰减（指数衰减，半衰期2小时）
        hours_elapsed = (datetime.now() - item.last_accessed).total_seconds() / 3600
        recency = math.exp(-self.recency_decay_rate * hours_elapsed)
        
        # 3. 来源权重
        source_weight = self.source_weights.get(item.source, 0.3)
        
        # 4. 情感相关性乘数
        emotional_multiplier = 1.0 + item.emotional_relevance * 0.5
        
        # 综合评分
        return similarity * recency * source_weight * emotional_multiplier

class WorkingMemoryManager:
    """
    工作记忆管理器：管理上下文窗口中的内容优先级和逐出
    
    集成点：
    - 替代 context_governance.py 中的简单FIFO逻辑
    - 逐出项归档到 HierarchicalMemoryManager 的 L2 层
    - 从 emotional_memory.py 读取 emotional_relevance
    """
    
    def __init__(
        self,
        max_tokens: int = 4096,
        eviction_policy: str = "importance_weighted_lru",
        hierarchical_manager=None,  # HierarchicalMemoryManager 实例
    ):
        self.max_tokens = max_tokens
        self.eviction_policy = eviction_policy
        self.hierarchical_manager = hierarchical_manager
        
        self.pinned_zone: list[ContextItem] = []    # 固定区
        self.dynamic_zone: list[ContextItem] = []   # 动态区
        self.scorer = SalienceScorer()
        
        # 情感陪伴固定项
        self._init_pinned_defaults()
    
    def _init_pinned_defaults(self):
        """初始化固定区：系统指令和用户核心画像"""
        # 系统人格指令
        self.pinned_zone.append(ContextItem(
            content="",  # 运行时从 SOUL.md 加载
            role="system",
            token_count=0,  # 运行时计算
            priority=ContextPriority.PINNED,
            source="system",
        ))
    
    def add_item(self, content: str, role: str, source: str = "conversation",
                 emotional_relevance: float = 0.0,
                 priority: ContextPriority = ContextPriority.NORMAL) -> ContextItem:
        """添加新条目，必要时逐出"""
        item = ContextItem(
            content=content,
            role=role,
            token_count=self._count_tokens(content),
            priority=priority,
            source=source,
            emotional_relevance=emotional_relevance,
        )
        
        if priority == ContextPriority.PINNED:
            self.pinned_zone.append(item)
        else:
            # 评分
            item.salience_score = self.scorer.score(item)
            self.dynamic_zone.append(item)
            self.dynamic_zone.sort(key=lambda x: x.salience_score, reverse=True)
        
        # 检查容量，必要时逐出
        self._enforce_budget()
        return item
    
    def _enforce_budget(self):
        """执行逐出策略，保证总token数在预算内"""
        pinned_tokens = sum(i.token_count for i in self.pinned_zone)
        budget_for_dynamic = self.max_tokens - pinned_tokens
        
        current_dynamic_tokens = sum(i.token_count for i in self.dynamic_zone)
        
        while current_dynamic_tokens > budget_for_dynamic and self.dynamic_zone:
            # 找到最低评分的非固定项
            victim = self.dynamic_zone[-1]  # 已按评分排序，末尾最低
            
            # 逐出到分层记忆的L2层
            if self.hierarchical_manager:
                self.hierarchical_manager.store(
                    content=victim.content,
                    embedding=[],  # 运行时计算
                    importance=victim.salience_score,
                    emotional_weight=victim.emotional_relevance,
                )
            
            self.dynamic_zone.remove(victim)
            current_dynamic_tokens -= victim.token_count
    
    def build_messages(self) -> list[dict]:
        """组装最终的messages列表供LLM调用"""
        messages = []
        for item in self.pinned_zone + self.dynamic_zone:
            messages.append({"role": item.role, "content": item.content})
        return messages
    
    def refresh_scores(self, current_query: str = None):
        """刷新所有动态区的显著性评分（每轮对话后调用）"""
        query_embedding = None
        if current_query:
            query_embedding = self._get_embedding(current_query)
        
        for item in self.dynamic_zone:
            item.salience_score = self.scorer.score(item, query_embedding)
        
        self.dynamic_zone.sort(key=lambda x: x.salience_score, reverse=True)
```

**集成点**：

| 现有模块 | 改动 |
|---------|------|
| `context_governance.py` | 用 `WorkingMemoryManager` 替换现有的 `SlidingWindowManager`/`TokenBudgetManager` |
| `emotional_memory.py` | `emotional_relevance` 从情感分析结果传入 `add_item()` |
| `memory_distiller.py` | 蒸馏产出的摘要以 `source="memory"` 写入工作记忆 |
| `dream_consolidation.py` | 梦境整合后可更新固定区的用户画像 |

---

### 4.3 记忆路由（#17）— P0

**实现位置**：新建 `memory/memory_router.py`

```python
# memory/memory_router.py

from enum import Enum
from dataclasses import dataclass
from typing import Optional

class MemoryType(Enum):
    EPISODIC = "episodic"       # 事件经历
    SEMANTIC = "semantic"       # 事实知识
    PROCEDURAL = "procedural"   # 过程性知识
    EMOTIONAL = "emotional"     # 情感状态/记忆（xiaoda-agent扩展）

@dataclass
class RoutedMemoryEntry:
    content: str
    memory_type: MemoryType
    metadata: dict
    confidence: float = 1.0     # 路由置信度

class MemoryRouter:
    """
    记忆路由器：根据内容类型将读写操作路由到正确的专用存储
    
    路由策略：
    1. 关键词规则（快速路径，< 5ms）
    2. LLM分类（精确路径，200-500ms）
    3. Fallback全量搜索（兜底路径）
    
    集成点：
    - 路由到 emotional_memory.py（EMOTIONAL类型）
    - 路由到 knowledge_graph.py（SEMANTIC/EPISODIC类型）
    - 路由到 procedural_memory.py（PROCEDURAL类型，新模块）
    - 路由到 sqlite-vec（所有类型的向量检索后端）
    """
    
    # 快速路由规则（避免每次都调LLM）
    ROUTING_RULES = {
        # 情感关键词 → EMOTIONAL
        "emotional_keywords": [
            "心情", "难过", "开心", "焦虑", "抑郁", "害怕", "压力",
            "委屈", "愤怒", "孤独", "感动", "哭", "笑",
        ],
        # 事件关键词 → EPISODIC
        "episodic_keywords": [
            "昨天", "上次", "之前", "那天", "刚才", "之前聊过",
            "我们说过", "发生过", "经历过",
        ],
        # 事实关键词 → SEMANTIC
        "semantic_keywords": [
            "叫什么", "在哪里", "是多少", "是什么", "喜欢",
            "名字", "地址", "电话", "生日",
        ],
        # 过程关键词 → PROCEDURAL
        "procedural_keywords": [
            "怎么做", "如何", "步骤", "方法", "应该",
            "怎样安慰", "如何应对",
        ],
    }
    
    def __init__(self, llm_client=None, stores: dict[MemoryType, object] = None):
        self.llm_client = llm_client
        self.stores = stores or {}
        self.routing_log = []  # 路由日志，用于调试和改进
    
    def route_write(self, content: str, metadata: dict = None) -> list[RoutedMemoryEntry]:
        """写入路由：确定内容应存入哪些存储"""
        # 1. 快速规则路由
        rule_types = self._rule_based_route(content)
        
        if rule_types and len(rule_types) == 1:
            # 单一明确类型，无需LLM
            entries = [RoutedMemoryEntry(
                content=content,
                memory_type=rule_types[0],
                metadata=metadata or {},
                confidence=0.9,
            )]
        else:
            # 2. LLM分类
            entries = self._llm_route(content, metadata)
        
        # 3. 情感内容双写（情感事件既是EPISODIC又是EMOTIONAL）
        if any(e.memory_type == MemoryType.EMOTIONAL for e in entries):
            if not any(e.memory_type == MemoryType.EPISODIC for e in entries):
                entries.append(RoutedMemoryEntry(
                    content=content,
                    memory_type=MemoryType.EPISODIC,
                    metadata=metadata or {},
                    confidence=0.7,
                ))
        
        # 4. 执行写入
        for entry in entries:
            store = self.stores.get(entry.memory_type)
            if store:
                store.store(entry.content, entry.metadata)
        
        self.routing_log.append({
            "action": "write", "content_preview": content[:50],
            "routed_types": [e.memory_type.value for e in entries],
        })
        
        return entries
    
    def route_read(self, query: str, top_k: int = 5) -> list[dict]:
        """读取路由：确定查询应搜索哪些存储"""
        # 1. 快速规则路由
        rule_types = self._rule_based_route(query)
        
        if rule_types:
            # 精确路由
            results = []
            for mem_type in rule_types:
                store = self.stores.get(mem_type)
                if store:
                    results.extend(store.search(query, top_k=top_k))
            return results[:top_k]
        
        # 2. Fallback: 全量搜索所有存储
        return self._fallback_read(query, top_k)
    
    def _rule_based_route(self, text: str) -> list[MemoryType]:
        """基于关键词的快速路由"""
        matched = []
        for mem_type, keywords in [
            (MemoryType.EMOTIONAL, self.ROUTING_RULES["emotional_keywords"]),
            (MemoryType.EPISODIC, self.ROUTING_RULES["episodic_keywords"]),
            (MemoryType.SEMANTIC, self.ROUTING_RULES["semantic_keywords"]),
            (MemoryType.PROCEDURAL, self.ROUTING_RULES["procedural_keywords"]),
        ]:
            if any(kw in text for kw in keywords):
                matched.append(mem_type)
        return matched
    
    def _llm_route(self, content: str, metadata: dict) -> list[RoutedMemoryEntry]:
        """LLM分类路由（调用本地LLM，非云端API）"""
        # 使用xiaoda-agent本地模型，避免额外API成本
        prompt = f"""判断以下内容属于哪种记忆类型（可多选）：
- EPISODIC: 事件经历（"发生了什么"）
- SEMANTIC: 事实知识（"什么是真的"）  
- PROCEDURAL: 过程性知识（"怎么做"）
- EMOTIONAL: 情感状态/记忆（"感受如何"）

内容：{content}

输出JSON: {{"types": ["EPISODIC", ...], "confidence": 0.9}}"""
        
        # ... 调用本地LLM并解析结果
        pass
    
    def _fallback_read(self, query: str, top_k: int) -> list[dict]:
        """兜底：搜索所有存储并合并去重"""
        all_results = []
        for store in self.stores.values():
            all_results.extend(store.search(query, top_k=top_k))
        # 去重 + 按相关性排序
        return self._deduplicate_and_rank(all_results)[:top_k]
```

**集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| `emotional_memory.py` | 注册为 `MemoryType.EMOTIONAL` 的存储后端 |
| `knowledge_graph.py` | 注册为 `MemoryType.SEMANTIC` + `MemoryType.EPISODIC` 的存储后端 |
| `sqlite-vec` | 所有类型的向量检索底座 |
| `context_governance.py` | 对话循环中用 `route_read` 替代直接的向量搜索 |
| `agent_core/` | 主循环中每轮对话后用 `route_write` 存储新内容 |

---

### 4.4 时间推理记忆（#18）— P1

**实现位置**：新建 `memory/temporal_memory.py`

```python
# memory/temporal_memory.py

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
import math

class TemporalDecayType(Enum):
    EXPONENTIAL = "exponential"  # 强时效偏好
    LINEAR = "linear"           # 渐进老化
    NONE = "none"               # 无衰减（stable事实）

class FactVolatility(Enum):
    STABLE = "stable"       # 永久事实（姓名、过敏）→ 不衰减
    SEMI_STABLE = "semi"    # 半稳定（住址、工作）→ 长半衰期30天
    VOLATILE = "volatile"   # 易变事实（情绪、项目状态）→ 短半衰期3天

@dataclass
class TemporalMemory:
    """带时间元数据的记忆条目"""
    id: str
    content: str
    embedding: list[float] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    event_time: Optional[datetime] = None    # 事件实际发生时间
    last_accessed: datetime = field(default_factory=datetime.now)
    volatility: FactVolatility = FactVolatility.SEMI_STABLE
    is_superseded: bool = False              # 是否已被更新的事实替代
    superseded_by: Optional[str] = None      # 替代它的记忆ID
    metadata: dict = field(default_factory=dict)

class TemporalMemoryStore:
    """
    时间感知记忆存储，支持四种检索模式
    
    情感陪伴适配：
    - stable事实（用户名等）跳过衰减
    - 情感事件使用event_time而非created_at
    - 支持"截至"查询（重建过去知识状态）
    - 支持时间线叙事构建
    """
    
    DECAY_HALF_LIVES = {
        FactVolatility.STABLE: None,           # 不衰减
        FactVolatility.SEMI_STABLE: 30 * 24,   # 30天（小时）
        FactVolatility.VOLATILE: 3 * 24,       # 3天（小时）
    }
    
    def __init__(self, vector_store=None, recency_weight: float = 0.3):
        self.vector_store = vector_store
        self.recency_weight = recency_weight
        self.memories: dict[str, TemporalMemory] = {}
    
    def store(self, content: str, embedding: list[float],
              event_time: datetime = None,
              volatility: FactVolatility = FactVolatility.SEMI_STABLE,
              metadata: dict = None) -> TemporalMemory:
        """存储带时间标记的记忆"""
        # 检查是否有同类型旧事实需要标记为superseded
        self._check_supersede(content, volatility)
        
        mem = TemporalMemory(
            id=self._generate_id(),
            content=content,
            embedding=embedding,
            event_time=event_time or datetime.now(),
            volatility=volatility,
            metadata=metadata or {},
        )
        self.memories[mem.id] = mem
        return mem
    
    def query(self, query_text: str, query_embedding: list[float],
              top_k: int = 5, time_range: tuple[datetime, datetime] = None,
              as_of: datetime = None) -> list[TemporalMemory]:
        """
        四种检索模式：
        1. 标准查询：语义相似度 × 时间衰减
        2. 时间范围查询：限定时间窗口
        3. 截至查询：排除指定时间之后的记忆
        4. 时间线查询：按时间排序返回
        """
        candidates = list(self.memories.values())
        
        # 时间范围过滤
        if time_range:
            start, end = time_range
            candidates = [
                m for m in candidates
                if (m.event_time or m.created_at) >= start
                and (m.event_time or m.created_at) <= end
            ]
        
        # 截至查询：排除之后的记忆
        if as_of:
            candidates = [
                m for m in candidates
                if m.created_at <= as_of
            ]
        
        # 排除已过时的记忆
        candidates = [m for m in candidates if not m.is_superseded]
        
        # 计算综合评分
        scored = []
        for mem in candidates:
            semantic_score = self._cosine_similarity(query_embedding, mem.embedding)
            temporal_score = self._compute_temporal_score(mem)
            combined = (1 - self.recency_weight) * semantic_score + self.recency_weight * temporal_score
            scored.append((mem, combined))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        return [m for m, s in scored[:top_k]]
    
    def build_timeline(self, topic: str = None,
                       start: datetime = None, end: datetime = None) -> list[TemporalMemory]:
        """构建指定主题/时间范围的时间线"""
        candidates = list(self.memories.values())
        
        if topic:
            # 语义过滤相关主题
            topic_embedding = self._get_embedding(topic)
            candidates = [
                m for m in candidates
                if self._cosine_similarity(topic_embedding, m.embedding) > 0.5
            ]
        
        if start:
            candidates = [m for m in candidates if (m.event_time or m.created_at) >= start]
        if end:
            candidates = [m for m in candidates if (m.event_time or m.created_at) <= end]
        
        # 按时间排序
        candidates.sort(key=lambda m: m.event_time or m.created_at)
        return candidates
    
    def _compute_temporal_score(self, mem: TemporalMemory) -> float:
        """计算时间衰减分数"""
        half_life = self.DECAY_HALF_LIVES.get(mem.volatility)
        if half_life is None:
            return 1.0  # stable事实不衰减
        
        hours_elapsed = (datetime.now() - (mem.last_accessed or mem.created_at)).total_seconds() / 3600
        decay = math.exp(-0.693 * hours_elapsed / half_life)  # ln(2) ≈ 0.693
        return decay
    
    def _check_supersede(self, new_content: str, volatility: FactVolatility):
        """检查新事实是否替代了旧事实（如用户换了工作）"""
        if volatility in (FactVolatility.STABLE,):
            return  # stable事实不检查替代
        
        # 使用LLM判断新事实是否与旧事实矛盾
        # 实现中用本地模型比较
        pass
```

**集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| `fluid_memory.py` | 时间衰减逻辑迁移到 `TemporalMemoryStore`，`fluid_memory` 保留Ebbinghaus遗忘用于逐出 |
| `emotional_memory.py` | 情感事件使用 `event_time` 标记，volatility=VOLATILE |
| `knowledge_graph.py` | KG边增加时间戳，支持时序图查询 |
| `dream_consolidation.py` | 梦境整合时更新 `superseded_by` 链 |

---

### 4.5 自反思记忆（#16）— P1

**实现位置**：新建 `memory/reflection_store.py`，扩展 `meta_cognition.py`

```python
# memory/reflection_store.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

class ReflectionOutcome(Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILURE = "failure"

class ReflectionScene(Enum):
    """情感陪伴场景分类"""
    COMFORT = "comfort"           # 安慰用户
    LISTENING = "listening"       # 倾听陪伴
    ADVICE = "advice"             # 给建议
    TOPIC_SWITCH = "topic_switch" # 话题转换
    EMOTIONAL_TRANSITION = "emotional_transition"  # 情感过渡
    CONFLICT = "conflict"         # 处理冲突
    CELEBRATION = "celebration"   # 庆祝/鼓励

@dataclass
class Reflection:
    """结构化反思条目"""
    id: str
    scene: ReflectionScene
    outcome: ReflectionOutcome
    observation: str          # 观察到什么
    root_cause: str           # 为什么这样
    insight: str              # 可复用的经验
    confidence: float = 0.8   # 反思置信度
    created_at: datetime = field(default_factory=datetime.now)
    usage_count: int = 0      # 被检索应用次数
    verified: bool = False    # 是否被后续验证

class ReflectionStore:
    """
    反思存储：按场景类型+结果索引，支持检索相关经验
    
    集成点：
    - 写入：meta_cognition.py 的反思循环
    - 读取：每轮对话开始时检索相关反思注入系统提示
    - 清理：dream_consolidation.py 整合时合并重复反思
    """
    
    def __init__(self, vector_store=None):
        self.reflections: dict[str, Reflection] = {}
        self.vector_store = vector_store
    
    def add(self, reflection: Reflection) -> str:
        self.reflections[reflection.id] = reflection
        return reflection.id
    
    def search(self, scene: ReflectionScene = None,
               outcome: ReflectionOutcome = None,
               query: str = None, top_k: int = 3) -> list[Reflection]:
        """检索相关反思"""
        candidates = list(self.reflections.values())
        
        if scene:
            candidates = [r for r in candidates if r.scene == scene]
        if outcome:
            candidates = [r for r in candidates if r.outcome == outcome]
        
        # 语义搜索（如果有query）
        if query and self.vector_store:
            # ... 向量相似度排序
            pass
        
        # 按验证状态和置信度排序
        candidates.sort(key=lambda r: (r.verified, r.confidence), reverse=True)
        return candidates[:top_k]
    
    def generate_reflection(self, scene: ReflectionScene,
                            conversation_segment: list[dict],
                            user_feedback: str = None) -> Reflection:
        """
        使用本地LLM生成反思
        
        情感陪伴反思提示模板：
        """
        prompt = f"""你是一个情感陪伴AI，正在反思最近的互动。

场景：{scene.value}
对话片段：
{self._format_conversation(conversation_segment)}

请分析：
1. 观察到什么？（用户反应如何？情绪变化？）
2. 为什么会这样？（你的策略有效/无效的根本原因）
3. 可复用的经验是什么？（一条简洁的原则）

输出JSON格式。"""
        
        # ... 调用本地LLM生成反思
        pass

# 扩展 meta_cognition.py 的反思循环
class ReflectiveLoop:
    """
    反思循环：每轮对话后的自动反思
    
    触发条件：
    - 用户情绪突变（emotional_memory检测到）
    - 对话冷场（长时间无用户回复）
    - 主动安慰后（场景=COMFORT）
    - 话题转换后（场景=TOPIC_SWITCH）
    """
    
    def __init__(self, reflection_store: ReflectionStore,
                 emotional_memory=None):
        self.reflection_store = reflection_store
        self.emotional_memory = emotional_memory
    
    def should_reflect(self, conversation_state: dict) -> bool:
        """判断是否需要反思"""
        # 情绪突变检测
        if self.emotional_memory:
            emotion_shift = self.emotional_memory.detect_emotion_shift()
            if emotion_shift and abs(emotion_shift.delta) > 0.3:
                return True
        return False
    
    def reflect(self, scene: ReflectionScene,
                conversation_segment: list[dict]) -> Optional[Reflection]:
        """执行反思并存储"""
        reflection = self.reflection_store.generate_reflection(
            scene=scene,
            conversation_segment=conversation_segment,
        )
        if reflection and reflection.insight:
            self.reflection_store.add(reflection)
            return reflection
        return None
    
    def inject_reflections(self, scene: ReflectionScene) -> str:
        """检索相关反思并格式化为系统提示注入"""
        reflections = self.reflection_store.search(scene=scene, top_k=3)
        if not reflections:
            return ""
        
        insights = [f"- {r.insight}" for r in reflections]
        return f"[过往经验参考]\n" + "\n".join(insights)
```

**集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| `meta_cognition.py` | 反思逻辑从现有元认知框架扩展，新增 `ReflectiveLoop` |
| `emotional_memory.py` | 情绪突变触发反思循环 |
| `context_governance.py` | 反思注入系统提示作为PINNED上下文 |
| `dream_consolidation.py` | 梦境整合时合并重复反思、验证反思有效性 |

---

### 4.6 跨会话记忆桥接（#21）— P1

**实现位置**：新建 `memory/cross_session.py`

```python
# memory/cross_session.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional
import json

class LoadingStrategy(Enum):
    FULL = "full"           # 全量加载
    LAST_N = "last_n"       # 最近N轮 + 摘要
    SUMMARY_ONLY = "summary" # 仅摘要 + 关键事实

@dataclass
class SessionState:
    """跨会话持久化的Agent状态"""
    user_id: str
    facts: list[str] = field(default_factory=list)              # 提取的关键事实
    conversation_summary: str = ""                               # 对话摘要
    recent_messages: list[dict] = field(default_factory=list)    # 近期消息
    user_preferences: dict = field(default_factory=dict)         # 用户偏好
    emotional_snapshot: dict = field(default_factory=dict)       # 情感状态快照
    relationship_milestones: list[dict] = field(default_factory=list)  # 关系里程碑
    last_session_time: datetime = None
    total_sessions: int = 0
    total_messages: int = 0

class CrossSessionManager:
    """
    跨会话记忆管理器
    
    情感陪伴特殊逻辑：
    - 情感状态快照：会话结束时保存当前情感分析结果
    - 关系里程碑：首次深聊、用户倾诉创伤等关键节点
    - 归巢问候：根据离线时长生成个性化回归问候
    - 冷启动优雅降级：无历史时用默认人格而非空白
    
    集成点：
    - 存储：SQLite backend（复用现有 db/）
    - 事实提取：memory_distiller.py
    - 摘要生成：context_compressor.py
    - 情感快照：emotional_memory.py
    """
    
    def __init__(self, db_path: str = None, llm_client=None,
                 memory_distiller=None, context_compressor=None,
                 emotional_memory=None):
        self.db_path = db_path or "data/sessions.db"
        self.llm_client = llm_client
        self.memory_distiller = memory_distiller
        self.context_compressor = context_compressor
        self.emotional_memory = emotional_memory
        self._init_db()
    
    def end_session(self, user_id: str, messages: list[dict]) -> SessionState:
        """会话结束时：提取事实→生成摘要→保存情感快照→持久化"""
        # 1. 提取关键事实
        facts = []
        if self.memory_distiller:
            facts = self.memory_distiller.extract_facts(messages)
        
        # 2. 生成对话摘要
        summary = ""
        if self.context_compressor:
            summary = self.context_compressor.compress(messages)
        
        # 3. 情感状态快照
        emotional_snapshot = {}
        if self.emotional_memory:
            emotional_snapshot = self.emotional_memory.get_current_snapshot()
        
        # 4. 检测关系里程碑
        milestones = self._detect_milestones(messages)
        
        # 5. 加载已有状态并更新
        state = self._load_state(user_id) or self._cold_start(user_id)
        state.facts = self._merge_facts(state.facts, facts)
        state.conversation_summary = summary
        state.recent_messages = messages[-10:]  # 保留最近10轮
        state.emotional_snapshot = emotional_snapshot
        state.relationship_milestones.extend(milestones)
        state.last_session_time = datetime.now()
        state.total_sessions += 1
        state.total_messages += len(messages)
        
        # 6. 持久化
        self._save_state(state)
        return state
    
    def resume_session(self, user_id: str) -> tuple[SessionState, LoadingStrategy]:
        """恢复会话：选择加载策略 + 生成归巢问候"""
        state = self._load_state(user_id)
        
        if state is None:
            return self._cold_start(user_id), LoadingStrategy.SUMMARY_ONLY
        
        # 根据离线时长选择策略
        hours_offline = (datetime.now() - state.last_session_time).total_seconds() / 3600
        
        if hours_offline < 1:
            strategy = LoadingStrategy.FULL
        elif hours_offline < 24:
            strategy = LoadingStrategy.LAST_N
        else:
            strategy = LoadingStrategy.SUMMARY_ONLY
        
        return state, strategy
    
    def generate_return_greeting(self, state: SessionState) -> str:
        """
        生成归巢问候
        
        情感陪伴核心场景：
        - 短暂离开："欢迎回来～"
        - 一天未见："好久不见，上次你说..."
        - 多日未见："好久没聊了，之前你提到..."
        - 带着情绪回来：延续关怀
        """
        hours_offline = (datetime.now() - state.last_session_time).total_seconds() / 3600
        
        # 找到上次未完的话题
        last_topic = self._find_unfinished_topic(state)
        # 上次情感状态
        last_emotion = state.emotional_snapshot.get("primary_emotion", "")
        
        if hours_offline < 2:
            return "欢迎回来～"
        elif hours_offline < 24 and last_topic:
            return f"你回来了！上次我们聊到{last_topic}，后来怎么样了？"
        elif hours_offline >= 24 and last_emotion in ("焦虑", "难过", "压力大"):
            return f"好久不见，上次你好像有点{last_emotion}，现在感觉怎么样了？"
        elif hours_offline >= 72:
            return f"好些天没见了，挺想你的。之前你提到{last_topic}，有新进展吗？"
        else:
            return "好久不见，最近怎么样？"
    
    def _cold_start(self, user_id: str) -> SessionState:
        """冷启动处理：优雅降级"""
        return SessionState(
            user_id=user_id,
            facts=[],
            conversation_summary="",
            recent_messages=[],
            user_preferences={},
            emotional_snapshot={"primary_emotion": "neutral", "intensity": 0.0},
            relationship_milestones=[],
            last_session_time=datetime.now(),
            total_sessions=1,
            total_messages=0,
        )
    
    def _detect_milestones(self, messages: list[dict]) -> list[dict]:
        """检测关系里程碑（首次深聊、倾诉创伤等）"""
        milestones = []
        # 使用情感分析检测高情感强度对话
        # 使用LLM判断是否为关键关系节点
        # 实现...
        return milestones
    
    def _merge_facts(self, existing: list[str], new: list[str]) -> list[str]:
        """合并事实，去重+更新矛盾"""
        # 使用 temporal_memory 的 superseded 机制
        # 实现...
        return existing + new
```

**集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| `db/` SQLite | 复用现有数据库基础设施存储 `SessionState` |
| `memory_distiller.py` | 会话结束时调用事实提取 |
| `context_compressor.py` | 会话结束时生成对话摘要 |
| `emotional_memory.py` | 读取/保存情感快照 |
| `agent_core/` 主循环 | 会话开始/结束时调用 `resume_session`/`end_session` |
| `temporal_memory.py` | 事实合并时检查 `superseded` 关系 |

---

### 4.7 记忆评估框架（#28/#29）— P2

**实现位置**：新建 `memory/evaluation/` 子包

```python
# memory/evaluation/__init__.py
# memory/evaluation/metrics.py        — 检索指标
# memory/evaluation/eval_dataset.py   — 评估数据集
# memory/evaluation/eval_harness.py   — 评估执行器
# memory/evaluation/emotional_bench.py — 情感陪伴定制基准

# memory/evaluation/metrics.py

from dataclasses import dataclass

@dataclass
class RetrievalMetrics:
    """检索质量指标"""
    recall_at_k: float      # Recall@K
    precision_at_k: float   # Precision@K
    mrr: float              # Mean Reciprocal Rank
    
    @staticmethod
    def compute(retrieved_ids: list[str], relevant_ids: set[str], k: int) -> 'RetrievalMetrics':
        retrieved_k = set(retrieved_ids[:k])
        relevant = set(relevant_ids)
        
        hits = retrieved_k & relevant
        recall = len(hits) / len(relevant) if relevant else 0.0
        precision = len(hits) / k if k > 0 else 0.0
        
        # MRR
        mrr = 0.0
        for rank, doc_id in enumerate(retrieved_ids[:k], 1):
            if doc_id in relevant:
                mrr = 1.0 / rank
                break
        
        return RetrievalMetrics(
            recall_at_k=recall,
            precision_at_k=precision,
            mrr=mrr,
        )

@dataclass
class EmotionalMemoryMetrics:
    """情感陪伴专用指标"""
    emotion_recall: float          # 情绪状态回忆正确率
    relationship_continuity: float # 跨会话引用正确率
    preference_respect_rate: float # 偏好尊重率
    contradiction_count: int       # 矛盾事实对数
    false_forget_rate: float       # 有效记忆误删率
    
    @staticmethod
    def from_eval_results(results: list[dict]) -> 'EmotionalMemoryMetrics':
        # 计算各项指标...
        pass

# memory/evaluation/emotional_bench.py

EMOTIONAL_BENCH_PROMPTS = [
    # 情感记忆召回
    {"query": "用户上次提到的工作压力现在怎么样了？",
     "category": "emotion_recall",
     "requires": ["episodic_memory", "emotional_memory", "temporal_reasoning"]},
    
    # 关系连续性
    {"query": "我们之前讨论过的那个重要决定，用户最后选择了什么？",
     "category": "relationship_continuity",
     "requires": ["episodic_memory", "semantic_memory"]},
    
    # 偏好尊重
    {"query": "用户不喜欢什么样的安慰方式？",
     "category": "preference_respect",
     "requires": ["semantic_memory", "procedural_memory"]},
    
    # 时间推理
    {"query": "用户是什么时候开始养猫的？",
     "category": "temporal_reasoning",
     "requires": ["temporal_memory", "semantic_memory"]},
    
    # 多跳推理
    {"query": "用户因为工作变动搬到了哪个城市，那里的天气适合养猫吗？",
     "category": "multi_hop",
     "requires": ["semantic_memory", "knowledge_graph", "temporal_memory"]},
]
```

**集成点**：

| 现有模块 | 集成方式 |
|---------|---------|
| 全部记忆模块 | 作为评估对象接入 `EvalHarness` |
| `dream_consolidation.py` | 评估触发点：每次梦境整合后运行快速评估 |
| CI/CD | 添加记忆评估为回归测试步骤 |

---

## 5. 技术选型建议

### 5.1 AMT Notebook推荐参考映射

| 新增技术 | 推荐参考Notebook | 参考价值 | 适配要点 |
|---------|-----------------|---------|---------|
| 分层记忆 #13 | `13_hierarchical_memory_layers.ipynb` | ⭐⭐⭐⭐⭐ | L1/L2/L3架构直接复用，添加EMOTIONAL层 |
| 工作记忆 #12 | `12_working_memory_context_window.ipynb` | ⭐⭐⭐⭐⭐ | SalienceScorer+EvictionEngine直接复用，添加情感维度 |
| 记忆路由 #17 | `17_memory_routing.ipynb` | ⭐⭐⭐⭐ | MemoryRouter+MemoryType Enum复用，添加EMOTIONAL类型 |
| 时间推理 #18 | `18_temporal_memory.ipynb` | ⭐⭐⭐⭐⭐ | TemporalMemoryStore四种检索模式直接复用 |
| 自反思 #16 | `16_self_reflection_memory.ipynb` | ⭐⭐⭐⭐ | ReflectionStore+ReflectiveAgent模式参考，场景适配情感陪伴 |
| 跨会话 #21 | `21_cross_session_memory.ipynb` | ⭐⭐⭐⭐ | SessionState+CrossSessionManager模式参考，SQLite后端适配 |
| 评估 #28 | `28_memory_evaluation.ipynb` | ⭐⭐⭐ | 评估框架结构参考，指标替换为情感陪伴定制指标 |
| 基准 #29 | `29_memory_benchmarks_LoCoMo.ipynb` | ⭐⭐⭐ | 评分方法参考，数据集需定制 |

### 5.2 框架选型参考（仅参考模式，不引入依赖）

| 框架 | 参考价值 | 参考内容 | 不引入的原因 |
|------|---------|---------|-------------|
| Mem0 (#25) | ⭐⭐⭐⭐ | 自动事实提取+更新+冲突解决的API设计模式 | 云服务，本地部署需自建 |
| Zep (#27) | ⭐⭐⭐⭐ | 时序知识图的设计思路、用户隔离模式 | 需服务端 |
| Graphiti (#24) | ⭐⭐⭐ | 从对话到知识图的自动提取流程 | 需Neo4j |
| Letta/MemGPT (#26) | ⭐⭐⭐ | 三层记忆架构(core/recall/archival)的设计哲学 | 虚拟上下文过重 |

### 5.3 代码复用策略

从AMT Notebook中可直接复用的代码模式（需适配为本地LLM调用）：

| 模式 | 来源Notebook | 复用方式 |
|------|-------------|---------|
| `SalienceScorer` 三维评分 | #12 Cell 12 | 直接移植，`get_embedding`替换为本地模型 |
| `EvictionEngine` 双策略 | #12 Cell 16 | 直接移植 |
| `TieredMemory` 数据模型 | #13 Cell 11 | 扩展添加 `emotional_weight` 字段 |
| `HierarchicalMemoryManager.cascading_query` | #13 Cell 15 | 直接移植级联检索逻辑 |
| `MemoryType(Enum)` + `MemoryRouter` | #17 Cell 8/14 | 扩展添加 `EMOTIONAL`，关键词规则本地化 |
| `TemporalMemory` + `TemporalDecay` | #18 数据模型 | 直接移植，volatility分类适配 |
| `SessionState` + `CrossSessionManager` | #21 Cell 9/15 | 参考结构，SQLite适配本地路径 |
| `ReflectionStore` | #16 反思存储 | 参考结构，场景分类适配情感陪伴 |

---

## 6. 实施路线图

### Phase 1：基础补全（第1-2周）

> 目标：补全P0级核心架构，建立分层和路由基础设施

| 任务 | 新增/修改文件 | 依赖 | 工作量 | 验收标准 |
|------|-------------|------|--------|---------|
| **T1.1** 分层记忆架构 | 新建 `memory/hierarchical_memory.py` | sqlite-vec | 3天 | L1/L2/L3层级可读写，级联检索正确率>90% |
| **T1.2** 工作记忆管理 | 新建 `memory/working_memory.py`，修改 `context_governance.py` | T1.1 | 3天 | 显著性评分生效，逐出项归档到L2，对话超8K token时不丢失关键信息 |
| **T1.3** 记忆路由 | 新建 `memory/memory_router.py` | T1.1, emotional_memory, knowledge_graph | 2天 | 4种类型路由正确率>85%，规则路由延迟<5ms |
| **T1.4** 集成测试 | 修改 `agent_core/` 主循环 | T1.1-T1.3 | 2天 | 全链路端到端测试通过，无回归 |

**Phase 1 依赖关系**：
```
T1.1 分层记忆架构
 ├── T1.2 工作记忆管理（逐出到L2依赖分层架构）
 └── T1.3 记忆路由（路由目标依赖各存储就绪）
      └── T1.4 集成测试
```

### Phase 2：认知增强（第3-4周）

> 目标：补全P1级认知能力，实现时间推理、自反思和跨会话连续性

| 任务 | 新增/修改文件 | 依赖 | 工作量 | 验收标准 |
|------|-------------|------|--------|---------|
| **T2.1** 时间推理记忆 | 新建 `memory/temporal_memory.py`，修改 `fluid_memory.py` | T1.1 | 3天 | 4种检索模式工作，截至查询正确，stable事实不衰减 |
| **T2.2** 跨会话记忆桥接 | 新建 `memory/cross_session.py`，修改 `agent_core/` 会话生命周期 | T1.1, memory_distiller, emotional_memory | 3天 | 会话恢复3种策略正确，归巢问候生成，冷启动优雅降级 |
| **T2.3** 自反思记忆 | 新建 `memory/reflection_store.py`，修改 `meta_cognition.py` | T1.3 | 2天 | 反思循环触发→生成→存储→检索→注入链路完整 |
| **T2.4** 过程性记忆 | 新建 `memory/procedural_memory.py` | T1.3 | 1天 | 过程性知识提取+存储+检索基本链路 |
| **T2.5** 集成测试 | 修改各模块集成点 | T2.1-T2.4 | 1天 | 全链路测试通过，跨会话场景回归测试通过 |

**Phase 2 依赖关系**：
```
T2.1 时间推理记忆（依赖T1.1分层架构的L2/L3存储）
T2.2 跨会话记忆桥接（依赖T1.1 + memory_distiller）
T2.3 自反思记忆（依赖T1.3路由到ReflectionStore）
T2.4 过程性记忆（依赖T1.3路由到ProceduralStore）
 └── T2.5 集成测试
```

### Phase 3：评估上线（第5-6周）

> 目标：建立评估体系，调优参数，确保生产质量

| 任务 | 新增/修改文件 | 依赖 | 工作量 | 验收标准 |
|------|-------------|------|--------|---------|
| **T3.1** 记忆评估框架 | 新建 `memory/evaluation/` 子包 | T2.5 | 3天 | Recall@K、Precision@K、MRR可计算 |
| **T3.2** 情感陪伴定制基准 | `memory/evaluation/emotional_bench.py` | T3.1 | 2天 | 5维度情感指标可度量 |
| **T3.3** 参数调优 | 修改各模块配置 | T3.1 | 2天 | 全指标对比Phase 0基线提升>20% |
| **T3.4** 生产加固 | 修改 `db/`、`config/`，添加备份/恢复 | T2.5 | 1天 | 数据备份/恢复可用，TTL清理工作 |
| **T3.5** 文档和CI | 更新README/CHANGELOG，添加CI评估步骤 | T3.1-T3.4 | 2天 | 文档完整，CI包含记忆评估步骤 |

**Phase 3 依赖关系**：
```
T3.1 评估框架
 ├── T3.2 情感基准测试
 └── T3.3 参数调优
T3.4 生产加固
 └── T3.5 文档和CI
```

### 整体路线图甘特图

```
Week 1-2  |████████████████| Phase 1: T1.1→T1.2→T1.3→T1.4
Week 3-4  |████████████████| Phase 2: T2.1/T2.2(并行)→T2.3/T2.4(并行)→T2.5
Week 5-6  |████████████████| Phase 3: T3.1→T3.2/T3.3(并行)→T3.4→T3.5
```

---

## 7. 预期效果

### 7.1 定量指标预期

| 指标 | 当前基线(v0.5.03) | Phase 1后 | Phase 2后 | Phase 3后 |
|------|-------------------|-----------|-----------|-----------|
| AMT技术覆盖度 | 47% | 63% | 80% | 87% |
| 长对话信息保留率 | ~60%（8K+token时FIFO丢失关键信息） | ~85% | ~92% | ~95% |
| 跨会话记忆连续性 | 35%（仅有SQLite持久） | 40% | 85% | 90% |
| 记忆检索准确率 | 未度量 | 未度量 | 未度量 | Recall@5 > 80% |
| 情感记忆召回率 | ~50% | ~65% | ~80% | ~85% |
| 矛盾事实检测率 | 0% | 0% | ~60% | ~85% |
| 冷启动用户体验 | 空白 | 空白 | 优雅降级 | 个性化冷启动 |

### 7.2 定性效果预期

| 维度 | 当前状态 | 优化后 |
|------|---------|--------|
| **对话深度** | 长对话后信息丢失，重复询问用户 | 智能保留关键信息，长对话不丢失 |
| **关系连续性** | 新会话=陌生人，需重新自我介绍 | "好久不见"自然衔接，记忆跨会话 |
| **情感关怀** | 安慰策略一成不变 | 从过往经验学习，安慰策略随关系进化 |
| **时间感知** | 无时间推理，不能关联过去事件 | "上次你说..."，理解事件先后因果 |
| **可维护性** | 记忆模块各自为政 | 统一路由+分层架构，新模块即插即用 |
| **可评估性** | 记忆质量全凭主观 | 量化指标驱动迭代优化 |

### 7.3 新增模块汇总

```
memory/
├── hierarchical_memory.py     # Phase 1 — 分层记忆架构
├── working_memory.py          # Phase 1 — 工作记忆管理
├── memory_router.py           # Phase 1 — 记忆路由
├── temporal_memory.py         # Phase 2 — 时间推理记忆
├── cross_session.py           # Phase 2 — 跨会话记忆桥接
├── reflection_store.py        # Phase 2 — 自反思记忆
├── procedural_memory.py       # Phase 2 — 过程性记忆
└── evaluation/                # Phase 3 — 记忆评估
    ├── __init__.py
    ├── metrics.py
    ├── eval_dataset.py
    ├── eval_harness.py
    └── emotional_bench.py
```

---

## 8. 不采纳的技术

| 技术 | 编号 | 不采纳原因 | 替代方案 |
|------|------|-----------|---------|
| **多Agent共享记忆** | #22 | xiaoda-agent是单用户本地部署，无多Agent协作场景；共享记忆的消息传递、一致性协议对本地部署无价值 | 不需要替代，架构不需要 |
| **Letta/MemGPT虚拟上下文** | #26 | 虚拟上下文机制（inner/outer monologue、heartbeat）对本地部署资源要求过高；xiaoda-agent的本地LLM推理能力不支持MemGPT的self-editing循环 | 分层记忆架构(#13)实现类似的三层结构但更轻量 |
| **Graphiti图记忆** | #24 | 需要Neo4j数据库，对本地部署（尤其是Windows桌面端）过重；xiaoda-agent已有knowledge_graph.py基础版 | 增强现有KG + 时间推理记忆(#18)替代时序图 |
| **Mem0托管模式** | #25 | 云端托管记忆服务，违背本地部署原则；且依赖外部API | 参考Mem0的事实提取+更新+冲突解决API设计模式，本地实现 |
| **Zep服务端模式** | #27 | 需要Zep服务端部署，增加部署复杂度 | 参考Zep的时序KG设计和用户隔离模式，本地实现 |

**为什么不采纳多Agent共享记忆（详细论述）**：

xiaoda-agent 的核心定位是**单用户本地部署的情感陪伴Agent**。这意味着：
1. 不存在多个Agent需要协作的场景——一个Agent服务于一个用户
2. 共享记忆需要的消息传递协议（message passing）、一致性协议（agreement protocol）在单用户场景下完全冗余
3. 引入共享记忆会增加锁竞争、数据同步等并发问题的复杂度
4. AMT Notebook #22 的核心价值在于多Agent团队的协调，而非记忆技术本身

**为什么不采纳Letta/MemGPT（详细论述）**：

Letta/MemGPT的核心创新是**虚拟上下文管理**——通过inner monologue和self-editing模拟无限上下文。但不适合xiaoda-agent的原因：
1. 资源要求：MemGPT的heartbeat机制需要持续LLM推理，本地模型推理延迟高
2. 复杂度：self-editing core memory的循环逻辑对7B-14B本地模型来说不可靠
3. 替代方案更好：分层记忆架构(#13) + 工作记忆(#12)实现了类似的效果但更简单可控
4. xiaoda-agent的对话场景不需要MemGPT设计的OS-like内存管理——情感陪伴对话的上下文模式相对可预测

---

## 附录A：AMT 30种技术快速索引

| # | 技术 | 家族 | 核心问题 | xiaoda-agent状态 |
|---|------|------|---------|-----------------|
| 01 | Conversation Buffer | 短期 | 逐字保存对话 | ✅ 完整 |
| 02 | Sliding Window | 短期 | 只保留最近K轮 | ✅ 完整 |
| 03 | Summary Memory | 短期 | 旧消息压缩为摘要 | ✅ 完整 |
| 04 | Summary Buffer | 短期 | 摘要+近期消息 | ✅ 部分 |
| 05 | Token Buffer | 短期 | 严格token预算 | ✅ 完整 |
| 06 | Vector Store | 长期 | 语义相似检索 | ✅ 完整 |
| 07 | Entity Memory | 长期 | 实体事实追踪 | ✅ 部分 |
| 08 | Knowledge Graph | 长期 | 关系图推理 | ✅ 部分 |
| 09 | Episodic Memory | 长期 | 事件经历存储 | ✅ 部分 |
| 10 | Semantic Memory | 长期 | 通用事实知识 | ✅ 部分 |
| 11 | Procedural Memory | 长期 | 过程性知识 | ❌ Phase 2 |
| 12 | Working Memory | 认知 | 上下文窗口智能管理 | ❌ Phase 1 |
| 13 | Hierarchical Layers | 认知 | 热/温/冷分层 | ❌ Phase 1 |
| 14 | Consolidation | 认知 | 记忆合并去重 | ✅ 部分 |
| 15 | Compaction | 认知 | 记忆压缩 | ✅ 完整 |
| 16 | Self-Reflection | 认知 | 自我反思学习 | ❌ Phase 2 |
| 17 | Memory Routing | 认知 | 智能路由分发 | ❌ Phase 1 |
| 18 | Temporal Memory | 认知 | 时间推理 | ❌ Phase 2 |
| 19 | Forgetting & Decay | 认知 | 有意遗忘 | ✅ 完整 |
| 20 | Retrieval Patterns | 检索 | 混合检索策略 | ✅ 部分 |
| 21 | Cross-Session | 检索 | 跨会话连续性 | ❌ Phase 2 |
| 22 | Multi-Agent Shared | 检索 | 多Agent共享 | ❌ 不采纳 |
| 23 | Memory as Tools | 检索 | 记忆工具化 | ✅ 部分 |
| 24 | Graphiti | 框架 | 时序知识图 | ❌ 不采纳 |
| 25 | Mem0 | 框架 | 托管记忆层 | ⚠️ 仅参考 |
| 26 | Letta/MemGPT | 框架 | 虚拟上下文 | ❌ 不采纳 |
| 27 | Zep | 框架 | 对话记忆服务 | ⚠️ 仅参考 |
| 28 | Evaluation | 评估 | 记忆质量度量 | ❌ Phase 3 |
| 29 | LoCoMo Benchmarks | 评估 | 标准化基准 | ❌ Phase 3 |
| 30 | Production Patterns | 评估 | 生产部署 | ⚠️ 参考 |

## 附录B：关键配置参数

```python
# config/agent.json5 中新增的记忆系统配置

"memory": {
    "hierarchical": {
        "l1_capacity_tokens": 4096,     // L1热层token容量
        "l2_capacity": 1000,            // L2温层条目上限
        "promote_threshold": 3,         // 访问N次晋升
        "demote_staleness_hours": 48,   // N小时未访问降级
        "emotional_pin_threshold": 0.8, // 情感权重>此值永不降级
    },
    "working_memory": {
        "max_tokens": 4096,
        "eviction_policy": "importance_weighted_lru",
        "recency_decay_rate": 0.1,      // 时间衰减系数
        "emotional_source_weight": 0.95, // 情感来源权重
    },
    "router": {
        "use_llm_routing": true,        // 是否启用LLM精确路由
        "rule_routing_first": true,      // 规则路由优先
        "fallback_to_all": true,         // 路由不确定时全量搜索
    },
    "temporal": {
        "recency_weight": 0.3,           // 时间权重
        "stable_half_life_hours": null,   // stable不衰减
        "semi_stable_half_life_hours": 720, // 30天
        "volatile_half_life_hours": 72,    // 3天
    },
    "cross_session": {
        "full_load_threshold_hours": 1,  // <1小时全量加载
        "last_n_threshold_hours": 24,    // <24小时加载最近N轮
        "recent_messages_count": 10,     // 保留最近N轮
    },
    "reflection": {
        "enabled": true,
        "trigger_on_emotion_shift": true,
        "trigger_on_cold_conversation": true,
        "max_reflections_per_scene": 5,
    },
    "evaluation": {
        "enabled": false,                // Phase 3开启
        "auto_eval_on_dream": false,     // 梦境整合后自动评估
        "bench_dataset_path": "data/eval/",
    }
}
```

---

> **文档维护说明**：本文档随xiaoda-agent记忆模块实现进展同步更新。每个Phase完成后更新覆盖度表格和预期效果指标。
