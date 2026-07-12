# FSRS-DSR 记忆系统重设计

> 日期: 2026-07-12
> 状态: 已确认，待实现
> 替代: fluid_memory.py + salience.py(检索评分) + recency_boost + peak_weight

## 1. 动机

当前记忆系统存在 4 套互相冲突的评分体系：

1. **FluidMemory** — `score = similarity × peak_weight × e^(-days/(3+14×access))`
2. **SalienceScorer** — `0.4×recency + 0.3×frequency + 0.3×emotion`（1 小时半衰期）
3. **recency_boost** — 阶梯函数 (0/7/30/90 天分档)
4. **final_score** — `0.5×rerank + 0.3×fluid + 0.1×kg + 0.1×recency`

核心问题：
- 时间衰减被双重计算（fluid_score 含 e^(-t/S)，recency_boost 又算一次）
- `peak_weight` 形同虚设（默认 1.0，从未在检索评分中更新）
- `access_count` 线性映射到 S (`3+14×n`)，5 次后永久化太粗暴
- 缺少"难度"维度——简单事实和复杂推理的衰减速率相同
- CognitiveMemory 和 MemoryManager 两套半衰期差 720 倍（1 小时 vs 3~143 天）

设计目标：
- **用最少空间记录更多记忆** — 遗忘是存储压缩策略，不是目的
- **重要记忆不丢** — 多次提及的记忆逐步升级为永久记忆
- **垃圾信息自动淘汰** — 从未被提及的记忆快速归档
- **单一衰减公式** — 消除双重计算，R = e^(-t/S) 是唯一的时间衰减

## 2. 现有体系可取之处分析

在用 FSRS-DSR 替代现有 4 套评分体系之前，先梳理每套体系中值得保留的设计。

### 2.1 FluidMemory — `similarity × peak_weight × e^(-days/(3+14×access))`

| 设计点 | 价值 | 保留决策 |
|--------|------|----------|
| 21 天缓冲期 (`GRACE_DAYS=21`) | 给新记忆"存活机会"，避免刚蒸馏就被衰减 | ✅ 保留，作为 BUFFER 阶段 |
| 永久记忆阈值 (`PERMANENT_ACCESS_THRESHOLD=5`) | 反复确认的记忆不衰减 | ✅ 保留，改为 S ≥ 30 天 |
| 三层保护结构 (永久→缓冲→衰减) | 分层决策比一刀切更合理 | ✅ 保留，扩展为 5 阶段状态机 |
| `score()` 纯函数设计 | 无副作用，可复用于 DreamConsolidator 和检索评分 | ✅ 保留，`retrievability()` 也是纯函数 |
| `should_filter()` / `should_archive()` 阈值分离 | 过滤（不返回）和归档（不删除）是不同决策 | ✅ 保留，R_ARCHIVE=0.05, R_FORGET=0.02 |
| `S = 3 + 14×access_count` 线性增长 | 5 次确认 S=73 天，但第 5 次和第 1 次确认质量可能完全不同 | ❌ 废弃，改为 FSRS 动态增长 |
| `peak_weight` 从未被更新 | `BOOST_PER_ACCESS=0.15` 定义了但从未在检索路径中应用 | ❌ 废弃，S 替代 |
| 三值连乘 `similarity × peak_weight × retention` | 三个 [0,1] 值相乘导致分数坍缩过快（0.7×0.6×0.5=0.21） | ❌ 废弃，改为加权和 |

### 2.2 SalienceScorer — `0.4×recency + 0.3×frequency + 0.3×emotion`

| 设计点 | 价值 | 保留决策 |
|--------|------|----------|
| **情绪维度** (`_emotion_score`) | 情绪强烈的记忆确实更难忘（心理学: 闪光灯记忆效应） | ✅ **核心保留** — FSRS 为无情绪的"卡片复习"设计，Agent 记忆有情绪标签，这是唯一被 FSRS 遗漏的维度 |
| PAD arousal × 标签匹配 | 双信号融合比单一标签更鲁棒 | ✅ 保留，用于 consolidation 决策和 D 初始化 |
| 对数归一化频率 (`log1p(access)/10`) | 避免高频记忆分数爆炸 | ⚠️ 部分保留，FSRS 用 S 替代频率，但 D 的均值回归有类似效果 |
| 三维加权求和 (而非乘法) | 加法比乘法更稳定，不会因一个维度低而整体坍缩 | ✅ 保留思路，最终评分公式用加权和 |
| 1 小时半衰期 (`RECENCY_HALF_LIFE=3600`) | 对 Agent 长期记忆太短，1 小时后 R≈0.001 | ❌ 废弃，R=e^(-t/S) 统一 |
| recency 用 `last_accessed` 而非 `created_at` | 导致"被看了一眼的垃圾记忆"比"从未被看的重要记忆"分数更高 | ❌ 废弃，R 基于 last_review (提及时间) |

**关键洞察: 情绪维度是现有体系中唯一被 FSRS-DSR 遗漏的维度。** 保留方式:

1. **情绪影响 D 初始化**: 情绪强烈的记忆 D += 1（更难稳定）
2. **情绪影响 consolidation 决策**: SalienceScorer 降级为 CognitiveMemory 内部的 consolidation 辅助，不再参与检索评分
3. **闪光灯记忆效应**: 情绪记忆虽然 D 更高（S 增长更慢），但情绪记忆更容易被提及（用户倾向反复聊情绪相关话题），因此 S 最终会通过多次提及增长到永久

### 2.3 recency_boost — 阶梯函数 (0/7/30/90 天)

| 设计点 | 价值 | 保留决策 |
|--------|------|----------|
| "今天"和"7天内"的区分 | 近期记忆确实应该有额外加成 | ✅ 被 R=e^(-t/S) 自然覆盖（新记忆 S=3 时 R≈1.0，21 天缓冲期内 R=1.0） |
| 无时间信息给 0.3 | 防止缺失时间戳的记忆被误杀 | ✅ 保留思路，缺失时间戳时 R 默认 0.5 |
| 阶梯函数不连续 | 第 7 天 R=0.8，第 8 天 R=0.5，一天之内跳变 37.5% | ❌ 废弃，连续指数函数替代 |
| 与 fluid_score 双重计算时间衰减 | fluid_score 已含 e^(-t/S)，recency_boost 又算一次 | ❌ 废弃，R 统一时间衰减 |

**关键洞察: 阶梯函数的唯一价值是"近期记忆加成"，但 R=e^(-t/S) 已经天然实现了这一点。不需要额外的 recency_boost。**

### 2.4 final_score — `0.5×rerank + 0.3×fluid + 0.1×kg + 0.1×recency`

| 设计点 | 价值 | 保留决策 |
|--------|------|----------|
| 加权和而非连乘 | 比乘法更稳定，一个维度低不会拖垮整体 | ✅ 保留 |
| rerank 权重最高 (0.5) | 语义相关性是最重要的检索信号 | ✅ 保留 |
| KG 实体匹配加成 | 结构化知识增强检索质量 | ✅ 保留 |
| 中间分数字段可观测 | `r["fluid_score"]`, `r["kg_boost"]` 等便于调试 | ✅ 保留，增加 `r["retrievability"]` |
| fluid 和 recency 双重计算时间 | 0.3×fluid + 0.1×recency 中时间衰减被算了两次 | ❌ 废弃，R 统一 |
| 缺少 importance 维度 | `effective_score = importance × fluid_score` 在过滤阶段用了 importance，但 final_score 没有 | ❌ 修复，补上 importance |

### 2.5 继承关系总览

```
FluidMemory ──→  21天缓冲期 ✅
             ──→  永久记忆阈值 ✅
             ──→  纯函数设计 ✅
             ──→  filter/archive 分离 ✅
             ──→  S=3+14×access ❌ → S 动态增长
             ──→  peak_weight ❌ → 废弃

SalienceScorer ──→  情绪维度 ✅ ← 唯一被 FSRS 遗漏的维度
                ──→  PAD arousal×标签匹配 ✅
                ──→  加权和(非乘法) ✅
                ──→  1小时半衰期 ❌ → R=e^(-t/S) 统一
                ──→  recency 用 last_accessed ❌ → last_review

recency_boost ──→  "近期加成"意图 ✅ → 被 R 天然覆盖
               ──→  阶梯函数 ❌ → 连续指数函数
               ──→  双重衰减 ❌ → 删除

final_score ──→  加权和结构 ✅
             ──→  rerank 0.5 ✅
             ──→  KG 0.1 ✅
             ──→  可观测性 ✅
             ──→  fluid+recency 双重 ❌ → R 统一
             ──→  缺 importance ❌ → 补上

★ 新增: 情绪 → 影响 D 初始化 (D += 1 if emotion)
★ 新增: final = 0.5×rerank + 0.3×R + 0.1×kg + 0.1×importance
★ 新增: SalienceScorer 降级为 consolidation 决策辅助
```

## 3. 理论基础: FSRS-DSR 模型

参考:
- FSRS (Free Spaced Repetition Scheduler), Jarrett Ye, ACM SIGKDD 2022
- 1.9 亿条人类记忆行为数据训练
- DSR 三变量: Difficulty / Stability / Retrievability

核心公式:

```
R(t) = e^(-t/S)

其中:
  t = 距上次提及的天数
  S = 记忆稳定性 (天), 等价于半衰期的度量
  R = 当前可提取概率 [0,1], 即"权重"
```

S 的物理含义: R 衰减到 1/e ≈ 0.37 所需的天数。S=3 意味着 3 天后只剩 37% 权重。

## 4. 记忆生命周期状态机

```
蒸馏产出
   │
   ▼
┌──────────┐    21天内被提及≥1次     ┌──────────────┐
│  BUFFER  │ ──────────────────────→ │  REINFORCED  │
│ (0-21天) │                         │  (S持续增长)  │
│  R=1.0   │ ←────────────────────  │              │
│  不衰减   │    衰减中被重新提及      └──────┬───────┘
└────┬─────┘ ─────────────────────          │
     │                                    S ≥ 30
     │ 21天到期，从未被提及                    │
     ▼                                    ▼
┌──────────┐                      ┌──────────────┐
│  DECAY   │   R < 0.05          │  PERMANENT   │
│ (快速衰减)│ ──────────────────→  │  (永不衰减)   │
│ R=e^(-t/S)│                      │  R = 1.0     │
└──────────┘                      └──────────────┘

任何状态: R < 0.02 → ARCHIVED (深度归档)
```

状态定义:

| 状态 | R | S | 说明 |
|------|---|---|------|
| BUFFER | 1.0 (恒定) | 初始 3.0，提及则增长 | 0-21 天缓冲期，不衰减 |
| REINFORCED | e^(-t/S) | 已增长 | 被提及过，正常衰减 |
| DECAY | e^(-t/S) | 初始 3.0 | 从未被提及，快速衰减 |
| PERMANENT | 1.0 (恒定) | ≥30 | 永久记忆，不衰减 |
| ARCHIVED | N/A | N/A | 已归档，不参与检索 |

## 5. 核心算法

### 5.1 Retrievability 计算

```python
def retrievability(self, now: float) -> float:
    if self.state == MemoryPhase.BUFFER:
        return 1.0
    if self.state == MemoryPhase.PERMANENT:
        return 1.0
    if self.stability <= 0:
        return 0.0
    elapsed_days = max(0, (now - self.last_review) / 86400.0)
    return math.exp(-elapsed_days / self.stability)
```

### 5.2 稳定性增长 (提及时)

```python
def update_stability_recall(S: float, D: float, R: float,
                            signal: ReinforcementSignal) -> float:
    """FSRS 简化: 提及时 S 增长

    核心洞察:
    - R 越低时被提及，S 增长越大 ("差点忘了但想起来了"效果最强)
    - D 越高，S 增长越小 (难记的东西就是难稳定)
    - 信号越强，S 增长越大
    """
    difficulty_factor = (10 - D) / 9.0   # D=1→1.0, D=10→0.11
    retrievability_bonus = 1.0 + 2.0 * (1.0 - R)  # R低时增长大

    growth = signal.growth_factor * difficulty_factor * retrievability_bonus
    S_new = S * (1.0 + growth)
    return min(S_new, S * 10.0)  # 单次增长上限 10 倍
```

信号强度与 growth_factor:

| 信号 | 触发条件 | growth_factor |
|------|---------|---------------|
| STRONG_CONFIRM | 用户主动说"对/没错/就是" | 2.0 |
| PASSIVE_USE | 记忆被检索并引用到回答中 | 1.5 |
| WEAK_HIT | 记忆被检索但未引用 | 1.0 |
| CORRECT | 用户纠正记忆内容 | 特殊处理 (见 5.3) |

### 5.3 稳定性回退 (纠正时)

```python
def update_stability_forget(S: float, D: float, R: float) -> float:
    """FSRS 简化: 纠正时 S 回退

    用户纠正 = 记忆有误，稳定性应下降
    """
    regress = 0.5
    d_power = 0.3
    s_alpha = 0.2
    S_new = S * regress * (D ** (-d_power)) * (((S + 1) ** s_alpha) - 1)
    return max(1.0, S_new)
```

### 5.4 难度初始化与更新

难度 D 的初始化融合了内容特征和情绪信号。情绪维度是从 SalienceScorer 继承的核心价值——
FSRS 为无情绪的"卡片复习"设计，但 Agent 记忆有情绪标签，这是唯一被 FSRS 遗漏的维度。

**闪光灯记忆效应**: 情绪强烈的记忆虽然 D 更高（S 增长更慢），但情绪记忆更容易被提及
（用户倾向反复聊情绪相关话题），因此 S 最终会通过多次提及增长到永久。

```python
def estimate_initial_difficulty(content: str, emotion_label: str = "") -> float:
    D = 5.0
    length = len(content)
    if length < 20:   D -= 1.0
    elif length > 200: D += 1.5
    if emotion_label and emotion_label not in ("neutral", ""):
        D += 1.0
    fact_kw = ["生日", "电话", "地址", "名字", "日期", "号码"]
    pref_kw = ["喜欢", "讨厌", "偏好", "习惯", "总是"]
    abst_kw = ["因为", "所以", "意味着", "本质上", "原理"]
    if any(k in content for k in fact_kw):      D -= 2.0
    elif any(k in content for k in pref_kw):    D -= 1.0
    elif any(k in content for k in abst_kw):    D += 2.0
    return max(1.0, min(10.0, D))


def update_difficulty(D: float, signal: ReinforcementSignal) -> float:
    """均值回归: 防止 D 无限偏离"""
    D0 = 5.0
    mean_revert = 0.3
    delta_map = {
        ReinforcementSignal.STRONG_CONFIRM: -0.5,
        ReinforcementSignal.PASSIVE_USE: -0.2,
        ReinforcementSignal.WEAK_HIT: 0.0,
        ReinforcementSignal.CORRECT: 1.0,
    }
    delta = delta_map.get(signal, 0.0)
    D_new = mean_revert * D0 + (1 - mean_revert) * (D + delta)
    return max(1.0, min(10.0, D_new))
```

### 5.5 7 天回看窗口: 提及检测

每次用户对话时，检索过去 7 天内创建的记忆:

```python
async def detect_reinforcement(self, query: str, now: float) -> list[tuple[str, ReinforcementSignal]]:
    """语义相似度检测提及"""
    recent = await self.db.get_memories_since(now - 7 * 86400)
    signals = []
    for mem in recent:
        sim = await self.compute_similarity(query, mem["summary"])
        if sim > 0.6:
            signal = ReinforcementSignal.PASSIVE_USE
            signals.append((mem["id"], signal))
    return signals
```

### 5.6 状态转换规则

```python
def transition(self, now: float) -> MemoryPhase:
    age_days = (now - self.created_at) / 86400.0

    if self.state == MemoryPhase.BUFFER:
        if age_days > BUFFER_DAYS:
            if self.reinforcement_count == 0:
                return MemoryPhase.DECAY
            elif self.stability >= S_PERMANENT:
                return MemoryPhase.PERMANENT
            else:
                return MemoryPhase.REINFORCED
        return MemoryPhase.BUFFER

    if self.state in (MemoryPhase.REINFORCED, MemoryPhase.DECAY):
        R = self.retrievability(now)
        if R < R_ARCHIVE:
            return MemoryPhase.ARCHIVED
        if self.stability >= S_PERMANENT:
            return MemoryPhase.PERMANENT
        return self.state

    if self.state == MemoryPhase.PERMANENT:
        return MemoryPhase.PERMANENT

    return self.state
```

## 6. 统一评分公式

替代现有 4 套体系:

```python
# 旧: final = 0.5×rerank + 0.3×fluid + 0.1×kg + 0.1×recency
# 新: final = 0.5×rerank + 0.3×R + 0.1×kg + 0.1×importance

R = retrievability(now)  # e^(-t/S), 唯一的时间衰减
```

R 统一了:
- fluid_score (艾宾浩斯衰减) → R
- recency_boost (时间新鲜度) → R
- peak_weight (已废弃, S 替代)

importance 从记忆元数据取，不再乘以 R (避免双重衰减)。

## 7. 阈值常量

| 常量 | 值 | 说明 |
|------|---|------|
| S_PERMANENT | 30.0 天 | S ≥ 30 → 永久记忆 |
| R_ARCHIVE | 0.05 | R < 0.05 → 归档 |
| R_FORGET | 0.02 | R < 0.02 → 深度归档 |
| BUFFER_DAYS | 21 | 缓冲期天数 |
| LOOKBACK_DAYS | 7 | 回看窗口天数 |
| SIMILARITY_THRESHOLD | 0.6 | 提及检测语义相似度阈值 |
| S_INIT | 3.0 天 | 初始稳定性 |
| D_INIT | 5.0 | 初始难度 (中等) |

## 8. 权重变化示例

### 场景 A: 21 天内被提及 3 次 → 永久记忆

```
R 1.0 ───────●──────────●──────────●─────────────────────→ 永久
             第3天       第10天      第18天
             强确认       被动使用    弱命中
             S: 3→8.4    S: 8.4→18  S: 18→30+
```

### 场景 B: 21 天内被提及 1 次 → 缓慢衰减

```
R 1.0 ──────────────────●────────────────╲──────────────
                        第14天             ╲  R≈0.5@30天
                        被动使用             ╲ R≈0.05@90天→归档
                        S: 3→6.3
```

### 场景 C: 21 天内从未被提及 → 快速衰减

```
R 1.0 ───────────────────────────────╲──────────────────
                                     ╲
                                      ╲ R≈0.37@24天
                                       ╲ R≈0.05@33天→归档
                        S=3 (初始值，从未增长)
```

### 场景 D: 衰减中被重新提及 → S 增长，R 回升

```
R 1.0 ───────────────╲──────●──────────────────────────
                      ╲      ↑ 第40天被提及
                       ╲     R从0.2回升，S从3→12
                        ╲    然后以更慢速度衰减
```

## 9. 模块变更清单

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `memory/fluid_memory.py` | 替换 → `memory/fsrs_model.py` | DSR 三变量模型 |
| `memory/memory_manager.py` | 修改 | `_apply_fluid_scoring` → `_apply_fsrs_scoring`; `_compute_final_scores` 新公式; 删除 `_compute_recency_boost` |
| `memory/confirm_correct.py` | 修改 | confirm 更新 S 和 D，而非 weight+0.15 |
| `memory/concept_graph.py` | 修改 | 节点增加 difficulty, stability, phase 字段 |
| `memory/cognitive_memory.py` | 修改 | SalienceScorer 仅用于 consolidation 决策 |
| `core/dream_consolidation.py` | 修改 | 用 R 值判断归档，不再双重衰减 |
| `memory/recall_scheduler.py` | 修改 | 回忆时更新 last_review + S |
| `memory/salience.py` | 保留但降级 | 仅 CognitiveMemory 内部 consolidation 决策使用；情绪维度通过 D 初始化融入 FSRS |
| `db/database.py` | 修改 | episodic_memories 表增加 difficulty, stability, phase 列 |
| `db/db_concept.py` | 修改 | concept_nodes 表增加 difficulty, stability, phase 列 |

## 10. 数据库 Schema 变更

### episodic_memories 表新增列

```sql
ALTER TABLE episodic_memories ADD COLUMN difficulty REAL NOT NULL DEFAULT 5.0;
ALTER TABLE episodic_memories ADD COLUMN stability REAL NOT NULL DEFAULT 3.0;
ALTER TABLE episodic_memories ADD COLUMN phase TEXT NOT NULL DEFAULT 'buffer';
ALTER TABLE episodic_memories ADD COLUMN last_review REAL NOT NULL DEFAULT 0;
ALTER TABLE episodic_memories ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0;
```

### concept_nodes 表新增列

```sql
ALTER TABLE concept_nodes ADD COLUMN difficulty REAL NOT NULL DEFAULT 5.0;
ALTER TABLE concept_nodes ADD COLUMN stability REAL NOT NULL DEFAULT 3.0;
ALTER TABLE concept_nodes ADD COLUMN phase TEXT NOT NULL DEFAULT 'buffer';
ALTER TABLE concept_nodes ADD COLUMN last_review TEXT;
ALTER TABLE concept_nodes ADD COLUMN reinforcement_count INTEGER NOT NULL DEFAULT 0;
```

## 11. 迁移策略

1. 新列都有 DEFAULT 值，旧数据自动获得 D=5.0, S=3.0, phase='buffer'
2. 旧数据统一 S=3.0, phase='buffer'，让它们自然经历 21 天缓冲期重新评估（不根据 access_count 估算 S，避免线性映射违背 DSR 模型）
3. 唯一例外: 旧数据中 `access_count >= 5` 的直接标记为 `phase='permanent'`（这些是已被反复确认的核心记忆，不应因迁移而丢失）
4. 迁移脚本在 DreamConsolidator 下次 consolidate 时执行，不需要停机

## 12. 测试策略

- `tests/test_fsrs_model.py` — 单元测试: R 计算、S 更新、D 更新、状态转换
- `tests/test_fsrs_scoring.py` — 集成测试: 新评分公式与旧公式对比
- `tests/test_fsrs_lifecycle.py` — 生命周期测试: buffer → reinforced → permanent / decay → archived
- `tests/test_fsrs_reinforcement.py` — 提及检测测试: 7 天窗口语义匹配
- 所有现有 `test_fluid_memory.py` 测试迁移到 `test_fsrs_model.py`