# xiaoda-agent 记忆系统优化 Spec：基于 mind 扩散激活机制

> **版本**: v1.0  
> **日期**: 2025-07-11  
> **目标版本**: xiaoda-agent v0.6.0  
> **参考项目**: [Da7-Tech/mind](https://github.com/Da7-Tech/mind) v6.2.8  

---

## 目录

1. [现状诊断](#1-现状诊断)
2. [mind 核心机制深度分析](#2-mind-核心机制深度分析)
3. [差距对比表](#3-差距对比表)
4. [优化方案](#4-优化方案)
5. [实施步骤](#5-实施步骤)
6. [预期效果](#6-预期效果)
7. [不采纳的部分](#7-不采纳的部分)

---

## 1. 现状诊断

### 1.1 检索方式单一，无联想能力

当前 xiaoda-agent 的检索完全依赖 **向量余弦相似度 + FTS5 关键词** 的双通道 top-k 模式：

```python
# memory/memory_manager.py（简化示意）
results = vector_store.search(query_embedding, top_k=5)  # 纯向量相似度
results += fts5.search(query_keywords, top_k=5)           # 纯关键词
results = deduplicate(results)
```

**问题**：
- 向量 top-k 只能返回与查询语义最接近的 N 条记忆，**无法沿语义链路扩散**。例如用户问"我之前用什么数据库？"，如果某条记忆提到"项目用了 PostgreSQL"，另一条提到"PostgreSQL 需要配置连接池"，后者不会因为"PostgreSQL"这个中间节点而被召回。
- FTS5 关键词检索本质上是精确匹配，无法发现隐含关联（如"缓存"与"Redis"的共现关系）。
- 两个通道的结果合并只是去重，没有融合排序机制，无法平衡直接匹配与间接关联。

### 1.2 无扩散激活，记忆召回率低

mind 的核心创新是 **扩散激活（Spreading Activation）**：从查询命中的种子节点出发，沿概念图的边逐跳传播激活值，3 跳内可达的关联记忆都能被召回。xiaoda-agent 完全没有这个机制，导致：

| 场景 | xiaoda-agent 行为 | mind 行为 |
|------|-------------------|-----------|
| 查询"我的项目框架"，记忆中存了"用 FastAPI 做后端"和"后端部署在 Docker" | 只召回 FastAPI 那条 | 召回两条（通过"后端"节点扩散） |
| 查询"配置 Redis"，记忆中只有"缓存用 Redis，端口 6379" | 只召回这一条 | 召回这条 + 扩散到"部署在 Docker" + "Docker 端口映射" |
| 查询"用户偏好"，记忆中无直接命中 | 返回空 | 通过 identity keys 扩散到用户名/城市等关联事实 |

### 1.3 Boost 上限过低，频繁确认的记忆衰减过快

当前 Boost 公式：

```python
# memory/fluid_memory.py（简化）
score = similarity * math.exp(-lambda * days) + min(alpha * math.log(1 + access_count), MAX_BOOST)
# MAX_BOOST = 0.3
```

**问题分析**：
- `MAX_BOOST = 0.3` 意味着无论用户确认多少次，记忆的额外加薪上限只有 0.3。
- 一个被确认 10 次的核心记忆（如"用户名字叫小明"），Boost = min(0.2 × ln(11), 0.3) = 0.3，与确认 3 次的记忆相同。
- 对比 mind：`BOOST_PER_ACCESS = 0.15`，每次确认加 0.15 到 weight（weight 上限 1.0），10 次确认可加 1.5（cap 在 1.0），效果远大于 0.3 的硬上限。
- 结果：频繁使用的核心记忆在 xiaoda-agent 中衰减速度与边缘记忆差异不大，长期使用后核心记忆容易被遗忘。

### 1.4 梦境周期不完整，缺乏真正的三阶段整合

当前 `dream_consolidation.py` 的"梦境"只是：

```python
# core/dream_consolidation.py（简化）
def dream():
    for memory in all_memories:
        days = (now - memory.last_accessed).days
        memory.weight *= math.exp(-lambda * days)  # Ebbinghaus 衰减
    archive_low_weight_memories()  # 归档低权重记忆
```

**缺失**：
1. **无 Light 阶段**：没有会话信号收集与分析。
2. **无 Deep 阶段的边衰减**：只有节点衰减，没有"突触稳态"（边权重衰减）。mind 中 `EDGE_DECAY_PER_DREAM = 0.95`，每次梦境衰减所有边 5%，不用的连接自然弱化。
3. **无 REM 阶段的聚类提升**：没有相似记忆的聚类和提升。mind 中 `CLUSTER_SIM = 0.45, PROMOTION_THRESHOLD = 3`，≥3 条相关记忆聚类后提升到 Cortex（长期巩固）。
4. **无 REM 阶段的矛盾检测**：没有发现相互矛盾的记忆并标记。
5. **无自动触发机制**：mind 在写入信号累积到阈值（`AUTO_DREAM_SIGNALS = 10`）或距上次梦境超过 24 小时时自动触发。

### 1.5 无 confirm/correct 机制

- **confirm**：用户确认某条记忆确实有用 → 强化该记忆及其边权重。xiaoda-agent 无此机制。
- **correct**：用户纠正错误记忆 → 旧记忆标记为"已替代"（superseded），保留溯源链。xiaoda-agent 只能删除旧记忆或添加新记忆，无法保留过渡关系。

### 1.6 无边权重演化

xiaoda-agent 的记忆之间没有语义边，自然也不存在边权重的强化与衰减。mind 中：
- `confirm` → 边权重 +`EDGE_BOOST`（0.25）
- `dream` → 边权重 ×`EDGE_DECAY_PER_DREAM`（0.95）
- 边权重 < `EDGE_PRUNE_THRESHOLD`（0.1）→ 修剪

这种"用进废退"机制使得活跃的语义连接越来越强，不活跃的逐渐消亡，模拟了神经突触的稳态调节。

---

## 2. mind 核心机制深度分析

### 2.1 三层记忆架构

```
┌─────────────────────────────────────────────────┐
│ Layer 1: Working Memory (ACTIVE.md)              │
│ 预算: ~800 字符 (~200 token)                     │
│ 内容: 当前最热的 N 条事实（按 weight+确认数排序）│
│ 生命周期: 每次写入/confirm/dream 后重新生成       │
│ 存储: 文本文件，直接注入 Agent 上下文             │
├─────────────────────────────────────────────────┤
│ Layer 2: Hippocampus (graph.json)                │
│ 结构: 加权概念图 (nodes + edges)                 │
│ 节点: 记忆事实 (text, weight, keys, history...)  │
│ 边:   语义链接 (relation, weight, created)       │
│ 生命周期: Ebbinghaus 衰减，45天宽限期            │
│ 存储: JSON 文件，读写锁保护                      │
├─────────────────────────────────────────────────┤
│ Layer 3: Cortex (cortex/*.md)                    │
│ 结构: 按主题归档的 Markdown 文件                 │
│ 内容: 聚类后提升的巩固知识                       │
│ 生命周期: 永久，除非手动删除                      │
│ 存储: 文件目录，每主题一个 .md                    │
└─────────────────────────────────────────────────┘
```

**数据流转**：
- **写入**：`remember` → Hippocampus 节点 + 自动提取 keys + 自动创建同现边
- **读取**：`recall` → Hippocampus 扩散激活 + RRF 融合
- **巩固**：`dream` → Deep 衰减/修剪 → REM 聚类 → Cortex 提升
- **展示**：`generate` → Working Memory 从 Hippocampus 取 top-N

### 2.2 扩散激活检索（核心算法）

mind 的 `recall` 不是简单的向量 top-k，而是一个 **三通道融合** 过程：

#### 2.2.1 通道一：直接命中（Direct Channel）

```python
# 对每个存活节点计算 IDF 加权的 key 重叠分
direct = {}
for nid in alive_nodes:
    node_keys = set(node["keys"])
    shared_keys = query_keys & node_keys
    if shared_keys:
        idf_score = sum(idf[k] for k in shared_keys)  # 稀有词权重更高
        weight_bias = 0.35 + 0.65 * node["weight"]     # 权重偏置，下限0.35
        direct[nid] = idf_score * weight_bias
    # 子串包含也加分
    if query_tokens_in_node_text or node_keys_in_query:
        direct[nid] += (substr_count + reverse_count) * 0.6 * weight_bias
```

**关键设计**：
- `weight_bias` 的下限是 0.35（不是 0），确保衰减的记忆如果精确匹配，仍能胜过新鲜但不相关的记忆。
- IDF 机制让稀有关键词（如人名"小明"）的匹配远高于常见词（如"项目"）。

#### 2.2.2 通道二：扩散激活（Spreading Channel）

```python
# 从直接命中节点出发，沿边扩散激活
spread = defaultdict(float)
wave = dict(direct)  # 初始波 = 直接命中节点

for hop in range(max_hops + 1):  # max_hops = RECALL_RADIUS = 3
    nxt = defaultdict(float)
    for nid, activation in wave.items():
        spread[nid] += activation  # 累积激活
        if hop < max_hops and activation > SPREADING_THRESHOLD:  # > 0.05
            for neighbor, edge in edges[nid].items():
                if neighbor not in alive: continue
                # 激活值 = 当前激活 × 衰减系数 × 边权重 / 距离
                propagated = activation * ACTIVATION_DECAY * edge["weight"] / (hop + 1)
                nxt[neighbor] += propagated
    wave = nxt
    if not wave: break
```

**参数解析**：
| 参数 | 值 | 含义 |
|------|-----|------|
| `RECALL_RADIUS` | 3 | 最大扩散跳数 |
| `ACTIVATION_DECAY` | 0.5 | 每跳激活衰减50% |
| `SPREADING_THRESHOLD` | 0.05 | 低于此值不继续传播 |
| 边权重 | 0.0~1.0 | 确认强化+梦境衰减的演化值 |

**示例**：3跳扩散的激活衰减
```
hop 0: activation = 1.0 （种子节点）
hop 1: activation = 1.0 × 0.5 × edge_weight / 1 = 0.5 × w
hop 2: activation = 0.5w × 0.5 × edge_weight / 2 = 0.125 × w1 × w2
hop 3: activation = 0.125w1w2 × 0.5 × edge_weight / 3 = 0.021 × w1 × w2 × w3
```

#### 2.2.3 RRF 融合

```python
# Reciprocal Rank Fusion：两个通道的排名融合
dr = rank_by_score(direct)    # 直接通道排名
sr = rank_by_score(spread)    # 扩散通道排名
rrf_k = 60  # 平滑参数

fused = {}
for nid in (direct_keys | spread_keys):
    fused[nid] = 1.0 / (rrf_k + dr.get(nid, default)) + \
                 1.0 / (rrf_k + sr.get(nid, default))
```

**为什么用 RRF 而非简单加权**：
- 直接通道和扩散通道的分数尺度不同，简单加权需要调权重。
- RRF 基于排名而非原始分数，尺度无关，鲁棒性好。
- `rrf_k = 60` 控制头部排名的影响力：k 越大，排名差异的影响越小。

#### 2.2.4 模式补全（Pattern Completion）

当直接通道无命中时，使用离线哈希嵌入做模糊匹配：

```python
if not direct and alive:
    for nid in alive:
        sim = hash_embedder.similarity(query, node["text"])
        if sim >= 0.25:  # 阈值
            direct[nid] = sim * FUZZY_ACTIVATION * node["weight"]
```

`FUZZY_ACTIVATION = 0.5`：模糊匹配给 50% 的激活值，确保不会完全空返，但也不会让模糊匹配压过精确匹配。

#### 2.2.5 词汇语义重排 + 模式分离

```python
# 1. 对 top-k*3 候选做语义重排
for nid, base_score in ranked[:top_k * 3]:
    sim = hash_embedder.similarity(query, node["text"])
    reranked.append((nid, base_score * (1.0 + sim)))

# 2. 去除近重复结果
for nid, score in ranked:
    if not any(similarity(node_text, selected_text) >= SEPARATION_SIM for ...):
        selected.append((nid, score))
```

`SEPARATION_SIM = 0.92`：相似度超过 92% 的结果视为重复，只保留排名更高的。

### 2.3 加权概念图

#### 2.3.1 节点结构

```python
node = {
    "text": "项目使用 FastAPI 做后端",        # 事实文本
    "weight": 0.85,                           # 显著性权重 [0, 1]
    "peak_weight": 1.0,                       # 历史最高权重（衰减基准）
    "created": "2025-07-01T10:00:00",         # 创建时间
    "last_accessed": "2025-07-10T15:30:00",   # 最后访问时间
    "access_count": 3,                        # 确认次数
    "confidence": 1.0,                        # 置信度 [0, 1]
    "keys": ["fastapi", "backend", "python",  # 索引关键词
             "framework", "web"],
    "origin": {"by": "agent", "session": "s1", "via": "remember"},
    "valid_from": "2025-07-01T10:00:00",      # 有效性起始
    "valid_to": None,                          # None=仍有效
    "superseded_by": None,                     # 被哪条记忆替代
    "history": [],                             # 历史版本（correct时追加）
}
```

**ID 生成**：`md5(cleaned_text)[:12]`，内容寻址，相同文本复用同 ID。

#### 2.3.2 边结构

```python
edge = {
    "relation": "related",                    # 关系类型
    "weight": 0.75,                           # 边权重 [0, 1]
    "created": "2025-07-01T10:00:00",         # 创建时间
}
# 双向存储: edges[A][B] 和 edges[B][A] 各一份
```

**边权重演化**：
- 创建时 weight = 1.0
- `confirm` 任一端点 → weight += `EDGE_BOOST`（0.25）
- `dream` → weight × `EDGE_DECAY_PER_DREAM`（0.95）
- weight < `EDGE_PRUNE_THRESHOLD`（0.1）→ 修剪

**边来源**：
1. **同现自动创建**：两条记忆共享 ≥2 个关键词时自动建边。
2. **显式 link**：`link "A" "B" "relation"` 手动创建。
3. **correct 迁移**：纠正时旧记忆的关联边迁移到新记忆（保留 supersedes/superseded-by 边）。

### 2.4 艾宾浩斯遗忘曲线

```python
# R = e^(-t/S)，t = 距上次访问天数，S = 稳定性
stability = STABILITY_BASE_DAYS + access_count * STABILITY_PER_ACCESS
#           = 3.0              + access_count  × 14.0
retention = math.exp(-days / stability)
new_weight = peak_weight * retention
```

| 确认次数 | 稳定性 S (天) | 半衰期 (天) | 30天后保留率 |
|----------|-------------|-----------|-----------|
| 0 | 3.0 | 2.1 | 0.00% |
| 1 | 17.0 | 11.8 | 17.0% |
| 2 | 31.0 | 21.5 | 38.0% |
| 3 | 45.0 | 31.2 | 51.3% |
| 5 | 73.0 | 50.6 | 66.3% |
| 10 | 143.0 | 99.1 | 81.0% |

**关键参数**：
- `STABILITY_BASE_DAYS = 3.0`：未确认的记忆 3 天半衰期，快速遗忘。
- `STABILITY_PER_ACCESS = 14.0`：**每次确认买两周稳定性**，这是最核心的设计——频繁使用的记忆几乎不会遗忘。
- `GRACE_DAYS = 45`：45 天宽限期，此期间内不删除任何记忆（即使权重低于阈值）。
- `WEIGHT_THRESHOLD = 0.1`：低于此权重 + 确认 < 2 次 + 超过宽限期 → 修剪（归档，非删除）。

### 2.5 确定性梦境周期三阶段

#### Stage 1: Light Sleep（浅睡）

```python
def light_sleep():
    signals = read_signals()       # 读取 signals.jsonl
    count = len(signals)           # 统计本次会话的写入次数
    clear_signals()                # 清除信号文件
    return count                   # 报告，不回放
```

**设计意图**：信号是遥测数据，整合的输入是节点/边权重本身。Light sleep 只做统计和清理。

#### Stage 2: Deep Sleep（深睡）

```python
def deep_sleep():
    # 2a. 艾宾浩斯衰减
    for node in all_nodes:
        if node.valid_to:  # 已被替代的旧事实
            if closed_days > GRACE_DAYS:
                prune(node)   # 超过宽限期的已替代事实直接归档
            continue
        days = (now - node.last_accessed).days
        stability = 3.0 + node.access_count * 14.0
        retention = math.exp(-days / stability)
        node.weight = node.peak_weight * retention
        # 修剪条件：权重 < 0.1 AND 确认 < 2 AND 超过45天
        if node.weight < 0.1 and node.access_count < 2 and days > 45:
            archive_and_prune(node)

    # 2b. 突触稳态：边衰减（每天最多一次）
    if today > last_edge_decay_date:
        for edge in all_edges:
            edge.weight *= 0.95   # EDGE_DECAY_PER_DREAM
            if edge.weight < 0.1:  # EDGE_PRUNE_THRESHOLD
                remove(edge)
```

**边衰减的数学含义**：
- 每天衰减 5%，约 45 天后边权重从 1.0 衰减到 0.1（修剪阈值）。
- 但如果边的端点被 confirm，边权重 +0.25（`EDGE_BOOST`），远超一天衰减量。
- 效果：**经常使用的连接不断强化，不用的连接在 ~45 天内自然消亡**。

#### Stage 3: REM（快速眼动——类比哺乳动物的REM睡眠）

```python
def rem_sleep():
    # 3a. 聚类提升
    clusters = []
    for node in sorted_nodes:
        placed = False
        for cluster in clusters:
            if similarity(node.text, cluster.centroid) > CLUSTER_SIM:  # 0.45
                cluster.members.append(node)
                placed = True
                break
        if not placed:
            clusters.append({"centroid": node.text, "members": [node]})

    for cluster in clusters:
        if len(cluster.members) >= PROMOTION_THRESHOLD:  # >= 3
            cortex.promote(cluster.centroid, cluster.members)
            # 写入 cortex/<topic>.md 永久保存

    # 3b. 矛盾检测
    for i, (id_a, node_a) in enumerate(alive_nodes):
        for j, (id_b, node_b) in enumerate(alive_nodes[i+1:]):
            rare_shared = shared_rare_keys(node_a, node_b)
            if len(rare_shared) < 2: continue
            sim = similarity(node_a.text, node_b.text)
            if 0.35 <= sim < 0.9:  # 相似但不完全相同
                link(id_a, id_b, "possible-conflict")
                # 提示用户用 correct 解决
```

**聚类参数**：
- `CLUSTER_SIM = 0.45`：相似度 > 45% 视为同类（宽松，鼓励合并）。
- `PROMOTION_THRESHOLD = 3`：≥3 条相关记忆才提升到 Cortex。
- `SEPARATION_SIM = 0.92`：检索去重阈值（严格）。

**矛盾检测逻辑**：
- 两条记忆共享 ≥2 个稀有关键词（DF ≤ max(2, N/4)）。
- 文本相似度在 [0.35, 0.9) 区间——足够相似到可能在说同一件事，但又不完全相同，可能是矛盾。
- 标记为 `possible-conflict` 边，不自动删除，留给用户/Agent 用 `correct` 解决。

### 2.6 confirm/correct 机制

#### confirm（确认强化）

```python
def confirm(node_ids):
    for nid in node_ids:
        node = nodes[nid]
        node.access_count += 1
        node.weight = min(1.0, node.weight + BOOST_PER_ACCESS)  # +0.15
        node.peak_weight = max(node.peak_weight, node.weight)
        node.last_accessed = now
        # 强化所有关联边
        for neighbor, edge in edges[nid].items():
            edge.weight = min(1.0, edge.weight + EDGE_BOOST)  # +0.25
            reverse_edge.weight = edge.weight  # 双向同步
```

**效果**：一次 confirm 让节点稳定性增加 14 天，边权重增加 0.25。这比衰减快得多——一次 confirm 抵消约 5 天的边衰减（0.25 / (1 - 0.95) ≈ 5 天的衰减量才能回到原值）。

#### correct（纠正超驰）

```python
def correct(old_hint, new_text):
    # 1. 找到最匹配旧提示的节点
    old_node = recall(old_hint, top_k=1)[0]
    
    # 2. 验证匹配质量：至少共享2个内容token，或覆盖短提示的50%
    shared = content_tokens(old_hint) & content_tokens(old_node.text)
    if len(shared) < 2 and len(shared) / len(hint_tokens) < 0.5:
        return None  # 拒绝：匹配不够
    
    # 3. 创建新节点（继承旧节点的连接，但不继承 supersedes 边）
    new_node = create_node(
        text=new_text,
        weight=old_node.weight,       # 继承权重
        confidence=old_node.confidence * 0.7,  # 降级置信度
        history=old_node.history + [  # 保留历史
            {"text": old_node.text, "replaced": now}
        ],
        origin={"via": "correct"}
    )
    
    # 4. 迁移旧节点的知识连接到新节点
    for neighbor, edge in old_node.edges.items():
        if edge.relation not in ("supersedes", "superseded-by"):
            new_node.edges[neighbor] = copy(edge)
            neighbor.edges[new_node] = copy(edge)
    
    # 5. 建立 supersedes 双向边
    new_node.edges[old_node] = {relation: "supersedes", weight: 0.5}
    old_node.edges[new_node] = {relation: "superseded-by", weight: 0.5}
    
    # 6. 关闭旧节点（不删除）
    old_node.valid_to = now
    old_node.superseded_by = new_node.id
```

**关键设计**：
- **融合而非擦除**：旧记忆标记为"已替代"（`valid_to` 设值），但保留在图中供 `why` 和 `recall --at` 查询。
- **溯源链**：`history` 字段记录完整的替换历史，`supersedes` 边保持可达性。
- **置信度降级**：新记忆的 confidence = old × 0.7，需要后续 confirm 恢复。
- **连接迁移**：旧记忆的知识连接迁移到新记忆，但 supersedes 边不迁移（防止混淆溯源链）。

### 2.7 provenance 日志

```python
# journal.jsonl：追加写入，永不删除
{"ts": "2025-07-01T10:00:00", "op": "remember", "by": "agent", "id": "a1b2c3d4e5f6", "text": "..."}
{"ts": "2025-07-02T15:00:00", "op": "confirm", "by": "agent", "ids": ["a1b2c3d4e5f6"]}
{"ts": "2025-07-03T09:00:00", "op": "correct", "by": "agent", "old_id": "a1b2c3d4e5f6", "new_id": "f6e5d4c3b2a1", "old_text": "...", "new_text": "..."}
{"ts": "2025-07-04T03:00:00", "op": "prune", "by": "system", "ids": ["..."], "texts": ["..."]}
```

`why <id>` 命令可查询任意记忆的完整历史：从创建、确认、纠正到可能的修剪，全部可追溯。

### 2.8 相关术语自动发现（RelatedTerms）

```python
class RelatedTerms:
    """基于同现的自动术语关联 + 2-hop PageRank"""
    
    def _build(corpus):
        # 对每条记忆提取 top-12 术语
        # 统计术语对在同一记忆中出现的频率（同现矩阵）
        # 计算文档频率 df
    
    def related(word, top_k=5, max_hops=2, damping=0.55):
        # Hop 1: 直接 Ochiai 系数 = cooc / sqrt(df_a * df_b)
        # Hop 2: 通过共享邻居扩散相似性
        # 结果：即使 A 和 C 从未同现，也能通过 B 发现 A~C 的关联
```

**用途**：
1. **key 扩展**：`_extract_keys` 中，稀有条目的关联术语被添加为额外 key，扩大检索命中面。
2. **模糊匹配**：`_fuzzy` 方法用编辑距离匹配未知词（拼写错误、变体）。

---

## 3. 差距对比表

| 维度 | xiaoda-agent v0.5.03 | mind v6.2.8 | 差距等级 |
|------|----------------------|-------------|---------|
| **记忆架构** | 单层（FlatMemory） | 三层（Working→Hippocampus→Cortex） | 🔴 重大 |
| **检索方式** | 向量 top-k + FTS5 | 扩散激活 + IDF + RRF + 模式补全 | 🔴 重大 |
| **概念图** | 无 | 加权有向图（nodes + edges） | 🔴 重大 |
| **边权重演化** | 无 | confirm 强化 + dream 衰减 + 修剪 | 🟡 中等 |
| **遗忘曲线** | 简单指数衰减 | Ebbinghaus R=e^(-t/S)，S=3+14×access | 🟡 中等 |
| **Boost 机制** | MAX_BOOST=0.3 硬上限 | BOOST_PER_ACCESS=0.15 无上限（cap 1.0） | 🟡 中等 |
| **梦境周期** | 衰减+归档（1阶段） | Light→Deep→REM（3阶段） | 🔴 重大 |
| **聚类提升** | 无 | CLUSTER_SIM=0.45, ≥3节点→Cortex | 🟡 中等 |
| **矛盾检测** | 无 | REM 阶段扫描 possible-conflict | 🟢 轻微 |
| **confirm/correct** | 无 | confirm 强化 + correct 超驰+溯源 | 🟡 中等 |
| **provenance** | 无 | journal.jsonl 永久溯源 | 🟢 轻微 |
| **自动梦境触发** | 无 | 信号≥10 或 >24h 自动触发 | 🟡 中等 |
| **身份查询** | 无特殊处理 | PRONOUN_FALLBACK + IDENTITY_KEYS | 🟢 轻微 |
| **并发安全** | 无保护 | 文件锁 + read-merge-write | 🟡 中等 |

---

## 4. 优化方案

### 4.1 引入概念图层（在现有 SQLite 基础上增加 node/edge 表）

#### 4.1.1 数据库 Schema 扩展

在现有 SQLite 数据库中新增两张表，与现有 `memories` 表共存：

```sql
-- 概念节点表（对应 mind 的 Hippocampus.nodes）
CREATE TABLE IF NOT EXISTS concept_nodes (
    id          TEXT PRIMARY KEY,              -- md5(cleaned_text)[:12]
    text        TEXT NOT NULL,                 -- 事实文本
    weight      REAL NOT NULL DEFAULT 1.0,     -- 显著性权重 [0, 1]
    peak_weight REAL NOT NULL DEFAULT 1.0,     -- 历史最高权重
    confidence  REAL NOT NULL DEFAULT 1.0,     -- 置信度
    access_count INTEGER NOT NULL DEFAULT 0,   -- 确认次数
    keys        TEXT NOT NULL DEFAULT '[]',    -- JSON: 索引关键词列表
    layer       TEXT NOT NULL DEFAULT 'hippocampus',  -- hippocampus | cortex
    created     TEXT NOT NULL,                 -- ISO timestamp
    last_accessed TEXT NOT NULL,               -- ISO timestamp
    valid_from  TEXT NOT NULL,                 -- 有效性起始
    valid_to    TEXT,                          -- NULL=仍有效
    superseded_by TEXT,                        -- 被替代者 ID
    history     TEXT NOT NULL DEFAULT '[]',    -- JSON: 历史版本
    origin      TEXT NOT NULL DEFAULT '{}',    -- JSON: 来源信息
    embedding   BLOB                           -- 向量嵌入（兼容现有 sqlite-vec）
);

-- 概念边表（对应 mind 的 Hippocampus.edges）
CREATE TABLE IF NOT EXISTS concept_edges (
    source_id   TEXT NOT NULL,                 -- 起始节点 ID
    target_id   TEXT NOT NULL,                 -- 目标节点 ID
    relation    TEXT NOT NULL DEFAULT 'related', -- 关系类型
    weight      REAL NOT NULL DEFAULT 1.0,     -- 边权重 [0, 1]
    created     TEXT NOT NULL,                 -- 创建时间
    PRIMARY KEY (source_id, target_id),
    FOREIGN KEY (source_id) REFERENCES concept_nodes(id),
    FOREIGN KEY (target_id) REFERENCES concept_nodes(id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_node_keys ON concept_nodes(keys);
CREATE INDEX IF NOT EXISTS idx_node_layer ON concept_nodes(layer);
CREATE INDEX IF NOT EXISTS idx_node_weight ON concept_nodes(weight);
CREATE INDEX IF NOT EXISTS idx_edge_source ON concept_edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edge_target ON concept_edges(target_id);

-- 元数据表（梦境衰减标记等）
CREATE TABLE IF NOT EXISTS concept_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
```

#### 4.1.2 数据迁移策略

```python
def migrate_memories_to_concept_nodes():
    """将现有 memories 表的数据迁移到 concept_nodes"""
    for mem in db.query("SELECT * FROM memories"):
        keys = extract_keys(mem.content)  # 复用 jieba + 新增 key 提取逻辑
        node_id = hashlib.md5(mem.content.encode('utf-8')).hexdigest()[:12]
        db.insert("concept_nodes", {
            "id": node_id,
            "text": mem.content,
            "weight": mem.fluid_score if hasattr(mem, 'fluid_score') else 1.0,
            "peak_weight": 1.0,
            "keys": json.dumps(keys),
            "layer": "hippocampus",
            # ... 其他字段用合理默认值填充
        })
        # 保留原 memories 表，不删除，作为回退
```

### 4.2 扩散激活检索实现

#### 4.2.1 新增 `memory/spreading_activation.py`

```python
"""
扩散激活检索引擎
替代纯向量 top-k，实现 mind 风格的三通道融合检索
"""
import math
import hashlib
import json
from collections import defaultdict
from typing import List, Tuple, Dict, Optional

# ──────────────────────────── 参数 ────────────────────────────
RECALL_RADIUS = 3           # 最大扩散跳数
RECALL_TOP_K = 5            # 返回结果数
ACTIVATION_DECAY = 0.5      # 每跳激活衰减系数
SPREADING_THRESHOLD = 0.05  # 低于此值不继续传播
FUZZY_ACTIVATION = 0.5      # 模糊匹配激活值
SEPARATION_SIM = 0.92       # 去重相似度阈值
CLUSTER_SIM = 0.45          # 聚类相似度阈值
RRF_K = 60                  # RRF 平滑参数


class SpreadingActivationEngine:
    """扩散激活检索引擎"""

    def __init__(self, db_conn, embedder, key_extractor):
        self.db = db_conn
        self.embedder = embedder        # 兼容现有 vector_store 的嵌入器
        self.key_extractor = key_extractor  # 关键词提取器

    def recall(self, query: str, top_k: int = RECALL_TOP_K,
               max_hops: int = RECALL_RADIUS,
               at: Optional[str] = None) -> List[Tuple[str, float, dict]]:
        """
        扩散激活检索主入口
        
        Returns:
            [(node_id, score, node_data), ...]
        """
        # Step 1: 提取查询 keys
        query_keys = set(self.key_extractor.extract(query, is_query=True))
        if not query_keys:
            return []

        # Step 2: 扩展 keys（同现关系）
        expanded_keys = self._expand_keys(query_keys)

        # Step 3: 获取存活节点
        alive_nodes = self._get_alive_nodes(at)
        if not alive_nodes:
            return []
        N = max(1, len(alive_nodes))

        # Step 4: 计算 IDF
        idf = self._compute_idf(expanded_keys, alive_nodes, N)

        # Step 5: 通道一 - 直接命中
        direct = self._direct_channel(expanded_keys, idf, alive_nodes, query)

        # Step 6: 模式补全（无直接命中时）
        if not direct:
            direct = self._pattern_completion(query, alive_nodes)
        if not direct:
            return []

        # Step 7: 通道二 - 扩散激活
        spread = self._spreading_channel(direct, alive_nodes, max_hops)

        # Step 8: RRF 融合
        fused = self._rrf_fusion(direct, spread)

        # Step 9: 语义重排
        fused = self._semantic_rerank(query, fused, top_k)

        # Step 10: 模式分离（去重）
        results = self._pattern_separation(fused, top_k)

        return results

    def _expand_keys(self, keys: set) -> set:
        """扩展查询 keys：稀有条目添加同现关联术语"""
        expanded = set(keys)
        for key in list(keys):
            # 只扩展稀有条目（df < 2）
            df = self._get_df(key)
            if df >= 2:
                continue
            related = self._get_related_terms(key, top_k=4)
            for term, score in related:
                if score >= 0.15 and term not in IDENTITY_KEYS:
                    expanded.add(term)
        return expanded

    def _direct_channel(self, keys: set, idf: dict,
                        alive_nodes: dict, query: str) -> dict:
        """
        直接命中通道：IDF 加权 key 重叠 + 子串包含
        
        伪代码:
        FOR each alive_node:
            shared_keys = keys ∩ node.keys
            IF shared_keys:
                idf_score = Σ idf[k] for k in shared_keys
                weight_bias = 0.35 + 0.65 × node.weight
                direct[nid] = idf_score × weight_bias
            END IF
            substr_count = count(query_tokens in node.text)
            reverse_count = count(node.keys in query)
            IF substr_count + reverse_count > 0:
                direct[nid] += (substr_count + reverse_count) × 0.6 × weight_bias
            END IF
        END FOR
        """
        direct = {}
        q_lower = query.lower()
        for nid, node in alive_nodes.items():
            node_keys = set(node.get("keys", []))
            w_bias = 0.35 + 0.65 * node.get("weight", 1.0)
            
            shared = keys & node_keys
            if shared:
                idf_score = sum(idf.get(k, 0) for k in shared)
                direct[nid] = direct.get(nid, 0) + idf_score * w_bias
            
            # 子串包含
            n_text = node["text"].lower()
            substr = sum(1 for w in keys if len(w) >= 4 and w in n_text)
            reverse = sum(1 for k in node_keys if len(k) >= 4 and k in q_lower)
            if substr + reverse:
                direct[nid] = direct.get(nid, 0) + (substr + reverse) * 0.6 * w_bias
        
        return direct

    def _spreading_channel(self, direct: dict,
                           alive_nodes: dict,
                           max_hops: int) -> dict:
        """
        扩散激活通道：从种子节点沿边传播激活值
        
        伪代码:
        spread = {}  // 累积激活值
        wave = direct  // 初始波 = 直接命中节点及其分数
        
        FOR hop = 0 TO max_hops:
            nxt = {}
            FOR (nid, activation) IN wave:
                spread[nid] += activation  // 累积
                IF hop < max_hops AND activation > SPREADING_THRESHOLD:
                    FOR (neighbor, edge) IN get_edges(nid):
                        IF neighbor NOT IN alive: CONTINUE
                        propagated = activation × ACTIVATION_DECAY × edge.weight / (hop + 1)
                        nxt[neighbor] += propagated
                    END FOR
                END IF
            END FOR
            wave = nxt
            IF wave IS EMPTY: BREAK
        END FOR
        
        RETURN spread
        """
        spread = defaultdict(float)
        wave = dict(direct)
        
        for hop in range(max_hops + 1):
            nxt = defaultdict(float)
            for nid, act in wave.items():
                spread[nid] += act
                if hop < max_hops and act > SPREADING_THRESHOLD:
                    edges = self._get_edges(nid)
                    for neighbor, edge in edges.items():
                        if neighbor not in alive_nodes:
                            continue
                        propagated = (act * ACTIVATION_DECAY
                                      * edge["weight"] / (hop + 1))
                        nxt[neighbor] += propagated
            wave = nxt
            if not wave:
                break
        
        return dict(spread)

    def _rrf_fusion(self, direct: dict, spread: dict) -> dict:
        """
        Reciprocal Rank Fusion：双通道排名融合
        
        伪代码:
        dr = rank(direct, descending)    // 直接通道排名
        sr = rank(spread, descending)    // 扩散通道排名
        dr_default = len(dr) + 1
        sr_default = len(sr) + 1
        
        fused = {}
        FOR nid IN (direct.keys ∪ spread.keys):
            fused[nid] = 1/(RRF_K + dr[nid, dr_default]) 
                       + 1/(RRF_K + sr[nid, sr_default])
        END FOR
        
        RETURN fused
        """
        dr = {n: i for i, (n, _) in enumerate(
            sorted(direct.items(), key=lambda x: (-x[1], x[0])))}
        sr = {n: i for i, (n, _) in enumerate(
            sorted(spread.items(), key=lambda x: (-x[1], x[0])))}
        dr_default = len(dr) + 1
        sr_default = len(sr) + 1
        
        fused = {}
        for nid in set(direct) | set(spread):
            fused[nid] = (1.0 / (RRF_K + dr.get(nid, dr_default)) +
                          1.0 / (RRF_K + sr.get(nid, sr_default)))
        return fused

    def _semantic_rerank(self, query: str, fused: dict,
                         top_k: int) -> dict:
        """词汇语义重排：对 head 候选做嵌入相似度加权"""
        ranked = sorted(fused.items(), key=lambda x: (-x[1], x[0]))
        reranked = []
        for nid, base in ranked[:top_k * 3]:
            node = self._get_node(nid)
            if node is None:
                continue
            sim = self.embedder.similarity(query, node["text"])
            reranked.append((nid, base * (1.0 + sim)))
        reranked.sort(key=lambda x: (-x[1], x[0]))
        return {nid: score for nid, score in reranked}

    def _pattern_separation(self, fused: dict,
                            top_k: int) -> List[Tuple[str, float, dict]]:
        """
        模式分离：去除近重复结果
        
        伪代码:
        selected = []
        FOR (nid, score) IN sorted(fused, by score DESC):
            is_dup = False
            FOR (sel_nid, _) IN selected:
                IF similarity(node[nid].text, node[sel_nid].text) >= SEPARATION_SIM:
                    is_dup = True
                    BREAK
                END IF
            END FOR
            IF NOT is_dup:
                selected.append((nid, score))
            END IF
            IF len(selected) >= top_k: BREAK
        END FOR
        
        RETURN [(nid, score, node_data), ...]
        """
        ranked = sorted(fused.items(), key=lambda x: (-x[1], x[0]))
        selected = []
        for nid, score in ranked:
            node = self._get_node(nid)
            if node is None:
                continue
            is_dup = False
            for sel_nid, _ in selected:
                sel_node = self._get_node(sel_nid)
                if sel_node and self.embedder.similarity(
                        node["text"], sel_node["text"]) >= SEPARATION_SIM:
                    is_dup = True
                    break
            if not is_dup:
                selected.append((nid, score, node))
            if len(selected) >= top_k:
                break
        return selected

    def _pattern_completion(self, query: str,
                            alive_nodes: dict) -> dict:
        """模式补全：无直接命中时，用嵌入做模糊匹配"""
        direct = {}
        for nid, node in alive_nodes.items():
            sim = self.embedder.similarity(query, node["text"])
            if sim >= 0.25:
                direct[nid] = sim * FUZZY_ACTIVATION * node.get("weight", 1.0)
        return direct

    # ──────── 辅助方法 ────────
    def _get_alive_nodes(self, at=None):
        """获取当前有效的节点（valid_to IS NULL 或在 at 之前）"""
        if at:
            rows = self.db.query(
                "SELECT * FROM concept_nodes WHERE valid_from <= ? AND "
                "(valid_to IS NULL OR valid_to > ?)", (at, at))
        else:
            rows = self.db.query(
                "SELECT * FROM concept_nodes WHERE valid_to IS NULL")
        return {r["id"]: dict(r) for r in rows}

    def _get_edges(self, node_id: str) -> dict:
        """获取节点的所有出边"""
        rows = self.db.query(
            "SELECT target_id, relation, weight FROM concept_edges "
            "WHERE source_id = ?", (node_id,))
        return {r["target_id"]: {"relation": r["relation"],
                                  "weight": r["weight"]} for r in rows}

    def _compute_idf(self, keys: set, alive_nodes: dict, N: int) -> dict:
        """计算 IDF 值"""
        df = defaultdict(int)
        for nid, node in alive_nodes.items():
            for k in set(node.get("keys", [])):
                df[k] += 1
        return {k: math.log(1 + N / (1 + df.get(k, 0))) for k in keys}

    def _get_node(self, nid: str) -> Optional[dict]:
        row = self.db.query_one(
            "SELECT * FROM concept_nodes WHERE id = ?", (nid,))
        return dict(row) if row else None

    def _get_df(self, term: str) -> int:
        """获取术语文档频率"""
        # 扫描所有节点的 keys JSON
        count = 0
        rows = self.db.query("SELECT keys FROM concept_nodes")
        for r in rows:
            keys = json.loads(r["keys"])
            if term in keys:
                count += 1
        return count

    def _get_related_terms(self, term: str, top_k: int = 4) -> list:
        """获取相关术语（基于同现矩阵或简单统计）"""
        # 简化实现：基于共享节点的同现
        # 后续可替换为 RelatedTerms 类
        return []
```

### 4.3 三层记忆架构

#### 4.3.1 Layer 1: Working Memory

```python
# memory/working_memory.py
class WorkingMemory:
    """
    工作记忆层：始终注入 Agent 上下文的热记忆
    对应 mind 的 ACTIVE.md
    """
    TOKEN_BUDGET = 800  # ~200 token，按字符计

    def generate(self, agent_context: str = "") -> str:
        """
        从 Hippocampus 取最热的 N 条记忆，生成工作记忆文本
        
        排序优先级:
        1. weight (显著性)
        2. access_count (确认次数)
        3. last_accessed (最近访问)
        4. id (确定性排序)
        """
        nodes = self._get_top_nodes()
        hot = []
        used = 0
        for node in nodes:
            line = f"- [fact] {node['text']}"
            if used + len(line) > self.TOKEN_BUDGET:
                continue
            hot.append(line)
            used += len(line)
            if len(hot) >= 8:
                break
        
        cortex_files = self._get_cortex_index()
        return self._render(hot, cortex_files)

    def _get_top_nodes(self) -> list:
        """按 weight+access_count+last_accessed 排序取 top 节点"""
        rows = self.db.query("""
            SELECT * FROM concept_nodes 
            WHERE valid_to IS NULL AND layer = 'hippocampus'
            ORDER BY weight DESC, access_count DESC, last_accessed DESC
            LIMIT 20
        """)
        return [dict(r) for r in rows]
```

#### 4.3.2 Layer 2: Hippocampus

```python
# memory/hippocampus.py — 概念图管理
class Hippocampus:
    """
    海马体层：加权概念图
    存储：SQLite concept_nodes + concept_edges 表
    """
    
    def remember(self, text: str, confidence: float = 1.0) -> str:
        """存储记忆到概念图"""
        cleaned = self._clean_text(text)
        node_id = hashlib.md5(cleaned.encode('utf-8')).hexdigest()[:12]
        keys = self.key_extractor.extract(cleaned, is_query=False)
        now = datetime.now().isoformat()
        
        if node_id in self._get_existing_ids():
            # 已存在：强化
            self._reinforce_existing(node_id)
        else:
            # 新建
            self.db.insert("concept_nodes", {
                "id": node_id,
                "text": cleaned,
                "weight": 1.0,
                "peak_weight": 1.0,
                "confidence": confidence,
                "access_count": 0,
                "keys": json.dumps(keys),
                "layer": "hippocampus",
                "created": now,
                "last_accessed": now,
                "valid_from": now,
                "valid_to": None,
                "superseded_by": None,
                "history": "[]",
                "origin": json.dumps({"via": "remember"}),
                "embedding": self._get_embedding(cleaned),
            })
            # 自动创建同现边
            self._auto_link(node_id, keys)
        
        self._save_journal("remember", id=node_id, text=cleaned)
        return node_id

    def _auto_link(self, node_id: str, node_keys: list):
        """
        自动创建同现边：与共享 ≥2 个 key 的现有节点建边
        
        伪代码:
        FOR each existing_node IN all_nodes:
            shared = node_keys ∩ existing_node.keys
            IF len(shared) >= 2:
                create_edge(node_id, existing_node.id, 
                           relation="co-occurrence", weight=1.0)
            END IF
        END FOR
        """
        existing = self.db.query(
            "SELECT id, keys FROM concept_nodes WHERE id != ?", (node_id,))
        for row in existing:
            existing_keys = set(json.loads(row["keys"]))
            shared = set(node_keys) & existing_keys
            if len(shared) >= 2:
                self._create_edge(node_id, row["id"], "co-occurrence", 1.0)
```

#### 4.3.3 Layer 3: Cortex

```python
# memory/cortex.py — 长期巩固层
class Cortex:
    """
    皮层层：聚类提升后的巩固知识
    存储：SQLite concept_nodes (layer='cortex') + 独立 Markdown 文件
    """
    
    def promote(self, topic: str, member_ids: list):
        """
        将聚类结果提升到皮层
        
        1. 创建皮层节点 (layer='cortex')
        2. 将成员节点的关键信息整合到皮层节点
        3. 建立皮层节点与成员节点的 "consolidates" 边
        4. 输出 Markdown 文件到 cortex/ 目录
        """
        members = [self._get_node(mid) for mid in member_ids]
        content = "\n".join(f"- {m['text']}" for m in members[:5])
        
        cortex_id = hashlib.md5(topic.encode('utf-8')).hexdigest()[:12]
        self.db.insert("concept_nodes", {
            "id": cortex_id,
            "text": content,
            "layer": "cortex",
            "weight": 1.0,
            # ... 其他字段
        })
        
        for mid in member_ids:
            self._create_edge(cortex_id, mid, "consolidates", 0.5)
```

### 4.4 确定性梦境周期三阶段

#### 4.4.1 梦境引擎

```python
# core/dream_engine.py
class DreamEngine:
    """
    确定性梦境周期：Light → Deep → REM
    模拟哺乳动物睡眠的三阶段整合过程
    """

    def __init__(self, db_conn, hippocampus, cortex, embedder):
        self.db = db_conn
        self.hippo = hippocampus
        self.cortex = cortex
        self.embedder = embedder

    def dream(self, dry_run: bool = False) -> dict:
        """
        执行完整的梦境周期
        
        Returns:
            {
                "stage_light": {"signals_read": int},
                "stage_deep": {"nodes_pruned": int, "edges_pruned": int},
                "stage_rem": {"clusters_promoted": int, "conflicts_found": int},
            }
        """
        report = {}

        # ──── Stage 1: Light Sleep ────
        report["stage_light"] = self._light_sleep(dry_run)

        # ──── Stage 2: Deep Sleep ────
        report["stage_deep"] = self._deep_sleep(dry_run)

        # ──── Stage 3: REM ────
        report["stage_rem"] = self._rem_sleep(dry_run)

        # 更新 Working Memory
        if not dry_run:
            self._update_working_memory()

        return report

    # ═══════════════════════════════════════════════════════════
    # Stage 1: Light Sleep — 信号收集与清理
    # ═══════════════════════════════════════════════════════════
    def _light_sleep(self, dry_run: bool) -> dict:
        """
        Light Sleep 逻辑:
        1. 读取 signals.jsonl（本次会话的写入遥测）
        2. 统计信号数量
        3. 清除信号文件
        
        设计意图:
        - 信号是遥测数据，整合的输入是节点/边权重本身
        - Light sleep 只做统计和清理，不对记忆做任何修改
        """
        signals = self._read_signals()
        if not dry_run:
            self._clear_signals()
        return {"signals_read": len(signals)}

    # ═══════════════════════════════════════════════════════════
    # Stage 2: Deep Sleep — Ebbinghaus 衰减 + 突触稳态
    # ═══════════════════════════════════════════════════════════
    def _deep_sleep(self, dry_run: bool) -> dict:
        """
        Deep Sleep 逻辑:
        
        2a. 艾宾浩斯衰减:
            FOR each node IN all_nodes:
                IF node.valid_to IS NOT NULL:  // 已被替代的旧事实
                    closed_days = (now - node.valid_to).days
                    IF closed_days > GRACE_DAYS:  // 45天宽限期
                        archive(node)  // 归档，非删除
                    END IF
                    CONTINUE  // 已替代的节点不做常规衰减
                END IF
                
                days = max(0, (now - node.last_accessed).days)
                stability = STABILITY_BASE_DAYS + node.access_count × STABILITY_PER_ACCESS
                // stability = 3.0 + access_count × 14.0
                retention = exp(-days / stability)
                new_weight = node.peak_weight × retention
                new_weight = clamp(new_weight, 0.0, 1.0)
                
                IF NOT dry_run:
                    node.weight = new_weight
                END IF
                
                // 修剪条件：权重 < 阈值 AND 确认 < 2 AND 超过宽限期
                IF new_weight < WEIGHT_THRESHOLD 
                   AND node.access_count < 2 
                   AND days > GRACE_DAYS:
                    pruned.append(node)
                END IF
            END FOR
            
            // 归档被修剪的节点（归档，非删除！）
            archive(pruned)
        
        2b. 突触稳态（边衰减，每天最多一次）:
            today = now.date()
            last_decay = get_meta("last_edge_decay")
            IF last_decay >= today: RETURN  // 今天已衰减过
            
            pruned_edges = 0
            FOR each edge IN all_edges:
                edge.weight = edge.weight × EDGE_DECAY_PER_DREAM  // ×0.95
                edge.weight = round(edge.weight, 4)
                IF edge.weight < EDGE_PRUNE_THRESHOLD:  // <0.1
                    remove(edge)
                    pruned_edges += 1
                END IF
            END FOR
            
            set_meta("last_edge_decay", today)
        """
        now = datetime.now()
        pruned_nodes = []
        
        # 2a. 艾宾浩斯衰减
        nodes = self.db.query(
            "SELECT * FROM concept_nodes WHERE valid_to IS NULL")
        for row in nodes:
            node = dict(row)
            days = max(0, (now - datetime.fromisoformat(
                node["last_accessed"])).days)
            stability = (STABILITY_BASE_DAYS 
                         + node["access_count"] * STABILITY_PER_ACCESS)
            retention = math.exp(-days / stability)
            new_weight = max(0.0, min(1.0, 
                            node["peak_weight"] * retention))
            
            if not dry_run:
                self.db.execute(
                    "UPDATE concept_nodes SET weight = ? WHERE id = ?",
                    (new_weight, node["id"]))
            
            if (new_weight < WEIGHT_THRESHOLD 
                    and node["access_count"] < 2 
                    and days > GRACE_DAYS):
                pruned_nodes.append(node)
        
        # 归档修剪的节点
        if pruned_nodes and not dry_run:
            self._archive_nodes(pruned_nodes)
            for node in pruned_nodes:
                self._remove_node_and_edges(node["id"])
        
        # 2b. 突触稳态
        today = str(now.date())
        last_decay = self._get_meta("last_edge_decay")
        pruned_edges = 0
        
        if last_decay < today:
            edges = self.db.query("SELECT * FROM concept_edges")
            for edge in edges:
                new_w = round(edge["weight"] * EDGE_DECAY_PER_DREAM, 4)
                if new_w < EDGE_PRUNE_THRESHOLD:
                    if not dry_run:
                        self.db.execute(
                            "DELETE FROM concept_edges "
                            "WHERE source_id = ? AND target_id = ?",
                            (edge["source_id"], edge["target_id"]))
                    pruned_edges += 1
                elif not dry_run:
                    self.db.execute(
                        "UPDATE concept_edges SET weight = ? "
                        "WHERE source_id = ? AND target_id = ?",
                        (new_w, edge["source_id"], edge["target_id"]))
            
            if not dry_run:
                self._set_meta("last_edge_decay", today)
        
        return {
            "nodes_pruned": len(pruned_nodes),
            "edges_pruned": pruned_edges,
        }

    # ═══════════════════════════════════════════════════════════
    # Stage 3: REM — 聚类提升 + 矛盾检测
    # ═══════════════════════════════════════════════════════════
    def _rem_sleep(self, dry_run: bool) -> dict:
        """
        REM Sleep 逻辑:
        
        3a. 聚类提升:
            clusters = []
            FOR each node IN sorted_alive_nodes:
                placed = False
                FOR cluster IN clusters:
                    IF similarity(node.text, cluster.centroid) > CLUSTER_SIM:
                        cluster.members.append(node)
                        placed = True
                        BREAK
                    END IF
                END FOR
                IF NOT placed:
                    clusters.append({centroid: node.text, members: [node]})
                END IF
            END FOR
            
            promoted = []
            FOR cluster IN clusters:
                IF len(cluster.members) >= PROMOTION_THRESHOLD:  // >=3
                    topic = cluster.centroid[:50]
                    cortex.promote(topic, cluster.members)
                    promoted.append(topic)
                END IF
            END FOR
        
        3b. 矛盾检测:
            conflicts = []
            alive = [n for n in nodes if valid_at(n)]
            N = len(alive)
            
            FOR i = 0 TO len(alive)-1:
                FOR j = i+1 TO len(alive)-1:
                    // 共享稀有关键词
                    rare_shared = [k for k in shared_keys(a, b) 
                                   if df[k] <= max(2, N//4)]
                    IF len(rare_shared) < 2: CONTINUE
                    
                    sim = similarity(a.text, b.text)
                    IF 0.35 <= sim < 0.9:
                        // 可能矛盾：相似但不完全相同
                        link(a.id, b.id, "possible-conflict", weight=0.5)
                        conflicts.append((a.id, b.id))
                    END IF
                END FOR
            END FOR
        """
        # 3a. 聚类提升
        nodes = self.db.query(
            "SELECT * FROM concept_nodes WHERE valid_to IS NULL "
            "ORDER BY id")
        clusters = []
        
        for row in nodes:
            node = dict(row)
            placed = False
            for cluster in clusters:
                sim = self.embedder.similarity(
                    node["text"], cluster["centroid"])
                if sim > CLUSTER_SIM:
                    cluster["members"].append(node)
                    placed = True
                    break
            if not placed:
                clusters.append({
                    "centroid": node["text"],
                    "members": [node],
                })
        
        promoted = []
        for cluster in clusters:
            if len(cluster["members"]) >= PROMOTION_THRESHOLD:
                topic = cluster["centroid"][:50]
                if not dry_run:
                    self.cortex.promote(topic, 
                                        [m["id"] for m in cluster["members"]])
                promoted.append(topic)
        
        # 3b. 矛盾检测
        conflicts = self._detect_conflicts(nodes, dry_run)
        
        return {
            "clusters_promoted": len(promoted),
            "conflicts_found": len(conflicts),
        }

    def _detect_conflicts(self, nodes, dry_run: bool) -> list:
        """
        矛盾检测：两条记忆共享稀有关键词且文本相似但不完全相同
        
        条件:
        1. 共享 ≥2 个稀有关键词 (df ≤ max(2, N/4))
        2. 文本相似度在 [0.35, 0.9) 区间
        3. 两条记忆都是当前有效的
        
        处理:
        - 创建 "possible-conflict" 边，weight=0.5
        - 不自动删除，等待用户/Agent 用 correct 解决
        """
        alive = [dict(r) for r in nodes 
                 if r["valid_to"] is None]
        N = max(1, len(alive))
        
        # 计算 df
        df = defaultdict(int)
        node_keys_map = {}
        for node in alive:
            keys = set(json.loads(node.get("keys", "[]")))
            node_keys_map[node["id"]] = keys
            for k in keys:
                df[k] += 1
        
        conflicts = []
        for i in range(len(alive)):
            for j in range(i + 1, len(alive)):
                a, b = alive[i], alive[j]
                a_keys = node_keys_map.get(a["id"], set())
                b_keys = node_keys_map.get(b["id"], set())
                
                rare_shared = [k for k in a_keys & b_keys
                               if df[k] <= max(2, N // 4)]
                if len(rare_shared) < 2:
                    continue
                
                sim = self.embedder.similarity(a["text"], b["text"])
                if 0.35 <= sim < 0.9:
                    if not dry_run:
                        now_iso = datetime.now().isoformat()
                        self._create_edge(a["id"], b["id"],
                                          "possible-conflict", 0.5, now_iso)
                    conflicts.append((a["id"], b["id"]))
        
        return conflicts
```

#### 4.4.2 自动梦境触发

```python
# core/dream_trigger.py
class DreamTrigger:
    """
    自动梦境触发机制（类似 git gc --auto）
    
    触发条件（满足任一）:
    1. 累积写入信号 ≥ AUTO_DREAM_SIGNALS (10)
    2. 距上次梦境 > AUTO_DREAM_HOURS (24小时)
    
    Kill switch: XIAODA_AUTO_DREAM=0 禁用
    """

    AUTO_DREAM_SIGNALS = 10
    AUTO_DREAM_HOURS = 24

    def should_trigger(self) -> bool:
        if os.environ.get("XIAODA_AUTO_DREAM", "1").lower() in ("0", "false"):
            return False
        
        pending = self._count_pending_signals()
        if pending >= self.AUTO_DREAM_SIGNALS:
            return True
        
        last_dream = self._get_last_dream_time()
        if last_dream is None:
            return pending > 0
        hours_since = (datetime.now() - last_dream).total_seconds() / 3600
        if hours_since >= self.AUTO_DREAM_HOURS and pending > 0:
            return True
        
        return False
```

### 4.5 confirm/correct 机制

#### 4.5.1 confirm（确认强化）

```python
# memory/confirm_correct.py
class ConfirmCorrect:
    """
    confirm: 用户确认记忆有用 → 强化节点 + 边权重
    correct: 用户纠正错误记忆 → 超驰旧事实 + 保留溯源链
    """

    BOOST_PER_ACCESS = 0.15    # 每次确认的权重增量
    EDGE_BOOST = 0.25          # 确认时边权重增量
    STABILITY_PER_ACCESS = 14  # 每次确认增加的稳定性天数

    def confirm(self, node_ids: list) -> dict:
        """
        确认强化
        
        效果:
        1. access_count += 1
        2. weight = min(1.0, weight + BOOST_PER_ACCESS)
        3. peak_weight = max(peak_weight, weight)
        4. last_accessed = now
        5. 所有关联边 weight += EDGE_BOOST (双向)
        
        Returns:
            {"reinforced": int, "unknown": int}
        """
        now = datetime.now().isoformat()
        reinforced = 0
        unknown = 0
        
        for nid in node_ids:
            node = self._get_node(nid)
            if node is None:
                unknown += 1
                continue
            
            # 强化节点
            new_access = node["access_count"] + 1
            new_weight = min(1.0, node["weight"] + self.BOOST_PER_ACCESS)
            new_peak = max(node["peak_weight"], new_weight)
            
            self.db.execute("""
                UPDATE concept_nodes SET
                    access_count = ?,
                    weight = ?,
                    peak_weight = ?,
                    last_accessed = ?
                WHERE id = ?
            """, (new_access, new_weight, new_peak, now, nid))
            
            # 强化所有关联边
            edges = self.db.query(
                "SELECT target_id, weight FROM concept_edges "
                "WHERE source_id = ?", (nid,))
            for edge in edges:
                new_edge_w = min(1.0, edge["weight"] + self.EDGE_BOOST)
                # 双向更新
                self.db.execute("""
                    UPDATE concept_edges SET weight = ?
                    WHERE source_id = ? AND target_id = ?
                """, (new_edge_w, nid, edge["target_id"]))
                self.db.execute("""
                    UPDATE concept_edges SET weight = ?
                    WHERE source_id = ? AND target_id = ?
                """, (new_edge_w, edge["target_id"], nid))
            
            reinforced += 1
        
        self._journal("confirm", ids=node_ids)
        return {"reinforced": reinforced, "unknown": unknown}
```

#### 4.5.2 correct（纠正超驰）

```python
    def correct(self, old_hint: str, new_text: str) -> dict:
        """
        纠正超驰（融合而非擦除）
        
        流程:
        1. recall(old_hint) 找到最匹配的旧记忆
        2. 验证匹配质量（共享 ≥2 内容 token，或覆盖 ≥50%）
        3. 创建新节点（继承旧节点的知识连接，不继承 supersedes 边）
        4. 建立双向 supersedes/superseded-by 边
        5. 关闭旧节点（valid_to = now）
        
        关键设计:
        - 旧记忆不删除，标记为"已替代"（保留溯源链）
        - 新记忆的 confidence = old.confidence × 0.7（降级，需 confirm 恢复）
        - 旧记忆的关联边迁移到新记忆（保持知识连通性）
        - supersedes 边不迁移（防止混淆溯源链）
        
        Returns:
            {"old_text": str, "new_text": str, "old_id": str, "new_id": str}
            或 {"error": "no match"} 
        """
        # 1. 找到旧记忆
        results = self.spreading_engine.recall(old_hint, top_k=1)
        if not results:
            return {"error": "no match"}
        
        old_id, _, old_node = results[0]
        
        # 2. 验证匹配质量
        hint_toks = self._content_tokens(old_hint)
        node_toks = self._content_tokens(old_node["text"])
        shared = hint_toks & node_toks
        if not (len(shared) >= 2 or 
                (hint_toks and len(shared) / len(hint_toks) >= 0.5)):
            return {"error": "insufficient match quality"}
        
        old_text = old_node["text"]
        now = datetime.now().isoformat()
        
        # 3. 创建新节点
        new_id = hashlib.md5(
            self._clean_text(new_text).encode('utf-8')).hexdigest()[:12]
        lowered_conf = round(old_node.get("confidence", 1.0) * 0.7, 3)
        
        history = json.loads(old_node.get("history", "[]"))
        history.append({"text": old_text, "replaced": now})
        
        new_keys = self.key_extractor.extract(new_text, is_query=False)
        
        self.db.insert("concept_nodes", {
            "id": new_id,
            "text": self._clean_text(new_text),
            "weight": old_node.get("weight", 1.0),
            "peak_weight": old_node.get("peak_weight", 1.0),
            "confidence": lowered_conf,
            "access_count": 0,
            "keys": json.dumps(new_keys),
            "layer": "hippocampus",
            "created": now,
            "last_accessed": now,
            "valid_from": now,
            "valid_to": None,
            "superseded_by": None,
            "history": json.dumps(history),
            "origin": json.dumps({"via": "correct"}),
        })
        
        # 4. 迁移旧节点的知识连接到新节点
        old_edges = self.db.query(
            "SELECT target_id, relation, weight FROM concept_edges "
            "WHERE source_id = ? AND relation NOT IN ('supersedes', 'superseded-by')",
            (old_id,))
        for edge in old_edges:
            if edge["target_id"] == new_id:
                continue
            self._create_edge(new_id, edge["target_id"],
                              edge["relation"], edge["weight"], now)
            self._create_edge(edge["target_id"], new_id,
                              edge["relation"], edge["weight"], now)
        
        # 5. 建立 supersedes 双向边
        self._create_edge(new_id, old_id, "supersedes", 0.5, now)
        self._create_edge(old_id, new_id, "superseded-by", 0.5, now)
        
        # 6. 关闭旧节点
        self.db.execute("""
            UPDATE concept_nodes SET
                valid_to = ?,
                superseded_by = ?
            WHERE id = ?
        """, (now, new_id, old_id))
        
        self._journal("correct", old_id=old_id, new_id=new_id,
                       old_text=old_text, new_text=new_text)
        
        return {
            "old_text": old_text,
            "new_text": new_text,
            "old_id": old_id,
            "new_id": new_id,
        }
```

### 4.6 边权重演化

#### 4.6.1 演化规则汇总

| 事件 | 边权重变化 | 公式 |
|------|-----------|------|
| remember 创建同现边 | 初始 weight=1.0 | — |
| confirm 任一端点 | +EDGE_BOOST | min(1.0, weight + 0.25) |
| dream 每日一次 | ×EDGE_DECAY_PER_DREAM | weight × 0.95 |
| weight < EDGE_PRUNE_THRESHOLD | 修剪 | weight < 0.1 → 删除 |

#### 4.6.2 演化时间线示例

```
Day 0:  edge 创建, weight = 1.0
Day 1:  dream → weight = 0.95
Day 2:  dream → weight = 0.9025
Day 3:  confirm(端点) → weight = min(1.0, 0.9025 + 0.25) = 1.0  ← 恢复!
Day 4:  dream → weight = 0.95
...
Day 30: 无confirm, weight = 0.95^30 ≈ 0.215
Day 45: 无confirm, weight = 0.95^45 ≈ 0.099 → 修剪
```

**结论**：不用的边在 ~45 天内自然消亡，经常使用的边通过 confirm 持续保持高强度。

### 4.7 MAX_BOOST 优化

#### 4.7.1 当前问题

```python
# 当前：硬上限0.3，抑制了频繁确认的记忆
score = similarity * exp(-λ×days) + min(α×ln(1+access_count), 0.3)
#                                                       MAX_BOOST ↑
```

10次确认 vs 3次确认 → Boost 都是 0.3，无法区分。

#### 4.7.2 优化方案：移除硬上限，改用 mind 的增量模型

```python
# 优化后：每次确认增加 BOOST_PER_ACCESS，weight 上限 1.0
# 新的评分公式
def compute_score(similarity, days, access_count, peak_weight):
    """
    融合 mind 的 Ebbinghaus 衰减 + 增量式 Boost
    
    score = weight (已经包含 Ebbinghaus 衰减和确认增量)
    weight = peak_weight × retention
    retention = exp(-days / stability)
    stability = STABILITY_BASE_DAYS + access_count × STABILITY_PER_ACCESS
    
    每次 confirm:
        weight = min(1.0, weight + BOOST_PER_ACCESS)  # +0.15
        peak_weight = max(peak_weight, weight)
    """
    stability = STABILITY_BASE_DAYS + access_count * STABILITY_PER_ACCESS
    retention = math.exp(-max(0, days) / stability)
    weight = peak_weight * retention
    return weight
```

#### 4.7.3 参数对照

| 参数 | mind 值 | xiaoda-agent 建议值 | 理由 |
|------|---------|-------------------|------|
| `BOOST_PER_ACCESS` | 0.15 | **0.15** | 每次确认增加 0.15，5 次确认可达 0.75 |
| `EDGE_BOOST` | 0.25 | **0.25** | 边强化量，一次确认抵消 5 天边衰减 |
| `MAX_BOOST` | — (无硬上限) | **移除** | 用 weight 上限 1.0 替代 |
| `STABILITY_BASE_DAYS` | 3.0 | **3.0** | 未确认记忆 3 天半衰期 |
| `STABILITY_PER_ACCESS` | 14.0 | **14.0** | 每次确认买两周稳定性 |
| `GRACE_DAYS` | 45 | **45** | 45天宽限期，防止高频月度事实被误删 |
| `WEIGHT_THRESHOLD` | 0.1 | **0.1** | 修剪阈值 |
| `EDGE_DECAY_PER_DREAM` | 0.95 | **0.95** | 每日边衰减 5% |
| `EDGE_PRUNE_THRESHOLD` | 0.1 | **0.1** | 边修剪阈值 |

---

## 5. 实施步骤

### Phase 1：基础设施（1-2 周）

**目标**：引入概念图表，保留现有检索逻辑

| 任务 | 说明 | 产出 |
|------|------|------|
| 1.1 创建 concept_nodes/edges 表 | SQLite DDL，见 4.1.1 | migration_v1.sql |
| 1.2 数据迁移 | 现有 memories → concept_nodes | migrate.py |
| 1.3 Key 提取器 | jieba 分词 + 停用词 + stem，提取索引 key | key_extractor.py |
| 1.4 同现边自动创建 | remember 时自动与共享 ≥2 key 的节点建边 | hippocampus.py |
| 1.5 基础 CRUD | node/edge 的增删改查 | hippocampus.py |

**验证标准**：
- 现有记忆迁移后，检索结果与迁移前一致（向量 top-k + FTS5 仍可用）
- 新 remember 命令能自动创建节点和边
- 回归测试全部通过

### Phase 2：扩散激活检索（2-3 周）

**目标**：替代纯向量 top-k，实现 mind 风格的三通道融合检索

| 任务 | 说明 | 产出 |
|------|------|------|
| 2.1 扩散激活引擎 | 实现 recall 的直接+扩散+RRF+重排+去重 | spreading_activation.py |
| 2.2 模式补全 | 无直接命中时的模糊匹配 | spreading_activation.py |
| 2.3 同现术语发现 | RelatedTerms 类，2-hop PageRank | related_terms.py |
| 2.4 A/B 对比测试 | 扩散激活 vs 向量 top-k，测量 recall@k | test_recall.py |

**验证标准**：
- 对"我的项目框架"类查询，扩散激活能召回间接关联记忆
- recall@5 比纯向量 top-k 提升 ≥30%
- 单次检索延迟 < 50ms（1000 节点规模）

### Phase 3：确认与纠正（1-2 周）

**目标**：实现 confirm/correct 机制

| 任务 | 说明 | 产出 |
|------|------|------|
| 3.1 confirm 机制 | 确认 → 节点+边权重强化 | confirm_correct.py |
| 3.2 correct 机制 | 纠正 → 超驰+溯源链 | confirm_correct.py |
| 3.3 provenance 日志 | journal.jsonl 追加写入 | journal.py |
| 3.4 why 命令 | 查询记忆完整历史 | commands.py |
| 3.5 MAX_BOOST 优化 | 移除 0.3 硬上限，改用增量模型 | fluid_memory.py |

**验证标准**：
- confirm 后节点的 access_count+1, weight+0.15, 边+0.25
- correct 后旧记忆 valid_to 被设置，新记忆继承连接
- why 命令可追溯完整的创建/确认/纠正历史
- 10 次确认的记忆 Boost 远大于 3 次确认的记忆

### Phase 4：梦境周期（2-3 周）

**目标**：实现三阶段梦境周期

| 任务 | 说明 | 产出 |
|------|------|------|
| 4.1 Light sleep | 信号收集与清理 | dream_engine.py |
| 4.2 Deep sleep | Ebbinghaus 衰减 + 边衰减（每日一次） | dream_engine.py |
| 4.3 REM - 聚类提升 | 相似度>0.45 的≥3条记忆提升到 Cortex | dream_engine.py |
| 4.4 REM - 矛盾检测 | 共享稀有关键词的矛盾对标记 | dream_engine.py |
| 4.5 Cortex 层 | 按主题归档的长期巩固层 | cortex.py |
| 4.6 自动触发 | 信号≥10 或 >24h 自动 dream | dream_trigger.py |
| 4.7 Working Memory | 从 Hippocampus 取 top-N 热记忆注入上下文 | working_memory.py |

**验证标准**：
- Deep sleep 后，未确认的记忆权重显著下降
- 45 天宽限期内无记忆被修剪
- 聚类提升后，Cortex 中出现整合的主题文档
- 矛盾检测能标记"用户偏好 Python"vs"用户偏好 Java"这类矛盾
- 自动触发在 10 次写入后正确执行

### Phase 5：集成与优化（1-2 周）

**目标**：与 xiaoda-agent 现有系统集成

| 任务 | 说明 | 产出 |
|------|------|------|
| 5.1 替换 memory_manager | 统一使用扩散激活检索 | memory_manager.py |
| 5.2 Agent 对话集成 | 对话中自动 remember/recall/confirm | agent_integration.py |
| 5.3 工作记忆注入 | 每轮对话自动注入 Working Memory | context_builder.py |
| 5.4 性能优化 | 缓存、批量查询、索引优化 | 各模块 |
| 5.5 端到端测试 | 完整对话场景测试 | test_e2e.py |

**验证标准**：
- 完整对话场景：用户自我介绍→记住→几天后回忆→纠正错误→确认→梦境整合
- 端到端延迟增加 < 100ms
- 内存占用增加 < 50MB

---

## 6. 预期效果

### 6.1 检索质量提升

| 指标 | 当前 | 优化后 | 提升幅度 |
|------|------|--------|---------|
| recall@5（直接+间接关联） | ~40% | ~65% | +62% |
| 联想召回率（间接关联记忆） | ~5% | ~35% | +600% |
| 用户偏好查询准确率 | ~60% | ~90% | +50% |
| 上下文连续性（跨会话） | 低 | 高 | 质变 |

### 6.2 记忆生命周期管理

| 指标 | 当前 | 优化后 |
|------|------|--------|
| 核心记忆保护 | Boost 上限 0.3，衰减快 | Ebbinghaus + 每次 confirm 买 14 天 |
| 不活跃记忆清理 | 简单归档 | 45天宽限 + Ebbinghaus 自然衰减 |
| 矛盾记忆处理 | 无 | 自动检测 + correct 超驰 |
| 溯源能力 | 无 | journal.jsonl 完整溯源 |

### 6.3 系统架构提升

| 维度 | 当前 | 优化后 |
|------|------|--------|
| 记忆层数 | 1 层（FlatMemory） | 3 层（Working→Hippocampus→Cortex） |
| 检索通道 | 2（向量+FTS5） | 3（直接+扩散+模式补全）+ RRF融合 |
| 概念图 | 无 | 加权有向图（nodes+edges） |
| 梦境周期 | 1 阶段（衰减+归档） | 3 阶段（Light→Deep→REM） |
| 确认/纠正 | 无 | confirm 强化 + correct 超驰+溯源 |

---

## 7. 不采纳的部分

以下 mind 机制**不纳入** xiaoda-agent 优化方案，附理由：

### 7.1 AGENTS.md / CLAUDE.md / GEMINI.md 导出

**mind 的做法**：将 Working Memory 导出为 Agent 规则文件（AGENTS.md 等），注入编码 Agent 的上下文。

**不采纳理由**：xiaoda-agent 是情感陪伴 AI，不是编码 Agent。xiaoda-agent 有自己的规则文件系统和上下文构建机制（context_builder.py），不需要通过文件系统注入规则。Working Memory 应通过 xiaoda-agent 现有的上下文构建管道注入对话上下文。

### 7.2 脚本式 CLI 接口

**mind 的做法**：`python3 mind.py remember "text"`，`python3 mind.py recall "query"` 等命令行操作。

**不采纳理由**：xiaoda-agent 是长期运行的服务进程，记忆操作通过内部 API 调用，不需要 CLI 接口。Agent 在对话中自动触发 remember/recall/confirm，用户无需手动操作。

### 7.3 .cursorrules / .windsurfrules 等工具特定 dotfile 导出

**mind 的做法**：根据项目已有的工具配置文件，自动写入 .cursorrules 等规则文件。

**不采纳理由**：这是编码工具链的特定需求，与情感陪伴场景无关。

### 7.4 Arabic 语言支持（停用词、词干提取、破碎复数）

**mind 的做法**：双语（English + Arabic）分词、词干提取、同现分析。

**不采纳理由**：xiaoda-agent 的目标用户使用中文，不需要 Arabic 支持。但**应该加强中文分词**（jieba 已有，可考虑加入 pkuseg 对比）、中文停用词、中文同义词扩展。这属于 Phase 1 的 key_extractor.py 工作范围。

### 7.5 CONCEPT_SEED 静态概念种子

**mind 的做法**：硬编码技术术语到分类的映射（如 `"fastapi": ("backend", "python")`）。

**不采纳理由**：mind 的 CONCEPT_SEED 面向编程领域，xiaoda-agent 面向情感陪伴领域，概念体系完全不同。如果要实现类似功能，应为情感陪伴领域设计专属的概念种子（如情绪分类、关系类型），但这不是核心需求，暂不实施。

### 7.6 并发文件锁（fcntl/msvcrt）

**mind 的做法**：graph.json 的 read-merge-write 需要文件锁保护，支持 POSIX fcntl 和 Windows msvcrt。

**不采纳理由**：xiaoda-agent 使用 SQLite 作为存储后端，SQLite 自带事务和锁机制（WAL 模式），不需要额外的文件锁。SQLite 的并发控制比文件锁更成熟可靠。

### 7.7 symlink 安全检查

**mind 的做法**：大量代码检查 `.mind/` 目录下的符号链接，防止写入逃逸信任边界。

**不采纳理由**：xiaoda-agent 本地部署，不存在多用户安全隔离需求。SQLite 数据库文件的安全性由文件系统权限保证。

---

## 附录 A：关键参数速查表

| 参数 | 值 | 来源 | 说明 |
|------|-----|------|------|
| `BOOST_PER_ACCESS` | 0.15 | mind | 每次 confirm 的节点权重增量 |
| `EDGE_BOOST` | 0.25 | mind | 每次 confirm 的边权重增量 |
| `STABILITY_BASE_DAYS` | 3.0 | mind | 未确认记忆的稳定性天数 |
| `STABILITY_PER_ACCESS` | 14.0 | mind | 每次确认增加的稳定性天数 |
| `GRACE_DAYS` | 45 | mind | 记忆修剪宽限期 |
| `WEIGHT_THRESHOLD` | 0.1 | mind | 节点修剪权重阈值 |
| `EDGE_DECAY_PER_DREAM` | 0.95 | mind | 每日边衰减系数 |
| `EDGE_PRUNE_THRESHOLD` | 0.1 | mind | 边修剪权重阈值 |
| `RECALL_RADIUS` | 3 | mind | 扩散激活最大跳数 |
| `ACTIVATION_DECAY` | 0.5 | mind | 每跳激活衰减系数 |
| `SPREADING_THRESHOLD` | 0.05 | mind | 扩散传播最低激活值 |
| `RECALL_TOP_K` | 5 | mind | 返回结果数 |
| `RRF_K` | 60 | mind | RRF 平滑参数 |
| `FUZZY_ACTIVATION` | 0.5 | mind | 模糊匹配激活系数 |
| `SEPARATION_SIM` | 0.92 | mind | 去重相似度阈值 |
| `CLUSTER_SIM` | 0.45 | mind | 聚类相似度阈值 |
| `PROMOTION_THRESHOLD` | 3 | mind | 聚类提升最小成员数 |
| `ACTIVE_TOKEN_BUDGET` | 800 | mind | 工作记忆字符预算 |
| `AUTO_DREAM_SIGNALS` | 10 | mind | 自动梦境触发信号数 |
| `AUTO_DREAM_HOURS` | 24 | mind | 自动梦境触发小时数 |

## 附录 B：扩散激活算法完整伪代码

```
FUNCTION recall(query, top_k=5, max_hops=3):
    // ─── Step 1: Key 提取 ───
    query_keys = extract_keys(query, is_query=True)
    IF query_keys IS EMPTY:
        RETURN []
    END IF
    
    // ─── Step 2: Key 扩展 ───
    expanded_keys = set(query_keys)
    FOR key IN query_keys:
        IF df(key) < 2:  // 稀有条目才扩展
            FOR (term, score) IN related_terms(key, top_k=4):
                IF score >= 0.15 AND term NOT IN IDENTITY_KEYS:
                    expanded_keys.add(term)
                END IF
            END FOR
        END IF
    END FOR
    keys = expanded_keys
    
    // ─── Step 3: 存活节点 + IDF ───
    alive = {nid: node FOR nid, node IN nodes IF valid_at(node)}
    N = max(1, len(alive))
    idf = {k: ln(1 + N / (1 + df[k])) FOR k IN keys}
    
    // ─── Step 4: 直接命中通道 ───
    direct = {}
    FOR (nid, node) IN alive:
        w_bias = 0.35 + 0.65 × node.weight
        shared = keys ∩ set(node.keys)
        IF shared IS NOT EMPTY:
            direct[nid] += Σ(idf[k] FOR k IN shared) × w_bias
        END IF
        substr = count(k IN keys WHERE len(k) >= 4 AND k IN node.text.lower())
        reverse = count(k IN node.keys WHERE len(k) >= 4 AND k IN query.lower())
        IF substr + reverse > 0:
            direct[nid] += (substr + reverse) × 0.6 × w_bias
        END IF
    END FOR
    
    // ─── Step 5: 模式补全（无直接命中时）───
    IF direct IS EMPTY:
        FOR (nid, node) IN alive:
            sim = embedder.similarity(query, node.text)
            IF sim >= 0.25:
                direct[nid] = sim × 0.5 × node.weight
            END IF
        END FOR
    END IF
    IF direct IS EMPTY:
        RETURN []
    END IF
    
    // ─── Step 6: 扩散激活通道 ───
    spread = {}
    wave = dict(direct)  // 种子波
    FOR hop = 0 TO max_hops:
        nxt = {}
        FOR (nid, activation) IN wave:
            spread[nid] = spread.get(nid, 0) + activation
            IF hop < max_hops AND activation > 0.05:
                FOR (neighbor, edge) IN get_edges(nid):
                    IF neighbor NOT IN alive: CONTINUE
                    propagated = activation × 0.5 × edge.weight / (hop + 1)
                    nxt[neighbor] = nxt.get(neighbor, 0) + propagated
                END FOR
            END IF
        END FOR
        wave = nxt
        IF wave IS EMPTY: BREAK
    END FOR
    
    // ─── Step 7: RRF 融合 ───
    dr = rank(direct, descending)
    sr = rank(spread, descending)
    fused = {}
    FOR nid IN (keys(direct) ∪ keys(spread)):
        fused[nid] = 1/(60 + dr.get(nid, len(dr)+1)) 
                   + 1/(60 + sr.get(nid, len(sr)+1))
    END FOR
    
    // ─── Step 8: 语义重排 ───
    ranked = sort(fused, by value DESC)
    reranked = []
    FOR (nid, base) IN ranked[:top_k × 3]:
        sim = embedder.similarity(query, nodes[nid].text)
        reranked.append((nid, base × (1.0 + sim)))
    END FOR
    reranked = sort(reranked, by value DESC)
    
    // ─── Step 9: 模式分离（去重）───
    selected = []
    FOR (nid, score) IN reranked:
        is_dup = False
        FOR (sel_nid, _) IN selected:
            IF embedder.similarity(nodes[nid].text, nodes[sel_nid].text) >= 0.92:
                is_dup = True
                BREAK
            END IF
        END FOR
        IF NOT is_dup:
            selected.append((nid, score))
        END IF
        IF len(selected) >= top_k: BREAK
    END FOR
    
    RETURN [(nid, score, nodes[nid]) FOR (nid, score) IN selected]
END FUNCTION
```

## 附录 C：梦境三阶段完整伪代码

```
FUNCTION dream(dry_run=False):
    report = {}
    
    // ═══════════════════════════════════════════
    // Stage 1: Light Sleep — 信号收集与清理
    // ═══════════════════════════════════════════
    signals = read_file("signals.jsonl")
    signal_count = len(signals)
    IF NOT dry_run:
        delete_file("signals.jsonl")
    END IF
    report["light"] = {"signals_read": signal_count}
    
    // ═══════════════════════════════════════════
    // Stage 2: Deep Sleep — Ebbinghaus 衰减 + 突触稳态
    // ═══════════════════════════════════════════
    pruned_nodes = []
    
    // 2a. 艾宾浩斯衰减
    FOR node IN all_nodes:
        // 已被替代的旧事实：超过宽限期则归档
        IF node.valid_to IS NOT NULL:
            closed_days = (now - parse(node.valid_to)).days
            IF closed_days > GRACE_DAYS:
                pruned_nodes.append(node)
            END IF
            CONTINUE  // 已替代节点不做常规衰减
        END IF
        
        // 常规衰减
        days = max(0, (now - parse(node.last_accessed)).days)
        stability = 3.0 + node.access_count × 14.0
        retention = exp(-days / stability)
        new_weight = clamp(node.peak_weight × retention, 0.0, 1.0)
        
        IF NOT dry_run:
            node.weight = new_weight
        END IF
        
        // 修剪条件
        IF new_weight < 0.1 AND node.access_count < 2 AND days > 45:
            pruned_nodes.append(node)
        END IF
    END FOR
    
    // 归档修剪的节点（归档，非删除！）
    IF pruned_nodes IS NOT EMPTY AND NOT dry_run:
        archive_to_file(pruned_nodes, "archive.md")
        FOR node IN pruned_nodes:
            remove_node_and_edges(node)
        END FOR
    END IF
    
    // 2b. 突触稳态（边衰减，每天最多一次）
    today = now.date()
    pruned_edges = 0
    IF get_meta("last_edge_decay") < today:
        FOR edge IN all_edges:
            edge.weight = round(edge.weight × 0.95, 4)
            IF edge.weight < 0.1:
                IF NOT dry_run: remove(edge)
                pruned_edges += 1
            ELSE IF NOT dry_run:
                save(edge)
            END IF
        END FOR
        IF NOT dry_run:
            set_meta("last_edge_decay", today)
        END IF
    END IF
    
    report["deep"] = {
        "nodes_pruned": len(pruned_nodes),
        "edges_pruned": pruned_edges
    }
    
    // ═══════════════════════════════════════════
    // Stage 3: REM — 聚类提升 + 矛盾检测
    // ═══════════════════════════════════════════
    
    // 3a. 聚类提升
    clusters = []
    FOR node IN sorted_alive_nodes:
        placed = False
        FOR cluster IN clusters:
            IF similarity(node.text, cluster.centroid) > 0.45:
                cluster.members.append(node)
                placed = True
                BREAK
            END IF
        END FOR
        IF NOT placed:
            clusters.append({centroid: node.text, members: [node]})
        END IF
    END FOR
    
    promoted = []
    FOR cluster IN clusters:
        IF len(cluster.members) >= 3:
            topic = cluster.centroid[:50]
            IF NOT dry_run:
                cortex.promote(topic, cluster.members)
            END IF
            promoted.append(topic)
        END IF
    END FOR
    
    // 3b. 矛盾检测
    conflicts = []
    alive = [n FOR n IN nodes IF valid_at(n)]
    N = len(alive)
    
    // 计算每个 key 的文档频率
    df = {}
    FOR node IN alive:
        FOR key IN node.keys:
            df[key] = df.get(key, 0) + 1
        END FOR
    END FOR
    
    FOR i = 0 TO len(alive) - 1:
        FOR j = i + 1 TO len(alive) - 1:
            a = alive[i], b = alive[j]
            rare_shared = [k FOR k IN (a.keys ∩ b.keys) 
                           IF df[k] <= max(2, N/4)]
            IF len(rare_shared) < 2: CONTINUE
            
            sim = similarity(a.text, b.text)
            IF 0.35 <= sim < 0.9:
                IF NOT dry_run:
                    link(a.id, b.id, "possible-conflict", weight=0.5)
                END IF
                conflicts.append((a.id, b.id))
            END IF
        END FOR
    END FOR
    
    report["rem"] = {
        "clusters_promoted": len(promoted),
        "conflicts_found": len(conflicts)
    }
    
    RETURN report
END FUNCTION
```

---

> **文档状态**: 初版完成，待团队评审  
> **下一步**: Phase 1 启动 — 创建 concept_nodes/edges 表 + 数据迁移
