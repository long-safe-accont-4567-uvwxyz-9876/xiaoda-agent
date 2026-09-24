# 开源致谢与借鉴声明（ACKNOWLEDGMENTS）

> 协议核实日期：2026-09-18（基于各项目 GitHub 仓库 LICENSE 原文与官方公告）

xiaoda-agent 的架构与实现均为原创，但在设计过程中深入研究了多个优秀开源项目，从中借鉴了大量经过验证的工程思想。本文件如实说明借鉴来源、所参考各项目的开源协议，以及本项目与这些协议的合规关系。

## 一、MIT 协议项目

以下项目采用 MIT License。本项目仅借鉴其设计思想与机制（未移植源码）；若未来移植代码，将按 MIT 要求在副本中保留原版权声明与许可文本。

| 项目 | 仓库 | 借鉴内容 |
|---|---|---|
| **OpenWorker** | [andrewyng/openworker](https://github.com/andrewyng/openworker)（MIT, © 2024 Andrew Ng） | Self-Wake 挂起/恢复机制与三种唤醒触发、上下文压缩的 token 阈值自动触发（80% 窗口）、记忆召回调度（run-once-catch-up / skip-on-overlap）、权限五态模型、Approver 审批回调、Skill 渐进式加载 |
| **TencentDB Agent Memory** | [TencentCloud/TencentDB-Agent-Memory](https://github.com/TencentCloud/TencentDB-Agent-Memory)（MIT） | 记忆全生命周期管理对照研究、分层记忆引擎设计思路 |
| **Claude Agent SDK** | [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)（MIT） | SessionStore 会话存储抽象、ContextUsageResponse 上下文用量监控、PermissionMode 权限模式、SandboxSettings 沙箱配置的接口范式 |
| **openclaw** | [openclaw/openclaw](https://github.com/openclaw/openclaw)（MIT, © 2026 OpenClaw Foundation） | CLI 斜杠命令两级 TAB 补全、命令短别名（aliases）机制、声明式命令元数据表 |
| **Hermes Agent** | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)（MIT） | 子代理工具禁用列表（delegate 思路）、状态文件原子写入、凭证池、错误分类器、长回复流式上限策略 |
| **Everything Claude Code (ECC)** | [affaan-m/everything-claude-code](https://github.com/affaan-m/everything-claude-code)（MIT） | Hooks 系统整体设计 |
| **mind** | [Da7-Tech/mind](https://github.com/Da7-Tech/mind)（MIT） | 记忆扩散激活机制研究 |

## 二、Apache License 2.0 项目

以下项目采用 Apache-2.0。本项目仅借鉴其设计思想与算法机制（未移植源码）；若未来移植代码，将按 Apache-2.0 要求保留版权、许可与 NOTICE 声明并标注修改。

| 项目 | 仓库 | 借鉴内容 |
|---|---|---|
| **mem0** | [mem0ai/mem0](https://github.com/mem0ai/mem0)（Apache-2.0） | scope 三级记忆隔离、异步实体提取与反向链接、ADD-only 写入架构、Entity Boost 精排公式、六路混合召回管线 |
| **graphiti** | [getzep/graphiti](https://github.com/getzep/graphiti)（Apache-2.0） | 时序知识图谱机制：双时态事实窗口、实体摘要动态融合、知识溯源链、标签传播社区发现、BFS 多跳检索（经评估弃用 Neo4j 后端，以 SQLite + Python 自行适配实现） |
| **Letta / MemGPT** | [letta-ai/letta](https://github.com/letta-ai/letta)（Apache-2.0） | 三层记忆架构（core/recall/archival）的设计哲学；其虚拟上下文机制经评估未采用 |

## 三、Copyleft 协议项目（特别声明）

以下项目采用强 Copyleft 协议。本项目**从未复制、链接、派生或移植其任何源代码**，仅参考其公开文档与架构理念中的方法论思想（思想与方法本身不受版权保护），因此不触发相应协议的传染性条款。特此声明以划清边界。

| 项目 | 仓库 | 协议 | 借鉴内容 |
|---|---|---|---|
| **AstrBot** | [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)（AGPL-3.0） | 仅学习其公开的工程接口设计理念：流式降级决策、break 分段信号、平台能力声明位、群聊攒批 ICL、唤醒判定单点化 |
| **mazemaker** | [itsXactlY/mazemaker](https://github.com/itsXactlY/mazemaker)（AGPLv3 / PolyForm Noncommercial 1.0.0 双许可） | 仅参考其认知架构 Stage 分层思想，并针对情感陪伴场景重新设计 |

## 四、闭源产品与技术理念参考（无开源协议约束，主动致谢）

- **Trae**（字节跳动 AI IDE，闭源商业产品）：多代理编排**模式**——SOLO 任务绑定、交叉验证、顺序管道、辩论综合、竞选择优
- **Anthropic Claude Tool Search**（公开技术文档）：工具按需加载与混合检索理念（BM25 + Vector + RRF）
- **Obsidian**（闭源商业软件）：知识图谱宇宙视图的粒子星图视觉灵感（Fibonacci 螺旋分布与 HSL 闪烁为公开的数学与渲染技术）
- **LangSmith**（闭源商业服务）：Prompt 版本管理与发布标签的模型参考

**关于闭源借鉴的说明**：著作权保护"表达"而非"思想"——以上借鉴均停留在产品理念、功能模式与公开技术文档层面，相关功能、视觉与文案均为本项目独立实现，未复制上述产品的任何代码、图标、美术资产或文案，亦未进行任何形式的逆向工程。本项目与上述产品无隶属、合作或官方背书关系，相应商标权归各自权利人所有。此类致谢属于行业惯例（如 "inspired by" 标注），目的在于如实说明灵感来源并留存独立实现之证据。

## 五、出处待补

- **Fairydex**：免责声明写法参考（原始仓库地址未能定位，如权利人见此，欢迎通过仓库 Issue 联系补充署名）

## 六、合规声明

1. 上述借鉴均为**设计思想、接口范式与算法机制层面**的学习、调研与独立实现，未原样移植受版权保护的代码表达。
2. 各项目的调研过程、架构权衡与踩坑记录在本仓库 `docs/research/external-specs/` 有完整存档。
3. 本项目基于 MIT 协议开源；对本文件所列项目的任何未来代码级引用，将遵循各项目协议要求（MIT/Apache-2.0 保留版权与许可声明，Apache-2.0 附 NOTICE 并标注修改；AGPL 类项目继续保持零代码接触）。
4. 上述第三方项目的版权与许可以各自原始仓库为准。向所有开源社区的前辈致敬。
