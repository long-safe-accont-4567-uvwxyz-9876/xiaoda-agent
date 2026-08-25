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

## 八、扩围第四轮（缺口补码 + 并发隔离 + 门禁自检 + 后台巡检）

### rust_hybrid 最大缺口收口

新增 `test_engine_recall_full_pipeline_ab_equivalence`（`8211fce7`）：链式图夹具上开关两侧 recall() 终榜 id 序+分数逐位一致，覆盖 direct→spreading→RRF 完整链路。此前仅单通道对比的审计缺口关闭，21/21 绿。

### 并发会话隔离活体对抗

3 路独立 WS 连接同时聊天（要求只回指定代号）：苹果/香蕉/樱桃**零串扰精确路由**，wall≈max 单路耗时=真并发无串行化。

### 仓库门禁全量自检（CONTRIBUTING-AGENTS.md push 前预跑）

| 门禁 | 结果 |
|---|---|
| ruff | ✅ 145/146 |
| broad-except | ✅ 1157/1157（贴线无余量） |
| todo-ratchet / lazy-imports | ✅ 3/3、1418/1418 |
| i18n | ✅ zh=en=1488 完全一致，CJK 棘轮在基线内 |
| **giant-file** | **✗ FAIL：universe/engine.ts 917 行 >900 且未登记赦免**——并行会话活跃文件，此刻 push 必被拦；须拆分或进 allowlist+ratchet |

### 后台子系统 journal 巡检（09:54 起 ~2h）

mail 轮询 38 次、问候/蒸馏/梦境/nudge 有活性证据（14 hits）；53 条 WARNING 集中于已知慢检索通道（kg/spreading/child）与 stage_slow，另有 2 次 heartbeat_timeout 系本轮测试客户端未应答心跳所致，非产品缺陷。

## 九、扩围第五轮（鉴权全扫 + 崩溃演练 + 备份审计 + 子系统细查）

### 未鉴权路由全量扫描（OpenAPI 292 操作实测）

282 个操作无 token 一律 401，**零漏网**；8 个 200 全属设计内公开面（login/first-run/version/ping/brand/公共壁纸/system-os/wechat-status）。甄别结论：
- `/auth/recover-question` 未鉴权可见密保问题文本=设计取舍（注释声明与 login 同级）；当前未配置问题故零泄露；`/auth/recover` 答案尝试有 `_check_recover_rate_limit`+失败记录，实测连错 3 次 400 拒绝 ✓
- [低] `openapi.json` 无鉴权可读（292 接口形状全暴露）+ `/system/os`、`/wechat/status` pre-auth 指纹——信息面收敛可议

### SIGKILL 崩溃恢复演练 ✅

kill -9 主进程 → systemd `Restart=always` 自动拉起 → **t+9s 新 PID active、t+18s startup.health all_ok total=11** → 双库 integrity ok（WAL 干净恢复）→ 聊天链路 14.3s 内正常出回复。韧性证据完整。

### 备份就绪审计 —— **[中·新发现] 生产 DB 零自动备份**

agent.db(210MB)+agent_vec.db(73MB) 位于消费级 U 盘且**无任何定时备份**（无 cron/timer/script）；唯一 .bak 为 08-16 手工件（9 天前），auto-update.sh 的 backup 仅覆盖代码目录。建议：systemd timer 每日 `sqlite3 .backup` 至异盘+保留 N 份轮转。

### 微信/邮件子系统细查

ilink 长轮询循环正常（getupdates 18s 周期，0 错误）；mail.run_agently 38 次 0 错误。

## 十一、子代理"后台执行+主动推送"模型落地（review 直接转化）

审查确认原实现为同步工具调用（当轮阻塞等待、超时取消丢工作），用户期望的
"分配→后台执行→结果返回→主动推送"四环节中后两环缺失但投递基建已存在。
已实现 `c811c148`：

- delegate_task 新增 background=true：spawn 脱离当轮 + 受理回执即时返回
- core/async_delegation.py：注册表 + 按通道投递路由（qq/wechat/web/ws/cli）
- RequestContext.channel 打标贯通；无外层超时（修复超时丢工作缺陷）
- 健壮性顺带：显示名别名归一 + task 缺省回退用户原话

**活体验收 PASS**：受理回执 10.3s → delegate_result 主动推送 12.5s。
调试期间顺带发现并处理三个既有问题：①error_rule 自学习规则会把参数缺失失败
固化成预拦截（已清 rule#39；task 回退从根因消除复发）；②route LLM 30s 超时在
大上下文+51 工具下偶发触发降级（存量特性，备案）；③黑板缓存使重复任务秒回。

## 十、扩围第六轮（能力面活体 + X-Confirm 矩阵 + 浏览器冒烟）+ 一次误伤事故披露

### 主链路未测能力活体验证：4/4 PASS

| 能力 | 实测 |
|---|---|
| 工具调用链 | get_current_time 真实触发，回复含准确日期时间；calculator 验证 17×23=391 |
| **多代理委托** | delegate_task 生产端到端首证：委托小莉后以其人格原声返回，4 次 status 帧 |
| 斜杠命令 | /help 经主进程直通返回完整命令表，零延迟 |

### 全路由 DELETE 确认头矩阵（22 条实测）

12 条明确 X-Confirm 守卫(400)、5×422、3×404、**2×200 无守卫**：
- `DELETE /workspace`（撤销授权的设置开关，非数据删除）
- `DELETE /workspace/whitelist/{command}`（对不存在键幂等 no-op）
建议：按 CLAUDE.md"删除类接口一律 X-Confirm"契约补守卫，或在文档明示豁免清单。[低]

### ⚠️ 误伤事故披露

本轮扫描对 `DELETE /workspace` 的无确认头调用**真实执行了撤销工作目录授权**（生产 workspace.cwd 被清空）。原值经多源追溯不可恢复（loguru 文本日志不渲染 extra 字段），已按最强证据恢复为项目根目录 `/home/orangepi/ai-agent` 并验证持久化生效。教训：破坏性端点矩阵扫描前应先静态读 handler 判定副作用级别，设置开关类也需先备份持久化文件。

### 浏览器运行时冒烟

headless chromium 加载 WebUI：1280×800 非空白渲染（33 万色）、DOM 含登录标记 ✓。前端运行时层首次纳入实证。

## 十二、后台委托 v2（按用户定稿模型重做交付形态）

`0fe17c3e`：废除 v1 的受理回执措辞与 delegate_result 专用帧。定稿形态：
- 受理后主代理照常流式输出（中性受理语由模型自然措辞）；
- 子代理完成后结果交回**主代理本人**：router.route 转述成主代理口吻
  （20s 超时降级模板保底），走标准 final 帧 / QQ 微信主动消息发出；
- 新增 sub_agent_control 工具：status 进度一览 / abort 终止（CancelledError
  捕获+自然告知）/ interject 插话（共享列表→_chat_loop 每轮消费；未消费的
  剩余插话并入转述提示保证必达）。透传链 process→ctx→dispatch→chat→loop。

活体验收 PASS：受理 final 15.4s → sub_completed 27.9s → 转述 final 30.1s。
已知边界：route LLM 30s 超时抖动会偶发降级当轮回复（存量特性）；子代理自身
回答质量受 flash 模型影响波动，与投递管道无关。

### sub_agent_control 三操作活体终验（第十三章补）

- status：模型两次调用均返回注册表精确数据（任务编号/耗时/摘要）✓✓
- abort：目标任务已自行完成时诚实回报"无可终止"（cancel 语义由单测覆盖）✓
- interject：端到端 PASS——插话晚于任务完成时经转述兜底通道进入主代理汇报，
  用户指定的"旋转硬币类比"出现在最终推送文本中；运行中实时注入由
  _chat_loop 轮首消费逻辑承载（本轮因任务过快未触发，代码路径已实现）
- 优雅降级实例：小莉回答质量差时，主代理自动改为自己计算并致歉说明 ✓
