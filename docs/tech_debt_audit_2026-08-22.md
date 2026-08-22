# 小妲 AI Agent 技术债排查报告 · 对账修订版（2026-08-22）

> 本文是当日早间排查报告（存档 tech-debt-audit-2026-08）的修订版。
> 修订方式：逐笔 review 报告发布后落地的偿还提交，验证"是否还到位"，并修正原报告过时结论。
> 复核基线：commit `6f35d0a9` 时点。复核手段：全仓 grep 引用面、新旧补丁逐段 diff、man 页实证、真实跑测试。

## 一、总览

原报告 12 项债：**7 项已偿还且验证到位**，2 项部分偿还，3 项未动；review 另发现 **2 个偿还引入的新问题**（1 个已修复、1 个待修）。

| # | 原债项 | 原级别 | 状态 | 提交 |
|---|--------|--------|------|------|
| 1 | 死代码 1119 行 + 幽灵建表 | 高 | 🔶 死代码已删✅；workflow_v2 定性修正（见四.5） | `47a1fa5e` |
| 2 | botpy 私有 API 耦合 + patch 双份漂移 | 高 | ✅ 已收敛（带回归，已修复，见三.1） | `3d5c15de` |
| 3 | DB 迁移三套并存 + 同步 sqlite 混用 | 高 | 🔶 迁移注释级收敛✅；rate_limit 同步 IO 未动 | `55818bd8` |
| 4 | ONNX 91MB 在 git | 高 | ✅ 已出库 + 检索默认切远程（含降级链与显式告警） | `22f1c96a` |
| 5 | 全集测试 continue-on-error | 中高 | ❌ 未动（仍 5 处） | — |
| 6 | web/dist 入库 | 中高 | 🔶 tsbuildinfo 出库✅；dist 有意保留待决策 | `42def5c2` |
| 7 | vue-tsc 装而不用 | 中 | ✅ 已接入 CI，本机实测全绿 | `dcd88caf` |
| 8 | WebUI 无进程托管 | 中 | ⚠️ 脚本有 bug 且未应用（见三.2） | `de795d28` |
| 9 | 巨型文件 | 中 | ❌ 未动 | — |
| 10 | 部署三轨并行 | 中 | ❌ 未动 | — |
| 11 | 裸 except / global 存量 | 中 | 🔶 6 处静默吞异常补了诊断日志 | `6f35d0a9` |
| 12 | 文档数字手工维护等 Minor | 低 | 🔶 CRLF 已解（`.gitattributes`）；README 数字未动 | `719b5f71` |

另：原报告"web 层直读 `_ACTIVE_BOT` 穿透抽象"已在 `95dca495` 清零。

## 二、逐提交 review 结论

### `47a1fa5e` 四个死模块删除 — ✅ 到位
- 全仓 grep（*.py/*.md/*.spec）：QQMediaMixin / QQSessionMixin / qq_bot_streaming / qq_bot_patches 零残留引用（仅 tests/__pycache__ 陈旧 .pyc，无源码命中）。
- channel_adapter_base.py 对已删 qq_bot_streaming 的注释提及同步修正。
- 活实现（_upload_base64/_compress_image、session 缓存）确认仍在 qq_bot_adapter.py。

### `1bd39963` codeact/ + CmdConfirmDialog.vue — ✅ 到位
### `df1341ca` v06_cognitive.sql 孤儿 SQL — ✅ 到位（超预期）
测试改造为"DatabaseManager 全量迁到 v27 后断言 5 表 3 列 9 索引"，比原来读 SQL 文件断言更能防回归。实测 test_v06_migration.py 7 passed。

### `620449d9` botpy 日志重定向 — ✅ 到位
仓库根目录 botpy.log* 已清零；重定向逻辑后被并入 botpy_compat.py 统一维护。

### `3d5c15de` botpy 补丁收敛 — ✅ 结构到位，但有回归（已修复）与文档损失
正确性核验：
- 四补丁（_is_system_event/_send_heart/on_closed/_pool_init）与旧内联版**逐段 diff 语义等价**，getattr 兜底属小加固；删除 multi_run 死分支的理由成立（async def 协程恒 truthy）。
- 幂等安装（_PATCH_INSTALLED）+ reset 钩子供测试隔离。
- `check_sdk_compat()` 探针 + docstring 升级风险清单——原审计"SDK 升级无 checklist"解除。
- 调用点正确：qq_bot_adapter.py:68 import 时安装、:188 首个 Client 实例化前重定向日志。
- 实测：tests/test_botpy_compat.py 5 passed。

两个问题见第三节。

### `719b5f71` .gitattributes — ✅ 到位
构建产物（web/dist/**、build/**、dist/**）排除行尾控制，避免归一化噪音，处理正确。

### `42def5c2` tsbuildinfo 出 git — ✅ 到位（收窄有据）
dist **有意保留**入库：SETUP.md 明示裸机/离线部署依赖仓库内预构建产物。原报告第 6 项要彻底关闭需先改部署模式，不能直接删。

### `de795d28` WebUI systemd 托管脚本 — ⚠️ 有 bug 且未应用
- 参数链验证通过：start-linux.sh `"$@"` 透传 → agent.py wd_parser 支持 --port/--host/--mode。
- **bug**：unit 里 `User=%u` 在 system 管理器下恒解析为 "root"（本机 man systemd.unit(5)，systemd 252 原文："In case of the system manager this resolves to 'root'"）；`Environment=HOME=%h` 同理解析为 /root。WebUI 将以 root 运行，违背脚本意图。修法：安装时 `RUN_USER=$(id -un)` 写死进 unit。
- **未应用**：本机无 xiaoda-webui 单元，WebUI 仍是手动 setsid 进程。脚本交付 ≠ 托管落地。

### `dcd88caf` typecheck 接入 CI — ✅ 到位
ci-tests.yml 新增独立 frontend-typecheck job + build-release build 前置门禁；JSpacePanel n-input→n-input-number 是真实类型修复。本机实测 `npm run typecheck` 全绿。

### `55818bd8` 迁移体系收敛 — 🔶 注释级收敛，可接受
legacy 唯一生产入口导航注释、idempotent_migrator 弃用声明、repair CLI 边界声明。复核确认 idempotent 生产零引用（db/index_manager.py 仅 docstring 提及非 import；database.py:23 import 的 index_manager 本身是活代码）。原报告"TEXT vs INTEGER schema_version 冲突"风险随生产零引用实际解除。物理上三文件仍并存，靠注释约束。

### 会话期间新落地（未及入原报告）
- `95dca495`：web 层 5 处直读 `_ACTIVE_BOT` 清零，统一走 get_active_bot()。
- `6f35d0a9`：jspace/npu/vector 析构与配置降级路径 6 处静默吞异常补诊断日志。
- `22f1c96a`：BGE 模型权重出 git + 检索默认切远程 API（同时消解原报告"模型缺失 load() 直接抛 FileNotFoundError"）。
- `d4631fea`：删除 chat_ultra 1M 阈值永久 skip 死测试（原报告第五节遗留项关闭）。

### 对账批偿还（本会话）
- `1aa2cade`：test_qq_bot_4008.py 引用修复（见三.1）。
- `661a2f29`：on_closed 补丁根因考古注释补回 botpy_compat.py（见三.3）。
- `924d279b`：install-webui-service.sh 用户解析修正（见三.2）。
- `423fa7f3`：rate_limit 持久化 except 收口 sqlite3.Error（见四.2）。

## 三、Review 发现的问题

### 1. 【已修复 · `1aa2cade`】test_qq_bot_4008.py 漏改——会话失效回归防线曾失效
`3d5c15de` 把 `_patched_on_closed`/`_original_on_closed` 搬到 botpy_compat.py，漏改 tests/test_qq_bot_4008.py → 3 项测试 AttributeError 失败。该测试正是 CodeRabbit 修复（4008 限频不得清 session）的回归防线，失效期间 4007/4008/4009 会话语义无守护。
**修复**：改引 botpy_compat 符号，test_qq_bot_4008 + test_botpy_compat 共 8 passed。全仓 grep 确认仅此一处旧符号引用。

### 2. 【脚本已修 · `924d279b`｜部署仍未落地】install-webui-service.sh `User=%u` root bug
%u/%h 解析问题已在安装时写死真实用户修复。**仍开放**：本机未实际安装 xiaoda-webui 单元，WebUI 仍是手动 setsid 进程——"脚本交付 ≠ 托管落地"，需择机执行安装并验证开机自启。附带发现：CLAUDE.md 记载的生产单元 qq-agent.service 在本机不存在（`could not be found`），QQ Bot 进程当前也未运行——文档与现实不符，需校正或重建单元。

### 3. 【已修复 · `661a2f29`】botpy_compat.py 丢失关键考古注释
旧内联版记录的根因——"botpy `_INVALID_RECONNECT_CODE=[9001,9005]` 不含 4009 → RESUME 已超时 session → 在线却收不到任何消息（07-28 实测）"——已补回补丁定义处，并附 SDK 修复后的处置指引（补丁应删）。

## 四、仍未偿还未动（按风险排序）

1. **continue-on-error 制度化**：ci-tests.yml 4 处 + build-release.yml 1 处，全集 flaky 门禁未恢复。
2. **rate_limit 审计修正 + 已收口（`423fa7f3`）**：原审计"请求路径同步 connect 阻塞事件循环"复核**不成立**——周期保存自 `8ceb4dcc`(2026-07-04) 起已 run_in_executor 挪出事件循环，init/load 仅启动时一次。真实问题是 except 未含 sqlite3.Error（锁库/损坏时穿透），已修复。
3. **workflow_v2 定性修正**：`web/routers/workflows_v2.py` 已挂 server.py:1077（原报告"仅包内互引"过时），但前端 api/index.ts 只调 `/workflows` v1，v2 仍零消费者；legacy_migrations v27 每次启动照旧建表。处置二选一：下线 v27+v2 路由，或推前端接入后转正。
4. **web/dist 96 文件入库**：出库前置条件是解决离线部署模式（SETUP.md 承诺），否则继续付 hash 翻动成本。
5. 双进程各持 AgentCore 并发写 SQLite、外挂盘 vfat 下 WAL 降级风险，未动。
6. 慢性病类（巨型文件、1128 裸 except 存量、i18n 双字典、three/echarts/3d-force-graph 三库并存）未动。

## 五、进行中 WIP（多会话并行实锤，归属标注）

- **另一会话 A（海报功能）**：web/agent_registry.py `wallpaper_poster()` + web/routers/agents.py `_extract_video_poster()`（ffmpeg 首帧抽帧）+ AgentBackdrop.vue/agents.ts/LoginView.vue + 全量 dist 重建。功能自洽，是壁纸第二阶段延续。仍待验证提交。
- **会话 B（ONNX 出库）已完成**：`22f1c96a`。
- **本会话（对账批）**：`1aa2cade` / `661a2f29` / `924d279b` / `423fa7f3` + 本报告。

## 六、优先级 v2

- **P0（本周）**
  - ~~test_qq_bot_4008 修复~~ ✅ `1aa2cade`
  - ~~install-webui-service.sh User= 修复~~ ✅ `924d279b`；**剩余**：实际安装启用 xiaoda-webui 并观察一次重启周期
  - ~~根因考古注释补回~~ ✅ `661a2f29`
  - 校正 CLAUDE.md 的 qq-agent 单元记载或重建单元。
- **P1**
  - ~~ONNX 出库收尾~~ ✅ `22f1c96a`（会话 B）
  - continue-on-error 收紧：先把全集 flaky 清单列出来再谈去 true；
  - ~~rate_limit 同步 IO~~ ✅ 复核为误报 + except 收口 `423fa7f3`；
  - 海报功能 WIP 验证后提交（会话 A）。
- **P2**
  - workflow_v2 生死决策（下线 or 推前端接入）；
  - dist 出库决策（先改 SETUP.md 部署模式）。
