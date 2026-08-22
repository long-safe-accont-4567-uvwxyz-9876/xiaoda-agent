# 巨型文件拆分规划（2026-08-22）

> 技术债四大专项 #4 的执行蓝图。当前 ≥800 行的后端文件 **30 个**（审计时 27，
> 代码持续生长），合计约 34K 行。本文档是后续专项会话的入口。

## 假阳性排除（无需拆分，长度天然合理）

| 文件 | 行数 | 理由 |
|---|---|---|
| tools/_builtin_manifest.py | 917 | 纯数据清单（BUILTIN_TOOLS 表），拆分徒增间接层 |
| db/legacy_migrations.py | 1082 | v1→v27 迁移注册表，append-only 设计，已收敛为唯一生产入口 |
| chaos/reliability_bench.py | 808 | 未进主链路的独立基准工具 |

## 拆分优先级（按 接缝清晰度 × 变更频率 × 测试覆盖 排序）

### P1 — web/routers/setup.py（1630 行，59 个平铺路由函数）
接缝最清晰的候选：按向导阶段切子路由（password / test-key / provider /
local-deploy / finish）。APIRouter include 模式零风险，契约测试已有
（test_setup_password_required 等）。**建议首个实操对象**。

### P2 — prompt_builder.py（1640 行，40 个顶层函数）
已有内部命名接缝：_build_stable_prompt / _build_dynamic_prompt /
_classify_scene / canary。按 Stable/Dynamic/Scene 三子模块拆，config 已用
PEP 562 懒转发（本日 `7834fafb`），无循环导入风险。

### P3 — memory/_retrieval_engine.py（2153 行，2 类 51 方法）★最大
按检索管线阶段切：query_transform → recall → fusion → rerank → post_filter。
风险最高（核心聊天路径），须在批次 A except 治理（见
broad_except_inventory）完成后再动——先让异常语义可见，再动结构。

### P4 — 双适配器（qq_bot_adapter 1819 / wechat_bot_adapter 1494）
共性逻辑下沉 channel_adapter_base 的收尾工作（原审计遗留）：session 缓存、
分段流式、假打字延时真正下沉后删旧实现。与 P3 同理依赖 except 批次 C。

### P5 — 其余按触碰随治理
vector_store(1677)/ws_hub(1228)/text_utils(1198) 等在功能迭代触碰时顺带
拆分（boy-scout），不单独开专项。

## 执行纪律（每个拆分会话必须遵守）

1. 拆前跑全集留基线；拆后全集 + 该文件专属测试必须全绿
2. 一次只拆一个文件，逐字节搬移优先于改写（本项目 Phase1-4 拆分的既有惯例）
3. 新模块 docstring 写明兼容契约 + 配套契约测试（参照 tests/test_config_*_module.py 模式）
4. re-export 保持旧 import 面不破（from config import X 式兼容）
