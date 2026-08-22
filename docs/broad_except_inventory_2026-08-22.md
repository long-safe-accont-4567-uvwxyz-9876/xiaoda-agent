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

## 分批治理策略（后续专项会话按此推进）

原则：**一次一批、批内全测、只窄不删**——把 `except Exception` 收窄到该调用点
实际可能抛出的类型集合，而不是删除容错。

- **批次 A（web/routers/*，约 130 处）**：请求路径，静默吞掉会把 bug 变成
  200+空响应。逐点判断：参数类→ValueError/KeyError/TypeError；IO 类→OSError/
  sqlite3.Error；外部服务→httpx.HTTPError/TimeoutException。API 层应让意外
  异常冒泡到统一 exception handler 返回 500 而非假成功。
- **批次 B（memory/*，约 130 处）**：检索/编码链路，多为"降级继续"语义——保留
  宽捕获但补结构化日志字段（stage/reason），并区分 LocalModelUnavailableError
  与意外异常。
- **批次 C（core/bootstrap + adapters，约 80 处）**：启动容错语义明确
  （单步失败不阻断核心聊天），收窄为 (OSError, RuntimeError, ValueError,
  json.JSONDecodeError) 组合即可覆盖 95% 实际场景。
- **批次 D（长尾 205 文件）**：随触碰随治理（boy-scout），不单独开批。

## 已完成的定点清除

- config.py 四 getter 静默失效 → warn-once（`fb1a6bcd`）
- rate_limit 持久化 except 收口 sqlite3.Error（`423fa7f3`）
- 纯 pass 吞没复核归零（本日复扫）
