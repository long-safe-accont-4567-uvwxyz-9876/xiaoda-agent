"""巨型文件止血守卫（ratchet）：热点文件只许拆小，不许继续堆肥。

背景（docs/tech_debt_audit_2026-08-25.md §二 / giant_files_split_plan_2026-08-22）：
qq_bot_adapter 在 P4 基类沉淀（eba85d91）后不降反涨 354 行——说明"结构性拆分"
挡不住日常迭代往单文件本体堆逻辑。本守卫把四个最高风险热点的行数钉在
当前实测基线上：任何使其变长的改动必须在同一提交里给出拆分或同步下调基线。

规则：
1. 文件行数 ≤ BASELINE 记录值。缩小是好事，允许（基线随后续净修复下调）；
2. 超限失败信息必须指向"拆分或下调基线"，防止机械 +1 绕过；
3. 基线只许随真实拆分/删码下调，与 broad_except 棘轮同纪律。

已知边界：本守卫只看总行数这一个代理指标，不判断代码好坏——它的目标不是
"让数字变小"，而是在有人往 2000+ 行的适配器本体再塞一个方法时，
强制其停下来想一想该进基类、子模块还是新文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 基线 = 各文件止血液轮落地时实测值（wc -l）。
# 下调流程：拆分/删码合入后，把这里改成新的实测值并在提交说明注明。
# 2026-08-25 P2 拆分①：prompt_builder.py(1669) → 包(门面148/common50/
#   scene781/assembly661/workspace215)，原条目替换为包内两大子模块。
# 2026-08-25 P2 拆分②：web/ws_hub.py(1591) → ws_hub(1118)+ws_terminal(518)，
#   原条目替换为拆分后两文件实测值。
# 2026-08-26 上调记录（协议 b：必要增长，理由供提交说明引用）：
#   qq_bot_adapter.py 2174→2175 —— __main__ 直启块 18 处 print 换 loguru
#     （可观测性清偿，多行消息合并致净 +1；见 fceb388d）；
#   web/ws_hub.py 1118→1160 —— 每连接 chat 并发硬顶(MAX_CHAT_TASKS_PER_CONN=3,
#     防换 msg_id 无限拉起 LLM 任务)+ ConnectionManager 封装方法(get/set_session、
#     get/set_agent、notify_pong、inflight_chat_count)+媒体清理 IO 下放线程池；
# 2026-08-27 上调记录（协议 b：必要增长）：
#   qq_bot_adapter.py 2175→2176 —— ffmpeg/silk 转码与媒体上传文件读迁移到
#     heavy-io 线程池（utils/thread_pools.py 双池隔离，解消息检索排队问题），
#     import 行 +1；
BASELINES: dict[str, int] = {
    "qq_bot_adapter.py": 2176,
    "wechat_bot_adapter.py": 1591,
    "web/ws_hub.py": 1160,
    "web/ws_terminal.py": 556,
    "prompt_builder/_prompt_scene.py": 783,
    "prompt_builder/_prompt_assembly.py": 661,
}

WEB_TEST_BASELINES: dict[str, int] = {
    "web/frontend/src/i18n/zh.ts": 1562,
    "web/frontend/src/i18n/en.ts": 1561,
    "web/frontend/src/views/ChatView.vue": 962,
    "web/frontend/src/views/RetrievalView.vue": 915,
    "tests/test_local_ai_device_registry.py": 2260,
    "tests/test_provider_onboarding.py": 1715,
}


def _line_count(rel: str) -> int:
    # 与 wc -l 同口径(数换行符):避免无尾换行文件在两套口径间差 1 行
    return (ROOT / rel).read_text(encoding="utf-8").count("\n")



# 赦免清单其余成员的基线(2026-08-25 一致性收口:进清单必被看守,
# 由 debt_audit.sh 的"赦免清单一致性"检查强制)
# 2026-08-26 上调记录（协议 b：必要增长）：
#   web/ws_terminal.py 534→553 —— 终端会话同款封装方法接线与 IO 下放；
#   prompt_builder/_prompt_scene.py 781→783 —— 桶排序差集 bug 修复
#     (对照 filtered_ordering 而非 module_set,原差集恒为空)；
#   web/server.py 1397→1404 —— .env→凭证存储启动回写(修 siliconflow 陈旧
#     占位符致探针 401)+ 全局 body 上限中间件(80MB)接线。
#   web/routers/setup.py 1322→1333 —— test-key 速率限制按 IP 分桶(防多用户
#     挤占配额、防伪造 IP 撑爆内存)+ first_run 探测 IO 下放线程池；
#   memory/retrieval/pipeline.py 1069→1075 —— KG 实体抽取预热与七路召回并行
#     (免费模型抽取串行曾吃掉全局窗口致 kg 通道 34% 失败率)；
#   agent_core/sub_agent.py 1027→1036 —— refresh_router():路由热更新后
#     子代理重抓 router 并刷新降级态；
#   web/routers/insight.py 923→926 —— 向量检索分页语义对齐 DB 分页 + 空实体
#     全图模式自动选根(原固定最近 80 条忽略 depth)；
#   llm_gateway/router_execution.py 1097→1099 —— 凭证池错误归因精确化:
#     report_error/report_success 传实际出错客户端 api_key(多凭证并发在途
#     时旧启发式误伤健康凭证), 两处调用点各拆两行；
# 2026-08-27 上调记录（协议 b：必要增长）：
#   memory/vector_store.py 1682→1686 —— 全部 sqlite/embed 同步操作迁移到
#     hot-io 延迟敏感线程池(utils/thread_pools.py)，import 行与注释；审查
#     后续修：_auto_rebuild 分钟级重建子进程从 hot 池改 heavy 池(挤压检索)；
#   memory/retrieval/pipeline.py 1075→1084 —— 小妲 P1-4 贯穿：简单路径预计算
#     查询向量传入混合检索(vec 主通道与 child 子通道批内复用, embed 减半)；
#   core/background_tasks.py 950→956 —— _spawn finally 的 ContextVar reset
#     增加 ValueError 防护(loop 关停 GC finalizer 路径 reset 必然失败抛错)；
#   core/background_tasks.py 1006→1018 —— 事件循环亚秒级漂移采样接线
#     (Windows"卡"定位)：watchdog 启/停挂起/取消 utils/loop_lag_monitor
#     采样任务，与 10s 栈取证互补；web/server.py 为止血对象不接线；
#   以上四项 2026-08-27 二次上调——归属小妲已入库批次(非本会话改动):
#     vector_store 1686→1732 —— embed 批量路径 EmbedCache 接入(95302d29,
#       仅 miss 子集送推理)+ provider 耗时直方图(a8e4f85d)；
#     insight 926→937 / background_tasks 956→1006 —— 记忆树原地编辑与
#       WAL 守护任务接入(a833ca4d/457dd118)；zh/en i18n 1536→1562/1535→1561
#       —— GSAP 编排批次文案扩充(b1e5db3b 等)；
ALLOWLIST_BASELINES: dict[str, int] = {
    "memory/vector_store.py": 1732,
    "web/server.py": 1404,
    "agent_context.py": 1345,
    "web/routers/setup.py": 1333,
    "db/legacy_migrations.py": 1289,
    "core/bootstrap.py": 1336,
    "utils/text_utils.py": 1151,
    "memory/_memory_encoder.py": 1134,
    "db/db_memory_reconciliation.py": 1102,
    "llm_gateway/router_execution.py": 1099,
    "tool_engine/mcp_client.py": 1090,
    "memory/retrieval/pipeline.py": 1084,
    "ilink_client.py": 1039,
    "agent_core/sub_agent_manager.py": 1131,
    "agent_core/sub_agent.py": 1036,
    "tools/_builtin_manifest.py": 1001,
    "web/routers/insight.py": 937,
    "core/background_tasks.py": 1018,
    "web/agent_registry.py": 912,
    "web/routers/local_deploy.py": 919,
    "cli.py": 908,
}

def test_hotspot_files_do_not_grow():
    all_baselines = {**BASELINES, **WEB_TEST_BASELINES, **ALLOWLIST_BASELINES}
    overgrown = [
        f"{rel}: {_line_count(rel)} > baseline {limit}"
        for rel, limit in all_baselines.items()
        if _line_count(rel) > limit
    ]
    assert not overgrown, (
        "巨型文件止血液轮被突破——这些文件比基线更大：\n  "
        + "\n  ".join(overgrown)
        + "\n处理方式（二选一）：\n"
        "  a) 把新增逻辑放进 channel_adapter_base / 子模块 / 新文件，而不是本体外加；\n"
        "  b) 确属必要的本体增长（如临时修复），同步下调 BASELINES 前先开拆分 issue。"
    )


def test_baseline_matches_reality_when_smaller():
    """基线若大于现状（文件已被拆小），提示下调基线但不失败——保持棘轮单调向下。"""
    stale = [
        f"{rel}: baseline {limit} > actual {_line_count(rel)}"
        for rel, limit in {**BASELINES, **WEB_TEST_BASELINES}.items()
        if _line_count(rel) < limit - 50  # 容忍 ±50 行内的自然波动噪音
    ]
    # 不 fail：仅当明显缩小时打印提醒。真正的棘轮下调由人工在拆分提交里完成。
    if stale:
        print("建议下调 BASELINES（文件已显著变小）:\n  " + "\n  ".join(stale))


if __name__ == "__main__":
    sys.exit(0 if not any(_line_count(rel) > limit for rel, limit in BASELINES.items()) else 1)
