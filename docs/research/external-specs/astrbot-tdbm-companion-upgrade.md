# 小妲长期情感陪伴升级方案 — 基于 AstrBot 与 TencentDB-Agent-Memory 对照研究

> **版本**: v1.1 | **日期**: 2026-08-23 | **实现基线**: `eba85d91`（xiaoda-agent）
> **参考固定点**: AstrBot `19d00fb1f0d822690a467e8dca498adebbb2d67b`、TencentDB-Agent-Memory `97f94654280b2932c35ba4806a491999ed244cc9`（`v2.0.1-beta.2` 后 1 个提交）
> **研究方式**: 三路并行只读调研——①AstrBot 平台接入/流式输出/群聊机制；②TDB 记忆全生命周期；③本项目记忆/流式/群聊现状基线
> **定位**: 本文档为**已评审、分批灰度的实现规格**；外部证据与仓库契约核验见 [`astrbot-tdbm-companion-upgrade-validation.md`](astrbot-tdbm-companion-upgrade-validation.md)

---

## 目录

1. [结论速览与三方定位](#1-结论速览与三方定位)
2. [本项目现状基线（对照锚点）](#2-本项目现状基线对照锚点)
3. [AstrBot 研究报告](#3-astrbot-研究报告)
4. [TencentDB-Agent-Memory 研究报告](#4-tencentdb-agent-memory-研究报告)
5. [优化方案（P0/P1/P2）](#5-优化方案p0p1p2)
6. [明确不采纳清单](#6-明确不采纳清单)
7. [实施路线图与风险](#7-实施路线图与风险)

---

## 1. 结论速览与三方定位

| 项目 | 定位 | 对本项目的价值 |
|---|---|---|
| **AstrBot** | 多平台 IM 框架：19 个平台适配器 + 洋葱模型管线 + 统一事件流 | 学**工程接口设计**：流式降级决策、break 分段信号、群聊攒批 ICL、唤醒判定单点化 |
| **TDB-Agent-Memory** | 团队级"经验资产管理流水线"：L0→L3 提炼—消解—聚合—装配 | 学**记忆数据流**：四动作冲突消解、warm-up 阈值、场景叙事层；但其情感观要**反着来** |
| **本项目** | 单用户深度情感陪伴，FSRS 遗忘 + 七路混合检索 | 检索侧已强于两个参考项目；洼地在：写入质量、群聊上下文、工具轮流式断流 |

**总判断**：

1. 不重写检索排序架构；P0-2 仍必须为所有召回通道补统一的 current-memory 可见性谓词，否则 superseded 记录会从旁路重新进入结果。
2. TDB 真正值得偷的是**写入侧**（去重消解）和**调度侧**（触发节奏），以及 L2"叙事文件"思想恰好补 MEMORY.md 静态/动态割裂。
3. AstrBot 值得偷的是三个小而美的接口模式（break 信号 / 能力声明位 / 攒批 ICL），不是它的管线全家桶。
4. 最大空白是群聊——但受 QQ 官方 bot API 限制（只回调 @bot 消息），先在现有协议内修"@了也失明"，NapCat 双轨作为远期可选项。

---

## 2. 本项目现状基线（对照锚点）

### 2.1 记忆系统

**写入链路**：`agent_core/mixins/main_path.py:211 _finalize_main_reply()` 尾部触发后台任务（:250）。前置门槛 `main_path.py:233`：

```python
_should_remember = is_master or source != "qq_group"
```

即群聊非主人消息完全不入库不入记忆；降级回复既不进 history 也不进记忆。

编码 pipeline（`memory/_memory_encoder.py:35 encode_memory`）：规则式摘要 → 内容校验 → 安全扫描 → 规则式重要性估计（`:886 _estimate_importance`：基础 0.3 + 负面情绪 +0.3 / 正面 +0.1 + 长度加分）→ **ADD-only 写入 `episodic_memories`（is_raw=1，无写入去重）** → FSRS 初始化（事实类关键词直接 permanent，否则 buffer/S_INIT）→ 异步补建索引（向量/概念图/子 chunk）→ 失效读缓存。

调度：`core/background_tasks.py:317-343`，固定门槛 `history >= 4` 条，取最近 6 条 + 压缩缓冲，90s 超时包裹。

**存储**：SQLite 主库（`db/ddl_schema.py` 五阶段 DDL）+ 独立向量库 `agent_vec.db`（`memory/vector_store.py:708` sqlite-vec vec0 四张虚拟表）+ 可选 numpy 暴力索引。三级 scope 隔离（user_id/session_id/agent_id，`memory/scope.py`，绑定于 `agent_core/core.py:430 bind_scope`）。

**检索**：`memory/_retrieval_engine.py:63 retrieve_memories_hybrid`——FTS + 向量 + KG + 子 chunk + 扩散激活 + 实体 + KG v2 七路并行召回 → 加权 RRF → Entity Boost → Reranker → FSRS 打分（`fsrs_model.py:214 similarity × retrievability`）→ recency boost → 内容相似度去重(0.85) → 话题触发补充召回。超时 `MEMORY_RETRIEVE_TIMEOUT=8s`（USB 盘历史教训）。

**注入**：`agent_context.py:851 build_messages` 三层 system（Stable=workspace MD / Context=画像+摘要+习得规则 / Volatile=时间语境+情绪惯性+待办）；记忆单独作第二条 system 消息插在 history 之后（`:901-904`，`<memory_retrieval>` XML 包裹 conversation_logs 与 distilled_memories 两段）。

**遗忘/治理**：FSRS-DSR 相位机（buffer/reinforced/decay/permanent/archived）、周期蒸馏（`_memory_maintenance.py:98` 未蒸馏 >200 条触发）、定时回忆笔记（3h 窗口、importance≥0.6）、事后冲突消解（`core/conflict_supersession.py` superseded_by 标记）、ContextNest 哈希链审计。

**会话边界**：内存 history token 驱动压缩（CCR→LLM summarize→强制 pop）；跨会话走 `conversation_logs` 表 + 重启恢复 24h 内 50 条转第一人称叙事 `_restored_summary`。

### 2.2 流式输出

- WebUI 有真 token 流式但**有条件断流**：`main_path.py:646` `if STREAM_TEXT_PUSH and status_callback and not tools:` ——一挂工具就整段返回。`agent_core/mixins/streaming.py:17 _stream_llm_response` 迭代 delta 推 `{type:"stream_text"}`，最终仍发一次 final 全量事件（`web/ws_hub.py:865-866`）。
- QQ 无真流式（官方 API 限制，`status_notify` 为空函数）：收到消息先发 ACK 文案占 1 次被动配额；拿到完整 ProcessResult 后伪流式分片（`qq_bot_adapter.py:1274 _send_reply_with_sticker`：>400 字按 300 字符切片，配额上限 ACK+4 片=官方 5 次/5 分钟限制，片间随机 0.8~1.2s 模拟打字）。
- ProcessResult（`agent_core/_shared.py`）是一次性终产物：reply/emotion/sticker/audio/video 全部在 finalize 后一次成型。

### 2.3 群聊

- 入口 `qq_bot_adapter.py:851 on_group_at_message_create`——botpy SDK 只把 **@bot 的消息**回调进来，非 @ 发言物理上到不了程序。
- 处理流程 `:787 _handle_group_at_message`：剥表情 tag → master 识别 → 发 ACK → `source="qq_group"` 调 process → 分片回复。仅有 per-group asyncio.Lock（:794）串行与官方 5 条/5 分钟被动配额，无唤醒词/概率回复/冷却限流。
- **最大缺口**：群里任何人的发言都不进 context.history、不写日志、不参与记忆编码（见 2.1 `_should_remember`）；且不同成员 user_id 不同，`switch_user_context` 各自切换 history，互相看不见对方刚说过什么——每次 @ 都是孤立单轮。

### 2.4 已确认的基线短板清单

| # | 短板 | 位置 |
|---|---|---|
| B1 | ADD-only 写入无去重，靠检索端补救，长期表膨胀 + "矛盾双记忆"可能同时注入 | `_memory_encoder.py` |
| B2 | 重要性评分纯规则，无 LLM 参与、无类型区分 | `_memory_encoder.py:886` |
| B3 | 记忆编码固定门槛 history≥4，新用户首轮关键信息（名字/喜好）沉淀慢 | `background_tasks.py:317` |
| B4 | MEMORY.md 静态人格记忆与 episodic 向量记忆两条通道互不相通 | `prompt_builder.py:296` vs `episodic_memories` |
| B5 | 工具轮内零流式反馈，用户面对纯 ACK 干等 | `main_path.py:646` |
| B6 | QQ 切片按字数硬切，句子腰斩；首片延迟 = 全程生成时间 | `channel_adapter_base.py:69` |
| B7 | 群聊上下文断裂（他人发言零留痕） | `main_path.py:233` |
| B8 | 群聊交互模式单一：无冷却限流、无概率插话 | `qq_bot_adapter.py:787` |

---

## 3. AstrBot 研究报告

架构一句话：19 个平台适配器只做协议翻译，收（raw→`AstrBotMessage`→commit_event 进全局队列）发（MessageChain→平台 API），中间所有唤醒判断/限流/LLM/人格/渲染跑在同一条九阶段洋葱管线上（WakingCheck → Whitelist → SessionStatus → RateLimit → ContentSafety → PreProcess → Process[插件/LLM] → ResultDecorate → Respond）。

### 3.1 平台接入抽象

- **统一事件主键**：`unified_msg_origin = platform_id:message_type:session_id`（`astr_message_event.py:106`），一根字符串打通 DB、配置映射、会话锁、偏好存储。
- **能力声明位**：`PlatformMetadata.support_streaming_message / support_proactive_message`（`platform_metadata.py`）——适配器声明能力，上游一处 if 决定整条链路走向（真流式 or 降级整段），新增平台核心零改动。
- **注册机制**：`register_platform_adapter(name, ..., support_streaming_message)` 装饰器 + 动态 import，`PlatformManager` 按 type 实例化并热插拔，内建 `PlatformStatus` 状态机供 Dashboard 展示每个适配器健康度。
- **消息链**：21 种 pydantic 消息组件（Plain/Image/At/Reply/Node...），`MessageChain.squash_plain()` 合并相邻文本段。

### 3.2 流式输出设计（重点）

数据通路：Agent 层产出 async generator → `MessageEventResult(result_content_type=STREAMING_RESULT, async_stream=...)` → RespondStage 调 `event.send_streaming()`。三种典型平台实现：

| 平台 | 机制 |
|---|---|
| Telegram 私聊 | `sendMessageDraft` 草稿动画：信号驱动（asyncio.Event，单 in-flight，RTT 自然限流），结束时发真实 MarkdownV2 消息持久化 |
| Telegram 群聊 | 首条 send_message，此后 edit_message_text 就地更新；节流 0.6s，typing action 单独 0.5s；遇 break 结束当前消息下个 delta 新起一条 |
| aiocqhttp(QQ/NapCat) | 不支持真流式：fallback 模式按标点正则切句逐条发送 + sleep(1.5) 模拟分条打字；否则缓冲合并一次发出 |

**精华点**：
1. **break 分段信号**（`astr_agent_run_util.py:216-223`）：工具调用前 yield 空 MessageChain(`type="break"`)，上层业务不需要知道任何平台细节就能要求"另起一条消息/更新状态"，各平台自行解释。
2. **集中降级开关**：`unsupported_streaming_strategy` 一处布尔运算决定 stream_to_general，降级决策不散落。
3. **Telegram draft 的信号驱动循环**：不引入定时器复杂度。

**糟粕点**（引以为戒）：
1. STREAMING_RESULT → yield → 结束设 STREAMING_FINISH → RespondStage 用 extra 再挡一次，三层防重复发送全靠约定，可读性差。
2. **流式路径完全跳过装饰阶段**（at 回复/引用/t2i/TTS/内容安全复查在流式下静默失效，仅一条 warning）——用户感知是"开了流式就不 @ 我了"这类难排查反馈。
3. aiocqhttp 分句正则与 sleep(1.5) 硬编码在事件类里，与 ResultDecorate 另一套可配置分段逻辑重复建设。

### 3.3 群聊设计（重点）

1. **唤醒判定单点化**：WakingCheckStage 一个文件收敛前缀命中/@机器人/@全体/引用 bot 消息/私聊默认唤醒/指令过滤/权限，后续 stage 只消费 `is_wake` 布尔。区分 `is_at_or_wake_command`（指令型）与普通唤醒。
2. **群成员发言进入上下文的"攒批 ICL"模式**（`builtin_stars/astrbot/group_chat_context.py`）——本项目最该抄的设计：
   - 每条未唤醒群消息格式化为 `[昵称/HH:MM]: 文本 [图片] [⚠️DIRECTED AT YOU]` 存 per-group 内存 deque（上限 1000 条，带 record_id）；
   - **bot 下次被唤醒触发 LLM 时，把这批积累一次性包进 `<system_reminder>` 块注入，然后清空缓冲**——token 成本 O(1)/轮，避免每条都烧 token；
   - 同时另一 handler 把群消息持久化到 DB（上限 700 条），并注册 `get_group_message_history` 工具（limit/keyword/sender/before_id 过滤）让模型需要时主动翻群史——冷热两级。
3. **概率主动回复**：配置 possibility_reply 0~1 + 白名单，条件满足时 random < p 即发起一次 LLM 插话。
4. **防刷屏**：RateLimitStage 固定窗口 per-session deque + lock，超限两种策略 stall（排队保序，适合聊天）/ discard；长回复超阈值自动转合并转发消息。
5. 人格挂在 conversation 上而非群上（`Conversation.persona_id`），同一群切换对话即可换人格。

---

## 4. TencentDB-Agent-Memory 研究报告

定位："凡是能让下一个 Agent 少走弯路的信息都应该被保存、组织、复用"——把记忆当有 Owner/版本/权限的**资产**而非聊天日志。TypeScript/Node ≥22，五模块（MemoryCore 引擎 / MemoryProxy 透明代理 / MemoryKnowledge / MemoryPanel / SDK）。

### 4.1 四层记忆模型

| 层 | 形态 | 说明 |
|---|---|---|
| L0 Conversation | SQLite 行 + JSONL append-only 备份 | 原始对话逐条落库，带向量/FTS 索引 |
| L1 Atom | 结构化原子记忆行 | chat 模式三类：persona(稳定属性)/episodic(客观事件，**排除纯主观感受**)/instruction(priority=-1 表示死命令)；字段含 priority(0-100)、timestamps[]、version、五维隔离列 |
| L2 Scenario | **Markdown 场景文件**（非数据库行） | META 头含 heat；模板章节：核心叙事(Trigger→Action→Result)/演变轨迹/待确认矛盾点；上限 maxScenes=15 |
| L3 Persona | 单一 persona.md ≤2000 字符 | 用户画像快照，尾部附 Scene Navigation 场景索引（按 heat 降序） |

### 4.2 生成/提炼机制

- **触发调度**（`utils/pipeline-manager.ts`）：L1 每 5 轮或空闲 600s；**warm-up 指数阈值**——新会话从 1 起，每次成功后翻倍（1→2→4→8…封顶 5），保证早期对话快速沉淀、成熟期降频省成本；L2 用 downward-only timer（只能提前不能推迟：L1 完成 → max(now+10s, lastL2+900s)，完成后无条件 3600s 轮询）；L3 全局 mutex + 每 50 条新记忆触发。
- **提取 prompt**（`prompts/l1-extraction.ts`）：单次调用同时做情境切分+记忆提取，原则"宁缺毋滥/独立完整/归纳合并"，输出带 source_message_ids 溯源的严格 JSON。
- **冲突消解四动作协议**（`record/l1-dedup.ts::batchDedup`，**最精华**）：Phase1 每条新记忆向量召回 topK=5 候选（降级链向量→FTS→跳过）；Phase2 **单次批量 LLM 判定统一候选池**，输出 store/update/merge/skip 四动作。规则极细：状态类倾向 merge、同事件前因后果 merge 为一条、跨 type 可合并、一条新记忆可替换多条旧碎片（target_ids[] 多对多）、merge 后 priority 酌情提升、timestamps 取新旧并集保完整时间线。
- **L2 场景重组**：给 LLM 发 read/write/edit 工具让它自主操作沙箱内的 md 文件；强制 UPDATE > MERGE > CREATE 序（每批至多新增 1 个场景）；heat 规则新建=1、更新+1、合并=sum+1；超配额必须合并 heat 最低者；冲突不覆盖而是记入"待确认矛盾点"。

### 4.3 检索机制

- hybrid RRF(k=60) 融合 BM25 与 cosine，候选过采样 ×2~3 后 scoreThreshold=0.3 过滤取 top5；硬超时 5000ms。
- **无 reranker、无时间衰减、无 importance 加权**——priority 只用于展示不参与排序。
- token 预算：per-memory + total 两级字符上限，截断尾巴提示可用工具查详情。
- **注入分区优化 KV cache**（亮点）：动态 L1 记忆 prepend 到 user prompt（`<relevant-memories>`），稳定 persona + 场景导航 append 到 system prompt——不变内容吃上游 KV cache。
- 渐进式披露：system prompt 只放场景索引摘要，全文靠 read_file 按需加载。

### 4.4 对情感陪伴场景的根本错位

1. **系统性排斥主观信息**：L1 提取 prompt 明确丢弃"纯主观感受（不带客观事件的情绪表达）"——陪伴场景里"用户今天很低落"恰恰是最该记的。沿用其 prompt 情感数据在入口就被掐断。
2. **无时间维度建模**：检索无 recency/decay 加权，三个月前琐事与昨晚倾诉等价——陪伴需要"上次你说要考试了，考得怎样？"
3. **画像静态快照化**：persona.md 每 50 条才重写、2000 字硬顶，承载不了高频变化的情绪基线与关系温度；没有关系阶段这类连续变量的容器。
4. **架构重量级高延迟**：Node22 + Redis/COS + 可选云 VDB + Proxy/Hub/Knowledge 三服务，面向团队 SaaS；反馈周期以十分钟计，Orange Pi 单机跑不动也不必要。
5. **隐私模型错位**：private/team/restricted ACL 解决团队共享泄漏，陪伴场景要的是单人纵向深挖 + 本地主权。

**一句话**：它是优秀的"经验资产管理流水线"，但是"关于用户的数据库"而非"与用户的关系模型"。

---

## 5. 优化方案（P0/P1/P2）

每项标注：来源（AST=AstrBot / TDB=TDB-Agent-Memory / SYN=两仓库都没有的自研合成项）、痛点编号（见 2.4）、落点、验收标准。

### 5.1 P0 — 直接改善陪伴体验核心

#### P0-1 记忆类型学改造：加入 affect/relation 类别 【SYN，反用 TDB】

- **痛点**：B2。重要性纯规则（负面+0.3/正面+0.1）；情绪事件与关系里程碑没有专属容器，和普通闲聊同等衰减速度。
- **方案**：复用现有异步 enrichment 的单次 LLM 调用，在 raw 证据完成落库后增加五类分类与 importance；不得在主写入前新增 LLM：
  - `fact`（事实：生日/地址/职业——保留现有关键词直通 permanent 逻辑）
  - `event`（客观事件，对应 TDB episodic）
  - `affect`（**情绪事件：触发源 + 情绪 + 小妲的回应方式 + 效果**）
  - `relation`（**关系里程碑：称呼变化、承诺、"爸爸说过的雷区/禁忌"**）
  - `instruction`（习得规则；本批只分类，不自动接入规则执行通道）
- `affect/relation` 类 importance 基线更高（0.6 起），FSRS 相位直升 reinforced；`fact` 维持关键词直通 permanent。分类失败回退 `event`，保留规则分，raw 记忆不得丢失。
- 重要性打分由 memory_encoding 槽位的 LLM 在分类时顺带给出（0~1），规则分作为不可下调的兜底下限。
- **落点**：`memory/_memory_encoder.py`（enrichment 分类与严格解析、_estimate_importance 兜底）、`db/ddl_schema.py` / `db/legacy_migrations.py`（v31 非破坏迁移）、`doctor/memory_schema_readiness.py`。
- **验收**：CI 用确定性 fixture 覆盖五类、非法枚举、NaN/Inf/bool/越界 importance、超时与取消；断言 affect/relation importance≥0.6、fact permanent、失败不丢 raw。预生产另用固定 golden dataset 做离线模型评估，目标正确率≥90%，随机模型输出不作为普通 CI 门禁。

#### P0-2 冲突消解四动作协议（写入侧去重）【TDB batchDedup】

- **痛点**：B1。ADD-only 导致"我搬到上海了"之后旧记忆"住在北京"不消失，矛盾双记忆可能同时注入；表无限膨胀。
- **方案**：借鉴 `l1-dedup.ts::batchDedup` 两阶段——①新知识异步召回同 scope 的 active topK=5 候选；②严格校验的批量 LLM 输出 store/update/merge/skip。raw 证据 append-only，四动作只作用于 `is_raw=0` 知识层；LLM/embedding 在事务外，动作与审计在单一写事务内，索引经 outbox 收敛。
- **约束**：`ConflictSupersession.detect_conflicts` 只识别数值 token 冲突，`apply_supersession()` 明确是无持久化 stub，不能复用为执行器。P0-2 使用独立 reconciliation job/action/provenance 模型，并以 shadow 为默认模式。
- **落点**：`memory/` reconciliation 组件、`db/ddl_schema.py` / `db/legacy_migrations.py`（v32）、`core/background_tasks.py`，以及所有检索通道的统一 active 可见性过滤。
- **验收**：注入"住在北京→搬到上海""喜欢猫→其实更爱狗"序列，断言旧知识 superseded 或 merged 且所有普通检索通道不再返回矛盾双条目；LLM 失败时 raw 保留并 fallback store，shadow 模式结果与现状一致。

#### P0-3 工具轮流式状态推送（break 思想）【AST break 信号】

- **痛点**：B5。`main_path.py:646` 一挂工具 WebUI 就整段返回；验收循环期间用户干等。
- **方案**：不能简单删除 `not tools`。当前 `ModelRouter.chat_stream()` 只暴露文本 `str`，会丢失结构化 tool calls。先在既有 transport 契约上增加 tool-call delta 与 turn result，新增版本化 `stream_event v1`；旧 `chat_stream()` 保持兼容。标准/DSML 工具调用、多轮 verification、finish_reason、fallback 与取消都必须保真。
  1. WebUI：预览事件带 `msg_id/seq/turn/tool_call_id`；`final` 仍是唯一权威结果，终态后拒绝迟到预览。
  2. DSML 使用跨 chunk 有界过滤状态机，工具协议文本不得泄漏到 UI。
  3. QQ：统一 `QQReplyBudget(max_total=5)`；ACK、SUB_STARTED/typing、最多一条延迟进度、正文和媒体共同记账，始终保留至少一条正文额度。
- **落点**：`llm_gateway/transports/`、`llm_gateway/router_execution.py`、streaming/verification/tool handler、`web/ws_hub.py`、Vue chat store 与工具卡、QQ adapter。
- **验收**：标准与 DSML 多轮工具均有状态和后续文本增量；seq/终态/取消幂等；final 与非流式业务结果一致；QQ 任意路径总发送数≤5。

#### P0-4 群聊上下文修复：GroupChatBuffer + 攒批 ICL【AST group_chat_context】

- **痛点**：B7。@了也失明：群消息零留痕、不同成员 history 互相隔离、每次 @ 都是孤立单轮。
- **方案**（在官方 botpy 协议内，不需要 NapCat）：
  1. 建有界 per-group `GroupChatBuffer`，只记录机器人实际收到的 @ 消息；官方 SDK 不提供昵称，使用进程内群级稳定别名，禁止注入 openid 尾号；
  2. snapshot 仅取当前输入之前的条目，以独立 `<group_recent_context>` system/volatile 块注入；成功后按 watermark 清理，失败/取消/降级保留；
  3. **保持现有“仅主人当前轮”边界**：成员级个人 AgentContext/scope 不改成群级共享 history；主人当前输入与回复可进入个人记忆，非主人和群 buffer 均不得进入主人 portrait/episodic；
  4. 群审计复用 `conversation_logs.source/session_id/request_context_json`，无需新 `group_channel` 列；日志不保存 member_openid。
- **落点**：新建 `agent_core/group_context.py`，扩展 QQ adapter、消息构造和后台日志/个人编码策略；不新增数据库列。
- **验收**：同群 A@ 后 B@ 能引用 A、不同群隔离、当前输入不重复；主人当前轮仍可编码；非主人和 buffer 不进入主人画像/记忆；FIFO/token/TTL/watermark/失败保留均有测试；只验收已收到的 @ 消息。

### 5.2 P1 — 结构性改进

#### P1-1 场景叙事层：蒸馏产物回写 MEMORY.md【TDB L2 思想轻量版】

- **痛点**：B4。MEMORY.md 静态人格文件只能手工维护，与自动记忆割裂。
- **方案**：不做"LLM 操作文件系统"重方案；把周期蒸馏（`_memory_maintenance.py:98 distill_old_memories`）产物从平铺条目升级为**按关系主题组织的叙事段落**（核心叙事 Trigger→Action→Result / 演变轨迹 / 待确认矛盾点三段式模板），自动追加/更新到 MEMORY.md 专属章节（或 workspace/scenes/*.md 走 scene bucket 注入）。heat 简化为"引用次数 + 最近更新时间"，超长强制合并最旧段落。
- **落点**：`memory/_memory_maintenance.py`、`prompt_builder.py:296`（若走 scenes 文件）、MEMORY.md 格式约定写入 CLAUDE.md。
- **验收**：连续运行两周后 MEMORY.md 出现自动维护的关系叙事段且与人工段落共存不互删；mtime 缓存失效正常（60s TTL 机制天然兼容）。

#### P1-2 Warm-up 指数阈值 + 下行定时器【TDB pipeline-manager】

- **痛点**：B3。固定 history≥4，新用户首轮关键信息沉淀慢。
- **方案**：编码触发阈值改为 warm-up 序列 1→2→4→8（封顶 6），每次成功编码后翻倍；蒸馏/画像 consolidate 的周期调度改 downward-only timer（只能提前不能推迟）。
- **落点**：`core/background_tasks.py:317`、`emotion/portrait_manager.py`。
- **验收**：新会话第 1 轮结束即产生首批记忆；长会话编码频率收敛到约每 6 轮一次。

#### P1-3 通道能力声明表【AST PlatformMetadata】

- **痛点**：B6 及散落的通道特判（空 status_notify 函数、全局布尔、切片参数硬编码多处）。
- **方案**：建 `channel_capabilities.py`，每通道声明 `{streaming, typing_hint, max_segments, proactive}`；流式降级决策集中一处。为将来加微信/WS 通道铺路（wechat_bot_adapter.py 已存在，正好受益）。
- **落点**：新建 `channel_capabilities.py`、`config_constants.py:240`、`qq_bot_adapter.py`、`channel_adapter_base.py`。
- **验收**：删除各 adapter 内散落的流式能力判断分支，全部读能力表；ruff + critical 测试全绿。

#### P1-4 标点切句替代字数切片【AST aiocqhttp fallback，改良版】

- **痛点**：B6。300 字符硬切腰斩句子，拟人感差。
- **方案**：切片算法改为"标点边界优先（。？！~…）+ 目标长度区间（200~350 字符）"，保留片间随机 0.8~1.2s 打字节奏与 markdown 代码块闭合逻辑。**只做一套**分段实现，避免 AstrBot 两套分段并存之弊。
- **落点**：`channel_adapter_base.py:69 _split_text_by_bytes`、`qq_bot_adapter.py:1274`。
- **验收**：构造含代码块/长句/短句混合回复，断言所有片段以标点收尾且 ≤400 字符；配额合并逻辑回归通过。

#### P1-5 群聊唤醒判定单点化 + 冷却限流 + 概率插话【AST WakingCheck / RateLimit / need_active_reply】

- **痛点**：B8。群聊只有 @触发 + ACK，无冷却限流、无概率自然搭腔。
- **方案**：
  1. 收敛判定为 `group_wakeup.should_respond(msg) -> {wake_type, stripped_text}` 单点（现阶段 wake_type 只有 at_mention，为将来引用回复/唤醒词留口）；
  2. per-group 冷却：时间窗内最多响应 N 次（默认 3），超出走 stall 排队（保序）而非丢弃；
  3. 概率主动插话：白名单群 + 冷却满足时 random < p（默认关，WebUI 可开）触发 nudge_engine 式搭腔——"活物感"的关键，务必默认保守。
- **落点**：新建 `agent_core/group_wakeup.py`、`qq_bot_adapter.py:787/:794`、`config/webui_overrides.json` 经 config_service 暴露开关。
- **验收**：模拟高频 @ 场景断言第 4 次起进入排队且顺序不乱；概率插话关闭时行为与现状完全一致（零回归风险）。

### 5.3 P2 — 观察前三批效果再定

#### P2-1 群史检索工具【AST get_group_message_history】
群聊 buffer 落库后注册工具（limit/keyword/sender 过滤），让小妲能自己翻群聊记录。落点：`tools/` 注册 + `config/workspace/TOOLS.md` 使用规则。注意 DSML 模式需同步加进 `text_utils.py FAKE_XML_TOOL_PATTERN`。

#### P2-2 承诺追踪 Commitment Ledger【SYN】
`relation` 类记忆中单独标记双向承诺（小妲答应的事/爸爸提到的进行中事项），接入 Volatile 层待办通道 + 定时回忆笔记（run_scheduled_recall），支撑时间敏感的主动关怀（"上次你说项目要验收，后来怎样啦？"）。这是陪伴感与静态画像的最大差异点，且 FSRS retrievability 天然支持"临近事件不衰减"定制。

#### P2-3 关系温度连续变量【SYN】
ProfileStore（已支持版本化 + get_as_of 时间点重建）增加亲密度/互动频率基线/当前情绪基调等连续字段，由画像 consolidate 顺带更新，注入 dynamic prompt。让"关系"成为一等公民。

#### P2-4 KV-cache 注入分区实验【TDB】
若 MiMo API 支持 prompt caching，试验把 `<memory_retrieval>` 从第二条 system 消息移到 user 消息前缀（动态部分），Stable 层保持 system 尾部不动。收益不确定，先压测。

#### P2-5 NapCat/OneBot 双轨评估【AST aiocqhttp】
真正的全量群消息 + 自由主动发言需自建 NapCat 客户端。部署形态变更（多一个常驻进程，风控自负）。adapter 层已够薄届时加 adapter 不难，但**先用 P0-4/P1-5 在官方 API 内榨干价值再议**。

---

## 6. 明确不采纳清单

| 来源 | 不采纳项 | 理由 |
|---|---|---|
| TDB | Node22/Redis/COS/Proxy/Hub 多服务架构 | Orange Pi 单机跑不动也没必要，本地主权优先 |
| TDB | 五维隔离（team/user/agent/session/task） | 单主人单 Agent，现有三级 scope 已冗余 |
| TDB | 无时间衰减检索 / priority 不参与排序 | 本项目 FSRS×相似度 + recency boost 更优，倒退不可取 |
| TDB | 提取 prompt 排除纯主观感受 | 与陪伴场景根本对立，反其道而行（P0-1） |
| TDB | LLM 操作文件系统的 L2 全量实现 | 单机场景过重，取"叙事段落回写"轻量版即可 |
| AST | 九阶段洋葱管线全家桶 | 本项目调用链已成型且带媒体/情绪/熔断定制，重写风险 >> 收益 |
| AST | STREAMING_FINISH 三层防重复协议 | 隐晦难排查；保持 final 兜底 + 预览旁路的简单模型 |
| AST | 流式路径跳过装饰阶段 | 行为矩阵不一致是它自己承认的坑；渐进增强不替换 ProcessResult |
| AST | unique_session 字符串拼接反解（`{sender}_{group}` 再 split("_")） | ID 含下划线即碎的反模式；群上下文用结构化 GroupChatBuffer 传递 |
| AST | 硬编码 sleep(1.5) 分句 + 两套分段并存 | 参数化进能力表，只留一套实现 |
| AST | 4536 行巨型 config dict | 本项目三套配置体系已够复杂，不再引入同类债 |

---

## 7. 实施路线图与风险

### 7.1 批次

| 批次 | 内容 | 改动面 | 预估规模 |
|---|---|---|---|
| 地基层 | 修正 `eba85d91` adapter 模板半成品 | 请求类型/异常边界/TTL/分段恢复 + 契约测试 | 中 |
| 第一批 | P0-1 记忆类型学 | v31 + enrichment/FSRS/回填 | 中 |
| 第二批 | P0-2 四动作消解 | v32 + reconciliation/provenance/outbox/检索可见性 | 大 |
| 第三批 | P0-3 结构化工具流式 | transport/router/verification/WS/Vue/QQ budget | 大 |
| 第四批 | P0-4 群聊 buffer/ICL | QQ adapter/group context/审计与个人记忆边界 | 中大 |
| 后续批 | P1/P2 项逐个评估 | — | 按需 |

### 7.2 风险与对策

1. **schema 迁移**（P0-1 v31、P0-2 v32）：走 ddl_schema + legacy_migrations 既有迁移阶段，迁移只增不删；生产库先做只读 readiness 检查并在外挂盘 btrfs 上由运维单独快照。P0-4 复用 conversation_logs 现有字段，不新增列。
2. **LLM 成本上升**（P0-2 dedup、P0-1 分类打分）：全部挂后台任务走既有空闲让路机制（_chat_idle + semaphore），主 chat 开始时可取消——现成基础设施直接复用。
3. **群聊隐私边界**：P0-4 明确"非 master 发言只进群上下文，不进个人画像/记忆"；实现时加单测锁死该边界。
4. **前端改动**：凡动 web/frontend 必须重新 build 并提交 web/dist，否则 pre-push 门禁拦截（仓库既有规则）。
5. **灰度与回滚**：`MEMORY_TYPE_ENRICHMENT_ENABLED=false`、`MEMORY_RECONCILIATION_MODE=shadow`、`STRUCTURED_STREAM_EVENTS=false`、`GROUP_CHAT_BUFFER_ENABLED=false` 为初始策略；关闭开关回到现状。迁移只增不删，不通过删除 schema_version 回滚。

### 7.3 总验收指标（陪伴体验向）

- 新用户首次对话结束后，第二轮即能正确使用其称呼/喜好（warm-up 生效）。
- 矛盾陈述（搬家/口味变化）一周后检索结果中旧值不再出现（消解生效）。
- 带 2 个以上工具的请求，WebUI 首个可见反馈 < 3s（状态推送生效）。
- 同一群内连续两次 @，第二次回复体现第一次的内容（ICL 生效）。
- MEMORY.md 在无人干预情况下持续演化且人工段落完好（叙事层生效）。

---

*调研原始素材：本文档第 3/4 节为浓缩版结论，完整探索记录见三个并行研究任务输出（2026-08-23）。参考仓库位于 `/mnt/kioxia/github-repos/`（外挂盘，未挂载时本文档代码引用不可复核）。*
