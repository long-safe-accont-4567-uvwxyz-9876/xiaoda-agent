# 分支全量 Review 台账 —— 2026-08-25

perf/rust-hybrid-poc 领先 origin 27 提交的多轮审查收束记录。
方法：每轮 2-4 个子 agent 并行（测试实测 / AST 静态比对 / 线上活体验证 / fresh-context 对抗复查），
重点为后端功能代码，裁决均以实际运行为证。

## 一、逐提交裁决

| 提交 | 内容 | 裁决 |
|---|---|---|
| `4a7721d4` | 后端三小修（ndcg/probes scope/Envelope） | ✅ 三处均为真修复；实测缓存命中修复前必 500 |
| `728dfd1d` | WebUI 玻璃提亮 + dist | ✅ 线上产物与提交一致 |
| `b92c14b0` `588ff31d` `6c00eedb` | P0-1~P0-4 记忆类型学/结构化工具流/群聊buffer/门禁 | ✅ 符合 48a57795 规格预期：双灰度默认关闭有证据、取消回收无双重发送竞态、三端契约同源 |
| `ff65841e`(+`3ec7edff`) | RAG治理+PromptA/B+情绪统一（18k 行） | ✅ 可留主干：主路径字节冻结完好、A/B 生产隔离结构性成立、情绪五层契约锁 |
| `1af8fc24` | degradation.py 清坟 | ✅ 零引用复验成立 |
| `48423e73` | lint 批次（309 文件） | ✅ 可信任为纯清理：AST 全量对比零夹带、import 重排无一翻转加载序；唯 NUDGE_ENABLED 等 truthy 集放宽属有意变更 |
| `b87b204a` | env_flag 收敛 | ✅ 默认值逐一保持、零收窄、特例完好；注意与父提交是原子对（中间态 import 即炸） |
| 其余 docs/gates/test 棘轮类 | 文档与门禁 | 低风险，未逐行审 |

## 二、本轮落地修复（4 提交）

1. **`a900a97c`** 终审尾巴四连偿——discover 缓存命中补 local-ort 注入+刷新去重 / probe_vector 只读化（record_access 开关）/ ndcg ideal 截断回退修复 / 守卫测试（后被勘误，见 3）
2. **`15c34e7e`** 对抗复查三收口——`installed=[]` 兜底（原 UnboundLocalError 可炸整个 discover）/ refreshing 标志防搁浅（create_task 前置+invalidate 复位）/ 守卫测试合并加固。**含勘误**：a900a97c 的守卫测试在提交窗口被并行会话覆盖为其演进版，声称的位置参/分叉校验当时不在库
3. **`9beac192`** 工具 schema 情绪词表对齐——tts_tools/_builtin_manifest/TOOLS.md（仓库+运行时副本）三处旧 15 词表全部改 EMOTION_VOCAB_SLASH 单一事实源派生，taxonomy 契约测试新增 4 断言防回流
4. **应急**：web/dist 一度在磁盘丢失（build 中途失败被清 outDir）致前端 404，已重建恢复

## 三、遗留尾巴（按优先级）

| 级别 | 事项 | 归属 |
|---|---|---|
| 中 | `web/routers/retrieval.py:87` 九个 RAG 键自建真值表缺 `"off"`，与 config_constants 对同一键真值表分叉 | 并行会话辖区（该文件在其 WIP 区） |
| 低 | intent.decompose 未设 `system_slot=True`，stage 校验空转 | 并行会话已有双槽重构在途 |
| 低 | golden 分类评估 90% 阈值纯手动、无自动化执行点 | 建议挂调度或手动跑留基线 |
| 低 | 探针与真实流量共享 query-cache namespace（300s TTL 内 sqlite-vec 单点故障可假阳性）；FSRS 懒迁移写不受 record_access 门控；评测页 prompt 模式仍 touch 记忆（注释声明有意） | 备案，暂不动 |
| 信息 | NUDGE_ENABLED/WECHAT_ILINK_ENABLED truthy 集合放宽为有意行为变更 | 发布说明补记 |

## 四、运行时验证记录

- 06:05 重启：startup.health all_ok total=11；discover 冷 2411ms→命中 10ms；探针 ok=true
- 09:54 全量重启（含并行会话 WIP）：预检 `import web.server` 通过后重启；all_ok total=11；agents=5；discover 冷 1692ms→命中 10ms 结构一致；vector 探针冷 18347ms（NPU embed 冷载）→热身 20ms 只读路径生效；前端新 dist 200；journal 近期零 traceback

## 五、方法论备注

- 双工作流并行时，**未跟踪文件不设防**：a900a97c 的守卫测试在被 git add 前被对方覆盖，导致提交信息与内容失实。教训：提交前 `git diff --cached` 逐文件过目，未跟踪新文件的归属要先约定
- 机械 lint 大批次的风险面 = F841 删除副作用 / F811 删错层 / E712 numpy 语义 / import 重排破坏 config 导入链；本次经 AST 对比法全部排除，该验证套路可复用

## 六、扩围第二轮（WIP 活体审查 + 真实聊天 E2E）

对象：并行会话 428 个未提交文件（实质改动 152 文件 966+/466-，已随 09:54 重启跑在生产）。

### 活体 E2E（真实 WS 聊天，非探针）

两条真实消息走完整主链路：人设语气回复 ✓、情绪标签落在**新 17 枚举**（greeting/love）且表情包目录映射正确 ✓、记忆检索诚实答"没有记录"无幻觉 ✓、单帧 final 模式与"结构化流默认关闭"灰度结论互证 ✓。
journal 观察：四检索通道全跑但 kg 通道 ~7.8s 偏慢（延迟主源）；emotion_llm 超时兜底按设计；`label_parse_failed fallback_to_alias` 归一链生效；`ws.close_failed` 双关闭小瑕疵[低]。

### WIP 审查裁决

| 域 | 裁决 | 关键发现 |
|---|---|---|
| core/memory/db/agent_core | **自洽可提交态** | 93 测试绿；字节冻结守卫成立；config 零纪律违规；唯一实质变更是 override 双槽重构（窄边界行为差异已识别） |
| web/security/emotion/tools/gateway | **自洽可提交态，两个提交时注意点** | 见下 |

- **[高·时点性]** `universe/engine.ts` 审查窗口内仍活跃编辑（09:52→10:15），dist 落后 src——此刻提交会入库半改源码+过期 dist；vite 构建不做类型检查，未定义标识符能过构建只在运行时炸。须等其收敛后重新 build 再提交
- **[低→必炸]** web/dist 新旧资产分裂（旧删未暂存/新未跟踪）：只 `git add -u` 会产出坏 dist，须全量 add+重新构建
- **[中·有意]** server.py 补回启动时 .env→凭证存储同步（修 915023d6 误删的真病）；语义须知：每次启动以 .env 覆盖凭证层，仅在 WebUI 轮换过凭证未回写 .env 的 key 会被覆盖
- **[中·修复但零测试]** insight.py create_memory 迁移（旧代码传不存在参数必然 500，属修复）；建议补一条 API 用例
- 安全面无松动：auth/metrics/X-Confirm/SSRF 全部原样，纯 isort 化

## 七、扩围第三轮（Rust POC + 协议对抗 + DB/安全活体）

### Rust hybrid POC —— 前提被推翻的关键发现

**`.env` 已设 `RUST_HYBRID_ENABLED=true`（05:13），POC 正在 2437 节点生产库上实际服役**（>MIN_NODES=500 门控），推翻此前所有轮次的"默认未启用"纸面前提。

质量实测：lib.rs 零 unsafe、无跨 FFI panic 路径、契约门（v2 版本+符号表双校验）有效、20 项等价测试全绿、纯计算子进程实测开关两侧输出一致（含坏 JSON/负 IDF/剪枝/空输入）、.so 非陈旧产物。

放行前置清单：①补 recall() 全链路开关 A/B 终榜等价测试（最大缺口，现有仅单通道对比）；②键序依赖（Rust HashMap 序 vs Python dict 序）目前靠下游 `(-score,id)` 排序兜底，无回归钉；③重复种子语义怪点（已知种子后者覆盖前者，与注释"合并累积"不符）；④二进制新鲜度自证（编译期嵌 git hash）；⑤ImportError 兜底分支与 env_flag 白名单漂移（只认 "1"）。

### WS 协议对抗活体（6/6 实质通过）

重放同 msg_id → `DUPLICATE_IN_FLIGHT` 拦截 ✓；畸形 JSON 不炸连接 ✓；未知类型静默 ✓；abort 中止生效（error 帧替代 final，任务真取消）✓；幽灵 abort 不炸 ✓；中止后新消息正常走完 ✓。
注意点：服务端有**应用层心跳**（JSON ping/pong，~40s 超时踢线），自研客户端必须应答；abort 后以 error 帧而非 final 收束是当前契约。

### 生产 DB 活体体检

agent.db 与 agent_vec.db 双库 `integrity_check=ok`、WAL 模式、103/21 张表；`migration_state` 显示 **v32 已落生产**（P0-1/P0-2 schema 生效中）；episodic_memories=2437、conversation_logs=2546。

### 安全控制活体抽测

X-Confirm 守卫接口无确认头一律 HTTP 400 强制拒绝 ✓；/metrics 无 token 401 ✓；错误密码 401 且正确密码立即可登录（无误锁）✓。
