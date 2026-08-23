# 宽口径 except 存量清单与棘轮防线（2026-08-22）

> 技术债四大专项 #2 配套文档。存量：**1136 处** `except Exception`（非测试源码，
> 225 个文件）；裸 `except:` 语法 0 处；`except Exception: pass` 纯吞没仅 2 处
> 且均为合理形态（窄类型捕获 / asyncio.CancelledError 惯例）。

## 防线机制（已生效）

- `scripts/check_broad_except.sh`：当前计数 vs 基线（`scripts/broad_except_baseline.txt`=1136），超基线即失败
- 已接入 pre-push 钩子（毫秒级，先于测试执行）
- **规则**：增量必须收窄异常类型；确属必要宽捕获时同步上调基线并在提交说明给理由；基线只许随净修复下调

## 文件分布 Top 20

| 文件 | 处数 | | 文件 | 处数 |
|---|---|---|---|---|
| memory/vector_store.py | 50 | | memory/npu_embed.py | 13 |
| web/routers/setup.py | 34 | | memory/knowledge_graph.py | 13 |
| core/bootstrap.py | 32 | | db/db_memory_entity.py | 13 |
| memory/_retrieval_engine.py | 30 | | agent_core/core.py | 13 |
| agent_core/sub_agent_manager.py | 25 | | web/routers/health.py | 12 |
| web/routers/insight.py | 24 | | prompt_builder.py | 12 |
| wechat_bot_adapter.py | 22 | | agent_core/mixins/main_path.py | 12 |
| memory/_memory_encoder.py | 22 | | market/installer.py | 11 |
| web/routers/agents.py | 16 | | （其余 205 文件） | ≤10 |
| web/routers/wechat.py / emotion/nudge_engine.py / agent_context.py | 15×3 | | | |

形态分布：~294 处伴随 `logger.debug`（原审计口径的"静默降级"主力）；
其余为 warn/error 日志或带回退值。

## 分批治理策略（v2，2026-08-22 晚按实测判读修订）

**打法修正**：切片 1（health/insight 全量判读）发现 routers 的宽捕获绝大
多数是"降级继续 + exception 级全栈日志"的正当形态——机械收窄会把单个
传感器/子系统故障放大成 500，反而劣化。真实债务经形态扫描量化为
**约 36 处假成功/debug 静默点**：

```
jspace.py 7 | agents.py 6 | model_discovery.py 6 | chat.py 3 |
setup.py 2 | 其余各 1（mail_manage/market/mcp/models/retrieval/system/
tools/wechat/workflows）
```

治理三分法：
1. **写操作假成功**（如 jspace config_set 返回 updated:[] 成功信封）→
   可预期失败转 HTTPException(500) 带原因；意外异常冒泡标准 500
2. **读路径 debug 级静默降级** → 保留降级语义，debug→warning 可见化
3. **双段捕获参考惯用法**（health.py：预期窄类型 warning + 意外桶
   exception 全栈）→ 保留不动，作为批次 B/C 的范本

- ~~切片 1：health.py（2 处真收窄+1 日志名）+ jspace.py（9 处可见化+
  假成功写修复）~~ ✅ `c0919853`
- ~~切片 2：model_discovery(6) + agents(6) + chat(3)~~ ✅ 逐点判读后
  **15 处全部正当**（SWR 后台降级 exception 级可见 / 登录页必须容错 /
  audit+广播 best-effort / CRUD 正确转 4xx）——启发式高估，routers 批次 A
  实际收尾。剩余宽捕获即参考惯用法本身，随批次 B/C 范本统一。
- 批次 B/C 策略不变：memory/bootstrap/adapters 按降级语义补结构化字段

## 已完成的定点清除

- config.py 四 getter 静默失效 → warn-once（`fb1a6bcd`）
- rate_limit 持久化 except 收口 sqlite3.Error（`423fa7f3`）
- 纯 pass 吞没复核归零（本日复扫）
