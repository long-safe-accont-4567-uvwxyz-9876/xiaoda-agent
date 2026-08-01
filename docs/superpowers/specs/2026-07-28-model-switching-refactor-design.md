# 模型切换逻辑彻底重构设计

> 日期: 2026-07-28
> 状态: 已批准（用户口头批准核心约束）
> 范围: model_router.py / web/server.py / web/config_service.py / web/routers/models.py / config.py

## 一、问题根因

### 1.1 用户反馈
"我之前把 Web UI 里面所有的模型全部换成了 Agnes，重启后又恢复成最开始的默认 MiMo 模型。"

### 1.2 表象与真因
之前定位为 `config_service._save()` 反向同步覆盖用户配置。深入查证后发现这是**误导项**:

- `mark_startup_complete()` 在生产代码中**从未被调用**（只存在于 [tests/test_model_persistence_bugfix.py:423](../../../tests/test_model_persistence_bugfix.py)）
- 因此 `_save()` 的"从 ROUTE_TABLE 反向恢复 _data"逻辑从未生效，是一段死代码

**真正的根因是架构层面的设计缺陷**:

#### 缺陷 1: ROUTE_TABLE 是模块级全局可变状态，被 4 个文件直接修改
- `model_router.py:491-492` set_chat_model 直接改
- `web/server.py:204-205, 221-222` _restore_chat_model 直接改（含失败时硬编码 fallback 到 mimo）
- `web/server.py:158-160` _apply_route_overrides 直接改
- `web/routers/models.py:254-256` update_route 直接改

#### 缺陷 2: 4 套并行"模型配置真相源"，无清晰优先级
- `ROUTE_TABLE`（运行时内存）
- `webui_overrides.json` 的 `models.routes`（持久化）
- `webui_overrides.json` 的 `models.chat_model`（持久化的"上次选择"）
- `config.py` 的 `MODEL_NAME` / `DEFAULT_PROVIDER`（环境变量派生，模块级冻结）

任一处被降级链临时污染，下一次任意 `cfg.set()` 都可能把污染持久化。

#### 缺陷 3: 降级链直接读 ROUTE_TABLE 的 fallback 路由
`model_router.py:829` `_try_fallback_chain` 通过 `ROUTE_TABLE.get(fallback_type)` 查 fallback 配置，与全局状态耦合。

#### 缺陷 4: set_chat_model 不是原子操作
`model_router.py:490-575` 顺序：改 ROUTE_TABLE → 检查 provider 注册 → 同步其他 task → 持久化。第二步抛 `LLMError` 时，ROUTE_TABLE 已被改但持久化没发生，留下半成品状态。

#### 缺陷 5: 硬编码默认模型散落 6 处
- `config.py:289` `MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")`
- `config.py:400` `DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "mimo")`
- `config.py:415-419` `_PROVIDER_DEFAULT_MODELS` 中 mimo→"mimo-v2.5", agnes→"agnes-v1"（agnes-v1 是老模型，与持久化文件中的 agnes-2.0-flash 冲突）
- `config.py:431` `AGNES_TEXT_MODEL = os.getenv("AGNES_TEXT_MODEL", "agnes-2.0-flash")`
- `model_router.py:95-96` 重复定义 MIMO_MODEL/MIMO_PRO_MODEL
- `model_router.py:138-146` ROUTE_TABLE 7 个路由全部引用这些冻结变量

#### 缺陷 6: 持久化文件中死路由未清理
`webui_overrides.json:214-262` 还存着 `chat_mimo`、`chat_mini`、`chat_ultra`——ROUTE_TABLE 中已删除，但持久化文件未清理，WebUI 仍会显示。

## 二、用户核心约束（设计纲领）

1. **默认行为**: 用户在 Web UI 没选过模型时，默认用 MiMo（作为初始 provider），具体模型 ID 从 `provider_metadata.json` 读，不在代码里硬编码
2. **持久化锁死**: 用户一旦在 Web UI 改过模型选择，写入 `webui_overrides.json` 后，**任何路径都不得覆盖**——包括降级链、_save 反向同步、_restore_chat_model 失败 fallback。只能等用户下次主动改
3. **模型来源**: 所有默认模型 ID 来自 `provider_metadata.json` 的 `providers.{pid}.default_model` 字段（该文件已就绪），环境变量优先级最高
4. **不停下来**: 不走"等用户审核 spec"流程，落盘后直接转入实现

## 三、架构设计

### 3.1 单一真相源 + ROUTE_TABLE 降级为只读快照

```
┌─────────────────────────────────────────────────────────────┐
│  ConfigService (webui_overrides.json)                       │
│  ─────────────────────────────────────────                  │
│  models.routes.{task}    ← 用户改过的路由（持久化、真相源）  │
│  models.chat_model       ← 用户上次选择的 chat 模型          │
└─────────────────────────────────────────────────────────────┘
              │
              │ 启动时一次性同步（单向）
              ▼
┌─────────────────────────────────────────────────────────────┐
│  ModelRouteRegistry (新增，封装 ROUTE_TABLE)                │
│  ─────────────────────────────────────────                  │
│  _route_table: dict    ← 只读快照，启动后不再被外部修改     │
│  update_route(task, model, provider, ...)                   │
│    → 原子操作：验证 → 改内存 → 持久化到 ConfigService        │
│    → 失败时回滚内存                                         │
└─────────────────────────────────────────────────────────────┘
              ▲
              │ 启动时填充默认值（仅当用户未选择时）
              │
┌─────────────────────────────────────────────────────────────┐
│  provider_metadata.json (已存在)                            │
│  providers.{pid}.default_model  ← 默认模型 ID               │
└─────────────────────────────────────────────────────────────┘
```

### 3.2 数据流方向（不可逆）

```
provider_metadata.json ──(默认值)──┐
                                    ▼
webui_overrides.json ──(真相源)──→ ROUTE_TABLE (只读快照) ──→ LLM 调用
        ▲                              │
        │                              │ 降级链读取只读快照，
        │                              │ 构造本地 fallback_config
        │                              ▼
        │                         _try_fallback_chain
        │                         (本地变量，不污染全局)
        │
        └──(用户改模型时)── WebUI update_route → ConfigService.set()
                                          → Registry.update_route()
                                          → 内存 + 持久化原子完成
```

**关键不变量**: ROUTE_TABLE 启动后只读。任何运行时修改必须走 `Registry.update_route()`，该方法原子地完成"改内存 + 持久化"，失败回滚。

### 3.3 默认模型解析优先级

启动时，对每个 task 路由：

```
1. 如果 webui_overrides.json 的 models.routes.{task} 存在且有效
   → 用持久化值（用户改过的，锁死）
2. 否则如果 task == "chat" 且 models.chat_model 存在
   → 用 chat_model 的 provider/model（向后兼容）
3. 否则
   → 从 provider_metadata.json 读 DEFAULT_PROVIDER 的 default_model
   → DEFAULT_PROVIDER 默认 "mimo"（来自环境变量或 fallback）
```

### 3.4 降级链重构

`_try_fallback_chain` 不再读 `ROUTE_TABLE.get(fallback_type)`，改为：

```python
def _build_fallback_config(self, original_task: str, original_provider: str) -> tuple[str, dict] | None:
    """从 FALLBACK_ROUTE 表 + 当前路由快照构造本地 fallback config。
    
    返回的 dict 是本地变量，修改它不会影响 ROUTE_TABLE。
    """
    fallback_type = FALLBACK_ROUTE.get(original_task)
    while fallback_type:
        # 从只读快照深拷贝，避免污染
        snapshot = self._registry.snapshot_task(fallback_type)
        if snapshot and self._is_client_configured(snapshot.get("client", "")):
            return fallback_type, copy.deepcopy(snapshot)
        fallback_type = FALLBACK_ROUTE.get(fallback_type)
    # 跨 provider 兜底
    cross = _CROSS_PROVIDER_MAP.get(original_provider)
    if cross and self._is_client_configured(cross[0]):
        return f"cross_{original_provider}", {
            "model": cross[1], "client": cross[0],
            "max_tokens": 2000, "thinking": {"type": "disabled"},
        }
    return None
```

## 四、实施步骤

### Step 1: 新增 ModelRouteRegistry
- 在 `model_router.py` 内新增 `ModelRouteRegistry` 类
- 封装 `_route_table` 字典，提供 `get_task(task)` / `snapshot_task(task)` / `update_route(...)` / `all_tasks()` 方法
- `update_route` 原子操作：先验证 provider 已注册 → 深拷贝构造新 entry → 写入内存 → 持久化到 ConfigService → 失败时回滚
- `get_task` 返回深拷贝，防止调用方通过引用污染

### Step 2: 删除 config.py 中的硬编码默认模型
- 删除 `MIMO_MODEL`、`_PROVIDER_DEFAULT_MODELS`、`AGNES_TEXT_MODEL` 的硬编码默认值
- 保留 `DEFAULT_PROVIDER = os.getenv("DEFAULT_PROVIDER", "mimo")` 作为 provider 选择默认值（这是合理的初始 provider，不是模型 ID 硬编码）
- `MODEL_NAME` / `PRO_MODEL_NAME` / `FLASH_MODEL_NAME` 改为运行时从 `provider_metadata.json` 解析的函数 `get_default_model_for_provider(pid)`
- 向后兼容：保留模块级变量名以减少改动面，但值从函数派生

### Step 3: 删除 model_router.py 顶部重复的硬编码
- 删除 `MIMO_MODEL`、`MIMO_PRO_MODEL` 的重复定义（L95-96）
- 改为从 `provider_metadata.json` 读
- ROUTE_TABLE 初始定义改为引用 `get_default_model_for_provider(DEFAULT_PROVIDER)` 等

### Step 4: 删除 _save() 反向同步逻辑
- `config_service.py:312-367` 整段删除（已经是死代码）
- 删除 `mark_startup_complete` 方法和 `_startup_complete` 字段
- 删除测试中对 `mark_startup_complete` 的调用

### Step 5: 重构 set_chat_model
- 改为通过 `Registry.update_route()` 实现原子操作
- 删除直接的 `ROUTE_TABLE["chat"]["model"] = ...` 赋值
- 同步更新 chat_pro / chat_flash 等也走 `update_route`
- 失败时回滚内存到旧值

### Step 6: 重构 _restore_chat_model
- 失败时不再硬编码 fallback 到 mimo
- 改为：日志告警 + 保留 ConfigService 持久化的用户选择不变 + 内存中回退到 `provider_metadata.json` 的 default_model（仅内存，不持久化）
- 这样下次启动重新尝试恢复用户选择

### Step 7: 重构 _try_fallback_chain
- 不再读 `ROUTE_TABLE.get(fallback_type)`，改用 `_build_fallback_config`
- fallback_config 是本地深拷贝，修改不影响全局

### Step 8: 启动时清理死路由
- `_apply_route_overrides` 中，遍历持久化的 `models.routes`，删除 ROUTE_TABLE 中已不存在的 task
- 日志记录清理的死路由

### Step 9: 重构 web/routers/models.py
- `update_route` API 改为调用 `Registry.update_route()`，不再直接改 ROUTE_TABLE
- `list_routes` 仍读 ROUTE_TABLE（只读快照，安全）

## 五、测试策略

### 5.1 回归测试（TDD）

新增 `tests/test_model_switching_refactor.py`，覆盖：

1. **核心场景: 用户改过模型后不被覆盖**
   - 模拟用户 set_chat_model("agnes", "agnes-2.0-flash")
   - 触发降级链（mock LLM 失败）
   - 重启模拟（重新加载 ConfigService）
   - 断言: ROUTE_TABLE["chat"] 仍然是 agnes/agnes-2.0-flash

2. **默认值场景: 用户未选择时用 mimo**
   - 清空 webui_overrides.json 的 models 字段
   - 启动 ConfigService + Registry
   - 断言: ROUTE_TABLE["chat"] 是 mimo/mimo-v2.5（来自 provider_metadata.json）

3. **原子性场景: set_chat_model 失败时回滚**
   - mock provider 未注册，set_chat_model 抛 LLMError
   - 断言: ROUTE_TABLE 保持旧值不变

4. **降级链不污染场景**
   - 触发降级链，fallback 到 chat_agnes
   - 断言: 降级后 ROUTE_TABLE["chat"] 未变（降级是本地变量）

5. **死路由清理场景**
   - 持久化文件中放入 chat_mimo/chat_mini/chat_ultra
   - 启动后断言: 持久化文件中这些 task 已被删除

6. **持久化锁死场景**
   - 用户改 agnes → 持久化
   - 模拟 _restore_chat_model 失败（agnes provider 未注册）
   - 断言: ConfigService 持久化值仍是 agnes（未被 fallback 覆盖）
   - 断言: 内存中临时回退到 mimo，但下次启动重新尝试 agnes

### 5.2 现有测试修复

- `test_model_persistence_bugfix.py`: 删除 `mark_startup_complete` 调用，调整为新的 Registry API
- `test_fallback_optimization.py`: 调整为新的降级链接口
- `test_agnes_max_tokens_and_sticky_fallback.py`: 调整为新的 Registry API

## 六、验收标准

1. ✅ 用户在 Web UI 切换 Agnes 后，重启服务仍为 Agnes
2. ✅ 触发降级链后，ROUTE_TABLE 全局状态不变
3. ✅ set_chat_model 失败时回滚，不留半成品状态
4. ✅ 持久化文件中无死路由（chat_mimo/chat_mini/chat_ultra）
5. ✅ 代码中无硬编码模型 ID（grep "mimo-v2.5" 只在 provider_metadata.json 和测试中出现）
6. ✅ _restore_chat_model 失败时不覆盖持久化配置
7. ✅ 所有现有测试通过（调整后的）

## 七、风险与回滚

### 风险
- ROUTE_TABLE 改为只读快照后，可能有遗漏的写入点未发现
- ModelRouteRegistry 是新类，可能引入新 bug

### 回滚策略
- 保留 git 分支 `model-switching-refactor`
- 每步独立提交，可逐步回滚
- 实施完成后跑全量回归测试，发现关键失败立即回滚

## 八、不做什么（YAGNI）

- ❌ 不重写整个 ModelRouter 类（只改路由表管理部分）
- ❌ 不删除 transports 抽象（与本次重构无关）
- ❌ 不重构凭证池（独立模块，本次不动）
- ❌ 不改前端代码（后端 API 兼容）
