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

### P4 — 双适配器（qq_bot_adapter 2175 / wechat_bot_adapter 1591）
共性逻辑下沉 channel_adapter_base 的收尾工作（原审计遗留）：session 缓存、
分段流式、假打字延时真正下沉后删旧实现。与 P3 同理依赖 except 批次 C。

> **2026-08-26 qq_bot_adapter 拆分前评估**（本会话实测，供拆分会话直接引用）：
>
> - 结构画像：AIQQBot 类 62 方法 ≈1785 行，占全文件 82%；模块级仅剩
>   QQReplyBudget/C2C NamedTuple 等轻量定义 + `__main__` 直启块。
> - **最清晰的第二刀不是继续下沉基类，而是媒体发送族独立成模块**
>   （`qq_media_sender.py` 或包）：`_send_streaming_reply`(142)、
>   `_send_fallback_reply_with_sticker`(122)、`_send_streaming_reply_with_sticker`(108)、
>   `_convert_to_silk`(107)、`_upload_base64`(77)、`_send_audio`(68)、
>   `_gather_media_send_tasks`(56)、`_send_video`(55)、`_compress_image`(53)
>   ——九个方法 ≈690 行，内聚度高（全部围绕"把内容发出去"），对 self 的
>   依赖集中在少数几个连接状态属性，接缝比 c2c/group 事件处理链干净得多。
>   拆出后本体可回到 ~1480 行，一步脱离巨型区（<1500）。
> - 外部 import 面已核：7 个生产消费方只取 `AIQQBot/run_qq_bot/
>   send_proactive_message/QQPipelineRequest/QQAmbiguousDelivery/_build_user_input/
>   _parse_master_ids/MAX_REPLY_LEN/_next_msg_seq/_qq_reply_budget_var`，
>   均为类与模块级符号，re-export 一层即可不破。
> - 变更频率佐证优先级：7 月以来 85 个 commit 触碰此文件，是全仓最高热点；
>   每次迭代都在给棘轮基线上涨找理由，拆分收益随时间放大。
> - broad_except 现状：本文件仅 6 处 `except Exception`（channel_adapter_base
>   同为 6），全仓 1164 处压基线——P4 所依赖的"except 批次 C"对本文件而言
>   实际已不构成前置障碍，**可以不再等批次 C 直接排期**。

### P5 — 其余按触碰随治理
vector_store(1677)/ws_hub(1228)/text_utils(1198) 等在功能迭代触碰时顺带
拆分（boy-scout），不单独开专项。

## 执行纪律（每个拆分会话必须遵守）

1. 拆前跑全集留基线；拆后全集 + 该文件专属测试必须全绿
2. 一次只拆一个文件，逐字节搬移优先于改写（本项目 Phase1-4 拆分的既有惯例）
3. 新模块 docstring 写明兼容契约 + 配套契约测试（参照 tests/test_config_*_module.py 模式）
4. re-export 保持旧 import 面不破（from config import X 式兼容）
