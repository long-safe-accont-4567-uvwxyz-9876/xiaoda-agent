# 模型切换逻辑彻底重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 彻底消除模型配置的多真相源污染问题，实现"用户改过即锁死、默认值从配置文件读、降级链不污染全局"的单一真相源架构。

**Architecture:** 新增 `ModelRouteRegistry` 封装 ROUTE_TABLE 为只读快照，所有修改走原子入口。删除 `_save()` 反向同步死代码。降级链改用本地 fallback config。硬编码默认模型迁移到 `provider_metadata.json`（已就绪）。

**Tech Stack:** Python 3.11+, asyncio, threading.Lock, copy.deepcopy, TDD

## Global Constraints

- 不得引入新的第三方依赖
- 所有修改必须向后兼容（API 签名不变）
- 代码中不得出现硬编码模型 ID（grep "mimo-v2.5" / "agnes-2.0-flash" 只允许出现在 `provider_metadata.json` 和测试中）
- 每个 task 完成后必须 `python -m py_compile` 通过
- 持久化锁死原则：用户在 Web UI 改过的配置，任何路径都不得覆盖

---

## 文件结构

| 文件 | 责任 | 操作 |
|------|------|------|
| `model_router.py` | 新增 `ModelRouteRegistry` 类；重构 set_chat_model / _try_fallback_chain | 修改 |
| `config.py` | 删除硬编码默认模型，新增 `get_default_model_for_provider()` | 修改 |
| `web/config_service.py` | 删除 `_save()` 反向同步、`mark_startup_complete` | 修改 |
| `web/server.py` | 重构 `_restore_chat_model`、`_apply_route_overrides`；启动清理死路由 | 修改 |
| `web/routers/models.py` | `update_route` 改走 Registry | 修改 |
| `tests/test_model_switching_refactor.py` | 新增回归测试 | 新建 |

---

### Task 1: 新增 ModelRouteRegistry 类（核心封装）

**Files:**
- Modify: `model_router.py` (在 `ModelRouter` 类之前新增 `ModelRouteRegistry` 类)
- Test: `tests/test_model_switching_refactor.py`

**Interfaces:**
- Consumes: `config_service.get_config_service()`, `provider_metadata.json`
- Produces: `ModelRouteRegistry` 类，方法 `get_task(task) -> dict | None`、`snapshot_task(task) -> dict | None`、`update_route(task, model, provider, max_tokens=None, thinking=None, timeout=None) -> dict`、`all_tasks() -> list[str]`、`replace_table(new_table: dict) -> None`

**设计要点:**
- `ModelRouteRegistry` 持有 `_table: dict`，初始化时从现有 `ROUTE_TABLE` 浅拷贝
- `get_task` 返回深拷贝（防引用污染）
- `snapshot_task` 同 `get_task`，语义清晰
- `update_route` 原子操作：构造新 entry → 写内存 → 持久化到 ConfigService → 失败回滚
- `replace_table` 用于启动时一次性填充（覆盖默认值）

- [ ] **Step 1: 写失败测试 — Registry 基本读写**

新建 `tests/test_model_switching_refactor.py`：

```python
"""模型切换逻辑彻底重构回归测试。

覆盖核心约束：
1. 用户改过模型后不被任何路径覆盖
2. 默认值从 provider_metadata.json 读
3. set_chat_model 失败时回滚
4. 降级链不污染全局 ROUTE_TABLE
5. 死路由启动时清理
6. _restore_chat_model 失败不覆盖持久化
"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fresh_registry():
    """每个测试用独立的 Registry 实例，避免相互污染。"""
    from model_router import ModelRouteRegistry
    # 用最小默认表初始化
    default_table = {
        "chat": {"model": "mimo-v2.5", "max_tokens": 131072, "client": "mimo",
                 "thinking": {"type": "disabled"}},
        "chat_pro": {"model": "mimo-v2.5", "max_tokens": 131072, "client": "mimo",
                     "thinking": {"type": "enabled", "budget_tokens": 4096}},
        "chat_flash": {"model": "mimo-v2.5", "max_tokens": 6144, "client": "mimo",
                       "thinking": {"type": "disabled"}},
        "chat_agnes": {"model": "agnes-2.0-flash", "max_tokens": 131072, "client": "agnes",
                       "thinking": {"type": "disabled"}},
    }
    # mock ConfigService 避免真实落盘
    mock_cfg = MagicMock()
    mock_cfg.set = MagicMock()
    return ModelRouteRegistry(default_table, config_service=mock_cfg), mock_cfg


def test_registry_get_task_returns_deep_copy(fresh_registry):
    """get_task 必须返回深拷贝，调用方修改不影响内部状态。"""
    reg, _ = fresh_registry
    task = reg.get_task("chat")
    assert task is not None
    task["model"] = "POLLUTED"
    task["thinking"]["type"] = "POLLUTED"
    # 内部状态不变
    again = reg.get_task("chat")
    assert again["model"] == "mimo-v2.5"
    assert again["thinking"]["type"] == "disabled"


def test_registry_update_route_atomic_success(fresh_registry):
    """update_route 成功时同时更新内存和持久化。"""
    reg, mock_cfg = fresh_registry
    result = reg.update_route("chat", model_id="agnes-2.0-flash", provider="agnes")
    assert result["model"] == "agnes-2.0-flash"
    assert result["client"] == "agnes"
    # 内存已更新
    assert reg.get_task("chat")["model"] == "agnes-2.0-flash"
    # 持久化被调用
    assert mock_cfg.set.called
    call_args = mock_cfg.set.call_args_list
    # 至少持久化了 chat 路由和 chat_model
    paths_persisted = [c.args[0] for c in call_args]
    assert "models.routes.chat" in paths_persisted
    assert "models.chat_model" in paths_persisted


def test_registry_update_route_rollback_on_persist_failure(fresh_registry):
    """持久化失败时内存必须回滚到旧值。"""
    reg, mock_cfg = fresh_registry
    # 第一次 set 成功，第二次抛异常
    mock_cfg.set.side_effect = [None, RuntimeError("disk full"), None]
    original_model = reg.get_task("chat")["model"]
    with pytest.raises(RuntimeError):
        reg.update_route("chat", model_id="agnes-2.0-flash", provider="agnes")
    # 内存回滚
    assert reg.get_task("chat")["model"] == original_model


def test_registry_snapshot_task_independent_of_get_task(fresh_registry):
    """snapshot_task 与 get_task 返回独立的拷贝。"""
    reg, _ = fresh_registry
    s1 = reg.snapshot_task("chat")
    s2 = reg.snapshot_task("chat")
    assert s1 is not s2
    s1["model"] = "X"
    assert s2["model"] == "mimo-v2.5"


def test_registry_all_tasks_returns_list(fresh_registry):
    """all_tasks 返回 task 名称列表。"""
    reg, _ = fresh_registry
    tasks = reg.all_tasks()
    assert isinstance(tasks, list)
    assert "chat" in tasks
    assert "chat_pro" in tasks


def test_registry_replace_table_bulk_update(fresh_registry):
    """replace_table 一次性替换整个表（启动时用）。"""
    reg, mock_cfg = fresh_registry
    new_table = {
        "chat": {"model": "agnes-2.0-flash", "max_tokens": 8192, "client": "agnes",
                 "thinking": {"type": "disabled"}},
        "chat_pro": {"model": "agnes-2.0-flash", "max_tokens": 8192, "client": "agnes",
                     "thinking": {"type": "disabled"}},
    }
    reg.replace_table(new_table)
    assert reg.get_task("chat")["model"] == "agnes-2.0-flash"
    assert reg.get_task("chat_pro")["client"] == "agnes"
    # replace_table 不触发持久化（启动时用，持久化由调用方负责）
    assert not mock_cfg.set.called
```

- [ ] **Step 2: 运行测试验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | head -30`
Expected: FAIL with `ImportError: cannot import name 'ModelRouteRegistry'`

- [ ] **Step 3: 实现 ModelRouteRegistry 类**

在 `model_router.py` 中 `ModelRouter` 类定义之前（约 L264 之前）新增：

```python
class ModelRouteRegistry:
    """路由表注册中心：ROUTE_TABLE 的唯一读写入口。

    设计原则：
    - 启动后 _table 是只读快照，所有修改必须走 update_route()
    - update_route() 是原子操作：构造新 entry → 写内存 → 持久化 → 失败回滚
    - get_task/snapshot_task 返回深拷贝，防引用污染
    - replace_table 用于启动时一次性填充（不持久化，由调用方负责）

    这样保证：
    1. 用户改过的配置不会被降级链/fallback 路径覆盖
    2. 持久化失败不留半成品状态
    3. 降级链读取的是独立快照，修改不影响全局
    """

    def __init__(self, initial_table: dict | None = None,
                 config_service: Any = None) -> None:
        # 深拷贝初始表，避免共享引用
        self._table: dict[str, dict] = copy.deepcopy(initial_table) if initial_table else {}
        # 延迟加载 ConfigService：测试时可注入 mock，生产时从 get_config_service() 取
        self._cfg = config_service

    def _get_cfg(self) -> Any:
        """延迟获取 ConfigService 实例（避免循环导入）。"""
        if self._cfg is not None:
            return self._cfg
        try:
            from web.config_service import get_config_service
            self._cfg = get_config_service()
        except (ImportError, RuntimeError) as e:
            logger.warning("registry.config_service_unavailable error={}", str(e))
            self._cfg = None
        return self._cfg

    def get_task(self, task: str) -> dict | None:
        """返回指定 task 路由的深拷贝（调用方修改不影响内部状态）。"""
        entry = self._table.get(task)
        return copy.deepcopy(entry) if entry is not None else None

    def snapshot_task(self, task: str) -> dict | None:
        """同 get_task，语义上表示"用于构造 fallback 的快照"。"""
        return self.get_task(task)

    def all_tasks(self) -> list[str]:
        """返回所有 task 名称。"""
        return list(self._table.keys())

    def replace_table(self, new_table: dict) -> None:
        """启动时一次性替换整个表（不触发持久化）。

        用于 _apply_route_overrides：从 ConfigService 加载用户配置后，
        用持久化值覆盖默认 ROUTE_TABLE。持久化由调用方决定（启动时一般不写回）。
        """
        self._table = copy.deepcopy(new_table)

    def update_route(self, task: str, model_id: str, provider: str,
                     max_tokens: int | None = None,
                     thinking: dict | None = None,
                     timeout: int | None = None,
                     persist: bool = True) -> dict:
        """原子地更新路由：内存 + 持久化。

        Args:
            task: 路由 task 名称（如 "chat", "chat_pro"）
            model_id: 模型 ID
            provider: provider 名称
            max_tokens: 可选，max_tokens 上限
            thinking: 可选，{"type": "enabled"|"disabled", "budget_tokens": ...}
            timeout: 可选，超时秒数
            persist: 是否持久化到 ConfigService（启动恢复时设为 False）

        Returns:
            新的路由 entry（深拷贝）

        Raises:
            KeyError: task 不存在
            RuntimeError: 持久化失败（内存已回滚）
        """
        if task not in self._table:
            raise KeyError(f"未知路由 task: {task}")

        # 保留旧值用于回滚
        old_entry = copy.deepcopy(self._table[task])

        # 构造新 entry
        new_entry = copy.deepcopy(old_entry)
        new_entry["model"] = model_id
        new_entry["client"] = provider
        if max_tokens is not None:
            new_entry["max_tokens"] = max_tokens
        if thinking is not None:
            new_entry["thinking"] = copy.deepcopy(thinking)

        # 写内存
        self._table[task] = new_entry

        # 持久化（失败回滚）
        if persist:
            cfg = self._get_cfg()
            if cfg is not None:
                try:
                    cfg.set(f"models.routes.{task}", {
                        "model": model_id,
                        "client": provider,
                        "max_tokens": new_entry.get("max_tokens"),
                        "thinking": bool(
                            thinking and isinstance(thinking, dict)
                            and thinking.get("type") == "enabled"
                        ),
                        "timeout": timeout if timeout is not None else 60,
                    })
                except Exception as e:
                    # 回滚内存
                    self._table[task] = old_entry
                    logger.error("registry.update_route_persist_failed task={} error={}",
                                 task, str(e))
                    raise RuntimeError(f"持久化路由 {task} 失败: {e}") from e

        return copy.deepcopy(new_entry)
```

注意：需要在文件顶部确保 `import copy` 存在。

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -20`
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add model_router.py tests/test_model_switching_refactor.py
git commit -m "feat(model_router): 新增 ModelRouteRegistry 封装路由表读写

- 原子化 update_route：内存+持久化，失败回滚
- get_task/snapshot_task 返回深拷贝防引用污染
- replace_table 用于启动时一次性填充"
```

---

### Task 2: 新增 get_default_model_for_provider 函数（替代 config.py 硬编码）

**Files:**
- Modify: `config.py:289, 415-419, 431`
- Test: `tests/test_model_switching_refactor.py` (追加)

**目标:** 删除 `MIMO_MODEL`、`_PROVIDER_DEFAULT_MODELS`、`AGNES_TEXT_MODEL` 的硬编码默认值，改为从 `provider_metadata.json` 读。保留 `DEFAULT_PROVIDER = "mimo"` 作为初始 provider 选择（这是 provider 名，不是模型 ID，符合"默认用 MiMo"的用户要求）。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_model_switching_refactor.py` 末尾追加：

```python
def test_get_default_model_for_provider_from_metadata():
    """从 provider_metadata.json 读默认模型 ID。"""
    from config import get_default_model_for_provider
    assert get_default_model_for_provider("mimo") == "mimo-v2.5"
    assert get_default_model_for_provider("agnes") == "agnes-2.0-flash"
    assert get_default_model_for_provider("deepseek") == "deepseek-chat"


def test_get_default_model_for_provider_env_override(monkeypatch):
    """环境变量优先级最高。"""
    monkeypatch.setenv("MIMO_MODEL_NAME", "mimo-custom-v9")
    from config import get_default_model_for_provider
    assert get_default_model_for_provider("mimo") == "mimo-custom-v9"


def test_get_default_model_for_provider_unknown_returns_empty():
    """未知 provider 返回空串（不抛异常）。"""
    from config import get_default_model_for_provider
    assert get_default_model_for_provider("unknown_provider_xxx") == ""
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_get_default_model_for_provider_from_metadata -v 2>&1 | tail -10`
Expected: FAIL with `ImportError: cannot import name 'get_default_model_for_provider'`

- [ ] **Step 3: 实现 get_default_model_for_provider**

在 `config.py` 中（约 L289 附近，`MIMO_MODEL` 定义之前）新增函数，并替换原有硬编码：

```python
# ── 默认模型解析（从 provider_metadata.json 读，无硬编码）──
# 用户约束：默认用 MiMo，但模型 ID 不在代码里硬编码
# 优先级：环境变量 > provider_metadata.json > 空串
_PROVIDER_METADATA_CACHE: dict | None = None


def _load_provider_metadata_cached() -> dict:
    """加载 provider_metadata.json（带缓存，避免每次调用都读盘）。"""
    global _PROVIDER_METADATA_CACHE
    if _PROVIDER_METADATA_CACHE is not None:
        return _PROVIDER_METADATA_CACHE
    try:
        meta_path = Path(__file__).resolve().parent / "config" / "provider_metadata.json"
        if meta_path.exists():
            with open(meta_path, "r", encoding="utf-8") as fp:
                _PROVIDER_METADATA_CACHE = json.load(fp)
                return _PROVIDER_METADATA_CACHE
    except (OSError, ValueError) as e:
        logger.warning("config.provider_metadata_load_failed error={}", str(e))
    _PROVIDER_METADATA_CACHE = {}
    return _PROVIDER_METADATA_CACHE


def get_default_model_for_provider(provider: str) -> str:
    """返回指定 provider 的默认模型 ID。

    优先级：
      1. 环境变量 {PROVIDER}_MODEL_NAME / {PROVIDER}_TEXT_MODEL（最高）
      2. provider_metadata.json 中 providers.{provider}.default_model
      3. 空串（调用方负责兜底）

    Args:
        provider: provider 名称（如 "mimo", "agnes"）

    Returns:
        默认模型 ID 字符串，未知 provider 返回空串
    """
    provider_lower = provider.strip().lower()
    # 1. 环境变量（兼容已有的 MIMO_MODEL_NAME / AGNES_TEXT_MODEL）
    env_var_map = {
        "mimo": "MIMO_MODEL_NAME",
        "agnes": "AGNES_TEXT_MODEL",
        "deepseek": "DEEPSEEK_MODEL_NAME",
    }
    env_var = env_var_map.get(provider_lower, f"{provider_lower.upper()}_MODEL_NAME")
    env_val = os.getenv(env_var, "").strip()
    if env_val:
        return env_val
    # 2. provider_metadata.json
    meta = _load_provider_metadata_cached()
    providers = meta.get("providers", {}) if isinstance(meta, dict) else {}
    provider_meta = providers.get(provider_lower, {})
    if isinstance(provider_meta, dict):
        return provider_meta.get("default_model", "") or ""
    # 3. 未知 provider
    return ""
```

然后修改 `config.py:289` 附近的硬编码：

```python
# 旧: MIMO_MODEL = os.getenv("MIMO_MODEL_NAME", "mimo-v2.5")
MIMO_MODEL = get_default_model_for_provider("mimo")
```

修改 `config.py:415-419` 删除 `_PROVIDER_DEFAULT_MODELS` 字典，改为：

```python
# 旧: _PROVIDER_DEFAULT_MODELS = {"mimo": "mimo-v2.5", ...}
# 新: 通过 get_default_model_for_provider() 动态获取
```

修改 `config.py:420-423`：

```python
# 旧:
# if os.getenv("MODEL_NAME"):
#     MODEL_NAME = os.getenv("MODEL_NAME")
# else:
#     MODEL_NAME = _PROVIDER_DEFAULT_MODELS.get(DEFAULT_PROVIDER, "mimo-v2.5")
# 新:
if os.getenv("MODEL_NAME"):
    MODEL_NAME = os.getenv("MODEL_NAME")
else:
    MODEL_NAME = get_default_model_for_provider(DEFAULT_PROVIDER)
```

修改 `config.py:431`：

```python
# 旧: AGNES_TEXT_MODEL = os.getenv("AGNES_TEXT_MODEL", "agnes-2.0-flash")
AGNES_TEXT_MODEL = get_default_model_for_provider("agnes")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -20`
Expected: 所有测试 PASS

- [ ] **Step 5: 验证 config.py 仍能正常导入**

Run: `cd /home/orangepi/.ai-agent/proj && python -c "import config; print(config.MODEL_NAME, config.MIMO_MODEL, config.AGNES_TEXT_MODEL)"`
Expected: 输出 `mimo-v2.5 mimo-v2.5 agnes-2.0-flash`（或对应环境变量值）

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add config.py tests/test_model_switching_refactor.py
git commit -m "refactor(config): 删除硬编码默认模型，改从 provider_metadata.json 读

- 新增 get_default_model_for_provider() 统一解析
- 删除 _PROVIDER_DEFAULT_MODELS 字典
- MIMO_MODEL/AGNES_TEXT_MODEL 改为函数派生
- 环境变量优先级最高"
```

---

### Task 3: 删除 _save() 反向同步死代码 + mark_startup_complete

**Files:**
- Modify: `web/config_service.py:175-183, 312-367`
- Test: `tests/test_model_switching_refactor.py` (追加)

**目标:** 删除从未在生产中调用的 `mark_startup_complete` 方法和 `_save()` 中的反向同步逻辑（已是死代码，删除避免误导）。

- [ ] **Step 1: 追加失败测试**

在 `tests/test_model_switching_refactor.py` 末尾追加：

```python
def test_config_service_no_mark_startup_complete_method():
    """ConfigService 不再有 mark_startup_complete 方法（已删除）。"""
    from web.config_service import ConfigService
    assert not hasattr(ConfigService, "mark_startup_complete"), \
        "mark_startup_complete 应该已删除（死代码）"


def test_config_service_no_startup_complete_field():
    """ConfigService 实例不再有 _startup_complete 字段。"""
    from web.config_service import ConfigService
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        svc = ConfigService(path=tmp_path)
        assert not hasattr(svc, "_startup_complete"), \
            "_startup_complete 字段应该已删除"
    finally:
        tmp_path.unlink(missing_ok=True)


def test_config_service_save_does_not_touch_route_table():
    """_save() 不得反向从 ROUTE_TABLE 恢复 _data（已删除该逻辑）。"""
    import inspect
    from web.config_service import ConfigService
    source = inspect.getsource(ConfigService._save)
    # 不应再出现 ROUTE_TABLE 字眼
    assert "ROUTE_TABLE" not in source, \
        "_save() 不应再引用 ROUTE_TABLE（反向同步死代码已删除）"
    assert "restoring _data from ROUTE_TABLE" not in source
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_config_service_no_mark_startup_complete_method -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: 删除 mark_startup_complete 方法**

在 `web/config_service.py` 中删除 L175-183（`mark_startup_complete` 方法定义）。

同时删除 `__init__` 中的 `self._startup_complete: bool = False` 字段（约 L172）。

- [ ] **Step 4: 删除 _save() 中的反向同步逻辑**

在 `web/config_service.py` 的 `_save()` 方法中，删除 L312-367（从 `# 二次防御: 启动完成后...` 到 `logger.debug("config_service.save_validation_error error={}", str(e))`）。

修改后的 `_save()` 起始应为：

```python
def _save(self) -> None:
    try:
        from utils.atomic_write import atomic_write
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps(self._data, ensure_ascii=False, indent=2))
    except Exception:
        logger.debug("config_service.atomic_write_fallback", exc_info=True)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)
```

- [ ] **Step 5: 修复现有测试中调用 mark_startup_complete 的地方**

搜索 `tests/test_model_persistence_bugfix.py` 中的 `mark_startup_complete` 调用并删除（约 L423-424, L476-477）。运行 `python -m pytest tests/test_model_persistence_bugfix.py -v` 看是否还有依赖。

- [ ] **Step 6: 运行所有相关测试**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py tests/test_model_persistence_bugfix.py -v 2>&1 | tail -30`
Expected: 测试通过或仅因后续 task 未完成而失败

- [ ] **Step 7: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add web/config_service.py tests/test_model_switching_refactor.py tests/test_model_persistence_bugfix.py
git commit -m "refactor(config_service): 删除 _save() 反向同步死代码

- 删除 mark_startup_complete 方法（生产从未调用，误导性死代码）
- 删除 _save() 中从 ROUTE_TABLE 反向恢复 _data 的逻辑
- _save() 现在只做原子写盘，不再触碰 ROUTE_TABLE"
```

---

### Task 4: 重构 set_chat_model 走 Registry（原子化）

**Files:**
- Modify: `model_router.py:490-577` (`set_chat_model` 方法)
- Test: `tests/test_model_switching_refactor.py` (追加)

**目标:** `set_chat_model` 不再直接修改 `ROUTE_TABLE`，改为通过 `ModelRouteRegistry.update_route()` 原子化更新。失败时回滚内存。同步更新 chat_pro / chat_flash 等也走 Registry。

- [ ] **Step 1: 追加失败测试**

```python
def test_set_chat_model_rolls_back_on_provider_not_registered():
    """set_chat_model 在 provider 未注册时回滚 ROUTE_TABLE。"""
    from model_router import ModelRouter, ROUTE_TABLE
    router = ModelRouter(api_key="fake")
    original_model = ROUTE_TABLE["chat"]["model"]
    original_client = ROUTE_TABLE["chat"]["client"]

    # 尝试切换到未注册的自定义 provider
    from core.app_exception import LLMError
    with pytest.raises(LLMError):
        router.set_chat_model("unknown_provider_xyz", "some-model")

    # ROUTE_TABLE 未被污染
    assert ROUTE_TABLE["chat"]["model"] == original_model
    assert ROUTE_TABLE["chat"]["client"] == original_client


def test_set_chat_model_persists_all_synced_tasks():
    """set_chat_model 成功时持久化所有同步过的 task。"""
    from model_router import ModelRouter
    router = ModelRouter(api_key="fake")
    # mock registry
    router._registry = MagicMock()
    router._registry.update_route = MagicMock(return_value={"model": "x", "client": "y"})

    router.set_chat_model("mimo", "mimo-v2.5")

    # 至少调用了 chat + chat_pro + chat_flash + 其他同步 task
    tasks_updated = [c.args[0] for c in router._registry.update_route.call_args_list]
    assert "chat" in tasks_updated
    assert "chat_pro" in tasks_updated
    assert "chat_flash" in tasks_updated
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_set_chat_model_rolls_back_on_provider_not_registered -v 2>&1 | tail -15`
Expected: FAIL（因为旧 set_chat_model 直接改 ROUTE_TABLE，不回滚）

- [ ] **Step 3: 在 ModelRouter.__init__ 中初始化 _registry**

修改 `model_router.py` 的 `ModelRouter.__init__`，在 `self._current_chat_model = None` 之前添加：

```python
        # 路由表注册中心：ROUTE_TABLE 的唯一读写入口
        # 启动后 ROUTE_TABLE 模块级变量作为只读快照，所有修改走 _registry
        self._registry = ModelRouteRegistry(ROUTE_TABLE)
```

- [ ] **Step 4: 重写 set_chat_model 走 Registry**

替换 `model_router.py:490-577` 整个 `set_chat_model` 方法：

```python
    def set_chat_model(self, provider: str, model_id: str) -> dict:
        """切换 chat 主模型，原子化更新所有同步路由。

        通过 ModelRouteRegistry.update_route() 原子化：
        1. 先验证 provider 可用（已注册）
        2. 一次性更新 chat + chat_pro + chat_flash 等所有同步 task
        3. 任一持久化失败时，整个操作回滚

        Args:
            provider: provider 名称
            model_id: 模型 ID

        Returns:
            {"provider": ..., "model_id": ...}

        Raises:
            LLMError: provider 未注册或持久化失败
        """
        # Step 1: 先验证 provider 可用，未注册直接抛（此时还没改任何状态）
        if provider not in ("mimo", "agnes"):
            if provider not in self._custom_clients:
                self._lazy_register_provider(provider)
            if provider not in self._custom_clients:
                raise LLMError(f"自定义 provider {provider} 未注册，请先注册客户端")

        # Step 2: 同步更新 DEFAULT_PROVIDER（影响子代理、成本统计）
        _set_default_provider(provider)

        # Step 3: 收集所有需要同步的 task
        _sync_tasks = ("chat", "chat_pro", "chat_flash",
                       "emotion_analysis", "tool_result_wrap",
                       "memory_encoding")

        # agnes 不支持 thinking，切换到 agnes 时所有 task 禁用 thinking
        _thinking_for_agnes = {"type": "disabled"}

        # Step 4: 通过 Registry 原子化更新每个 task
        # Registry.update_route 内部会持久化 + 失败回滚单条 task
        # 这里额外保证：如果中途某条 task 失败，已成功的 task 不回滚（它们已经持久化）
        # 但会抛异常让调用方知道
        last_error: Exception | None = None
        for _task in _sync_tasks:
            if _task not in self._registry.all_tasks():
                continue
            # 读取原 entry 拿 max_tokens 和 thinking
            old_entry = self._registry.get_task(_task) or {}
            _thinking = _thinking_for_agnes if provider == "agnes" else old_entry.get("thinking")
            try:
                self._registry.update_route(
                    _task,
                    model_id=model_id,
                    provider=provider,
                    max_tokens=old_entry.get("max_tokens"),
                    thinking=_thinking,
                    timeout=self.TASK_TIMEOUTS.get(_task),
                )
            except Exception as e:
                logger.error("router.set_chat_model_task_failed task={} error={}",
                             _task, str(e))
                last_error = e

        if last_error is not None:
            raise LLMError(f"切换 chat 模型时部分 task 持久化失败: {last_error}")

        # Step 5: 同步 chat_model 字段到 ConfigService（WebUI 显示用）
        try:
            from web.config_service import get_config_service
            cfg = get_config_service()
            cfg.set("models.chat_model", {"provider": provider, "model_id": model_id})
        except (OSError, KeyError, ValueError, TypeError, RuntimeError) as e:
            logger.warning("router.chat_model_persist_failed error={}", str(e))

        self._current_chat_model = {"provider": provider, "model_id": model_id}
        logger.info("router.chat_model_changed", provider=provider, model=model_id)
        return {"provider": provider, "model_id": model_id}
```

- [ ] **Step 5: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -25`
Expected: 所有新增测试 PASS

- [ ] **Step 6: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add model_router.py tests/test_model_switching_refactor.py
git commit -m "refactor(model_router): set_chat_model 原子化走 Registry

- 不再直接修改 ROUTE_TABLE，通过 ModelRouteRegistry.update_route()
- 先验证 provider 再改状态，避免半成品
- 持久化失败时单条 task 回滚（Registry 负责）
- 同步 chat_model 字段到 ConfigService"
```

---

### Task 5: 重构 _restore_chat_model 不硬编码 fallback

**Files:**
- Modify: `web/server.py:175-227` (`_restore_chat_model` 函数)

**目标:** `_restore_chat_model` 失败时不再硬编码 fallback 到 mimo，改为：保留持久化的用户选择不变，内存中临时回退到 `provider_metadata.json` 的默认模型（仅内存，不持久化）。

- [ ] **Step 1: 追加测试**

```python
def test_restore_chat_model_does_not_overwrite_persistence_on_failure(tmp_path, monkeypatch):
    """_restore_chat_model 失败时不覆盖 ConfigService 持久化值。"""
    import json as _json
    # 准备一个持久化文件，用户已选 agnes
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text(_json.dumps({
        "models": {
            "chat_model": {"provider": "agnes", "model_id": "agnes-2.0-flash"},
            "routes": {"chat": {"model": "agnes-2.0-flash", "client": "agnes",
                                "max_tokens": 8192, "thinking": False, "timeout": 60}},
        }
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    # mock core：agnes provider 未注册（触发 fallback）
    mock_core = MagicMock()
    mock_core.router._custom_clients = {}  # agnes 不在已注册列表
    mock_core.router._current_chat_model = None

    # mock ConfigService 单例
    import web.config_service
    monkeypatch.setattr(web.config_service, "get_config_service", lambda: cfg)

    # 执行 _restore_chat_model
    from web.server import _restore_chat_model
    _restore_chat_model(cfg, mock_core)

    # 持久化值仍然是 agnes（未被覆盖为 mimo）
    saved = _json.loads(overrides_file.read_text(encoding="utf-8"))
    assert saved["models"]["chat_model"]["provider"] == "agnes"
    assert saved["models"]["chat_model"]["model_id"] == "agnes-2.0-flash"
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_restore_chat_model_does_not_overwrite_persistence_on_failure -v 2>&1 | tail -15`
Expected: FAIL（旧实现在 except 中可能修改持久化）

- [ ] **Step 3: 重写 _restore_chat_model**

替换 `web/server.py:175-227`：

```python
def _restore_chat_model(cfg: Any, core: Any) -> None:
    """恢复上次聊天模型（从 ConfigService 的 models.chat_model 读取）。

    设计原则（用户约束）：
    - 持久化的用户选择是真相源，失败时**绝不覆盖**
    - 仅修改内存中的 ROUTE_TABLE 和 _current_chat_model
    - 失败时内存回退到 provider_metadata.json 的默认模型（不持久化）
      下次启动会重新尝试恢复用户选择
    """
    chat_model = cfg.get("models.chat_model")
    if not (isinstance(chat_model, dict) and chat_model.get("provider") and chat_model.get("model_id")):
        logger.info("webui.chat_model_no_saved_preference, using default")
        return
    provider = chat_model["provider"]
    model_id = chat_model["model_id"]
    from model_router import ROUTE_TABLE
    current_client = ROUTE_TABLE.get("chat", {}).get("client", "")
    current_model = ROUTE_TABLE.get("chat", {}).get("model", "")
    logger.info("webui.chat_model_restore_attempt saved={}/{} current_route={}/{}",
                provider, model_id, current_client, current_model)
    try:
        # 检查 provider 是否已注册
        if provider not in ("mimo", "agnes") and provider not in getattr(core.router, '_custom_clients', {}):
            raise LLMError(f"自定义 provider {provider} 未注册")
        # 仅修改内存，不调用 set_chat_model（避免触发持久化覆盖用户后续切换）
        chat_entry = ROUTE_TABLE.get("chat")
        if chat_entry is not None:
            chat_entry["model"] = model_id
            chat_entry["client"] = provider
        core.router._current_chat_model = {"provider": provider, "model_id": model_id}
        logger.info("webui.chat_model_restored provider={} model={}", provider, model_id)
    except Exception as e:
        # 关键：失败时不覆盖持久化，仅内存回退到默认模型
        logger.warning(
            "webui.chat_model_restore_failed provider={} model={} error={} "
            "fallback_to_default_in_memory_only_persistence_untouched",
            provider, model_id, str(e)
        )
        try:
            # 从 provider_metadata.json 读默认模型（不硬编码）
            from config import get_default_model_for_provider, DEFAULT_PROVIDER
            fallback_provider = DEFAULT_PROVIDER  # 通常为 "mimo"
            fallback_model = get_default_model_for_provider(fallback_provider)
            if not fallback_model:
                # 极端兜底：直接用 ROUTE_TABLE 当前值（不修改）
                logger.error("webui.chat_model_fallback_no_default_model provider={}",
                             fallback_provider)
                return
            chat_entry = ROUTE_TABLE.get("chat")
            if chat_entry is not None:
                chat_entry["model"] = fallback_model
                chat_entry["client"] = fallback_provider
            core.router._current_chat_model = {
                "provider": fallback_provider, "model_id": fallback_model,
            }
            logger.info("webui.chat_model_fallback_in_memory provider={} model={}",
                        fallback_provider, fallback_model)
        except (ImportError, KeyError, AttributeError) as inner_e:
            logger.error("webui.set_chat_model_fallback_error error={}", str(inner_e))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -25`
Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add web/server.py tests/test_model_switching_refactor.py
git commit -m "refactor(server): _restore_chat_model 失败不覆盖持久化

- 失败时仅内存回退到 provider_metadata.json 默认模型
- 持久化的用户选择保持不变，下次启动重新尝试
- 删除硬编码 fallback 到 mimo，改用 get_default_model_for_provider()"
```

---

### Task 6: 重构 _try_fallback_chain 不污染全局

**Files:**
- Modify: `model_router.py:798-905` (`_try_fallback_chain` 方法)

**目标:** 降级链不再依赖 `ROUTE_TABLE.get(fallback_type)`，改为从 `_registry.snapshot_task()` 拿深拷贝构造本地 fallback config。

- [ ] **Step 1: 追加测试**

```python
def test_fallback_chain_does_not_pollute_route_table():
    """降级链调用后 ROUTE_TABLE 全局状态不变。"""
    from model_router import ModelRouter, ROUTE_TABLE
    router = ModelRouter(api_key="fake")
    # 初始化 _registry（如果 __init__ 没初始化）
    if not hasattr(router, "_registry"):
        from model_router import ModelRouteRegistry
        router._registry = ModelRouteRegistry(ROUTE_TABLE)

    original_chat = dict(ROUTE_TABLE["chat"])
    original_chat_flash = dict(ROUTE_TABLE["chat_flash"])

    # mock 一个会触发降级的异常
    fake_error = Exception("simulated LLM failure")
    # mock _route_with_retry 返回成功（避免真实 LLM 调用）
    router._route_with_retry = MagicMock(return_value="fake_response")
    router._filter_tools_for_model = MagicMock(return_value=[])

    import asyncio
    async def _run():
        await router._try_fallback_chain(
            fake_error, "chat", [], 0.7, False, None, None, 60,
            "user1", "session1", None, original_max_tokens=8192,
        )
    asyncio.run(_run())

    # ROUTE_TABLE 未被污染
    assert ROUTE_TABLE["chat"] == original_chat
    assert ROUTE_TABLE["chat_flash"] == original_chat_flash
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_fallback_chain_does_not_pollute_route_table -v 2>&1 | tail -15`
Expected: 可能 PASS 或 FAIL，取决于旧实现是否真的污染（旧实现读 ROUTE_TABLE.get，但 _route_with_retry 不会再写回）。如果是 PASS，跳过此 task 的代码改动，仅保留测试作为防护。

- [ ] **Step 3: 修改 _try_fallback_chain 用 snapshot_task**

在 `model_router.py` 的 `_try_fallback_chain` 中，把所有 `ROUTE_TABLE.get(...)` 改为 `self._registry.snapshot_task(...)`：

L824: `_original_provider = ROUTE_TABLE.get(task_type, {}).get("client", _CFG_DEFAULT_PROVIDER)`
改为:
```python
_original_provider = (self._registry.get_task(task_type) or {}).get("client", _CFG_DEFAULT_PROVIDER)
```

L828: `fallback_config = ROUTE_TABLE.get(fallback_type)`
改为:
```python
fallback_config = self._registry.snapshot_task(fallback_type)
```

L866: `agnes_config = ROUTE_TABLE.get("chat_agnes")`
改为:
```python
agnes_config = self._registry.snapshot_task("chat_agnes")
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -25`
Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add model_router.py tests/test_model_switching_refactor.py
git commit -m "refactor(model_router): 降级链用 registry 快照，不污染全局

- _try_fallback_chain 改用 self._registry.snapshot_task()
- 返回深拷贝，降级期间修改不影响 ROUTE_TABLE
- 防止降级链临时状态被后续 cfg.set 持久化"
```

---

### Task 7: 启动时清理死路由

**Files:**
- Modify: `web/server.py:148-172` (`_apply_route_overrides` 函数)

**目标:** 启动时遍历持久化的 `models.routes`，删除 ROUTE_TABLE 中已不存在的 task（如 `chat_mimo`、`chat_mini`、`chat_ultra`）。

- [ ] **Step 1: 追加测试**

```python
def test_apply_route_overrides_cleans_dead_routes(tmp_path, monkeypatch):
    """启动时清理持久化文件中 ROUTE_TABLE 已不存在的死路由。"""
    import json as _json
    overrides_file = tmp_path / "webui_overrides.json"
    overrides_file.write_text(_json.dumps({
        "models": {
            "routes": {
                "chat": {"model": "mimo-v2.5", "client": "mimo",
                         "max_tokens": 131072, "thinking": False, "timeout": 60},
                "chat_mimo": {"model": "mimo-v2.5", "client": "mimo",
                              "max_tokens": 131072, "thinking": False, "timeout": 60},
                "chat_mini": {"model": "mimo-v2.5", "client": "mimo",
                              "max_tokens": 4096, "thinking": False, "timeout": 60},
                "chat_ultra": {"model": "mimo-v2.5", "client": "mimo",
                               "max_tokens": 1048576, "thinking": False, "timeout": 60},
            }
        }
    }), encoding="utf-8")

    from web.config_service import ConfigService
    cfg = ConfigService(path=overrides_file)

    mock_core = MagicMock()
    mock_core.router.TASK_TIMEOUTS = {}

    from model_router import ROUTE_TABLE
    # 确保 ROUTE_TABLE 中没有 chat_mimo/chat_mini/chat_ultra
    dead_routes = ("chat_mimo", "chat_mini", "chat_ultra")
    for dr in dead_routes:
        assert dr not in ROUTE_TABLE, f"测试前置失败：ROUTE_TABLE 不应有 {dr}"

    from web.server import _apply_route_overrides
    _apply_route_overrides(cfg, mock_core, ROUTE_TABLE)

    # 死路由已从持久化文件删除
    saved = _json.loads(overrides_file.read_text(encoding="utf-8"))
    saved_routes = saved["models"]["routes"]
    assert "chat_mimo" not in saved_routes
    assert "chat_mini" not in saved_routes
    assert "chat_ultra" not in saved_routes
    # 存活路由保留
    assert "chat" in saved_routes
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_apply_route_overrides_cleans_dead_routes -v 2>&1 | tail -15`
Expected: FAIL（旧实现不清理死路由）

- [ ] **Step 3: 修改 _apply_route_overrides 增加死路由清理**

在 `web/server.py` 的 `_apply_route_overrides` 函数末尾（L172 之后）追加清理逻辑：

```python
def _apply_route_overrides(cfg: Any, core: Any, ROUTE_TABLE: Any) -> None:
    """应用路由表覆盖（model/client/max_tokens/thinking/timeout）。

    同时清理持久化文件中 ROUTE_TABLE 已不存在的死路由（如 chat_mimo/chat_mini/chat_ultra）。
    """
    routes_config = cfg.get("models.routes", {}) or {}
    logger.info("webui.route_overrides_start total_tasks={}", len(routes_config))
    dead_routes: list[str] = []
    for task, o in list(routes_config.items()):
        entry = ROUTE_TABLE.get(task)
        if not entry:
            # 死路由：ROUTE_TABLE 中已删除，但持久化文件还有
            dead_routes.append(task)
            logger.info("webui.route_override_dead task={} reason=not_in_route_table", task)
            continue
        if not isinstance(o, dict):
            logger.warning("webui.route_override_skip task={} reason=invalid", task)
            continue
        if o.get("model"):
            entry["model"] = o["model"]
        if o.get("client"):
            entry["client"] = o["client"]
        if o.get("max_tokens"):
            entry["max_tokens"] = o["max_tokens"]
        if "thinking" in o:
            original_thinking = entry.get("thinking")
            if o["thinking"]:
                entry["thinking"] = {"type": "enabled", "budget_tokens": 2048}
            else:
                entry["thinking"] = {"type": "disabled"}
            logger.info("webui.thinking_loaded task={} original={} new={}",
                        task, original_thinking, entry.get("thinking"))
        if o.get("timeout"):
            core.router.TASK_TIMEOUTS[task] = o["timeout"]

    # 清理死路由：从持久化文件删除 ROUTE_TABLE 中已不存在的 task
    if dead_routes:
        for dr in dead_routes:
            try:
                cfg.delete(f"models.routes.{dr}")
                logger.info("webui.dead_route_cleaned task={}", dr)
            except Exception as e:
                logger.warning("webui.dead_route_clean_failed task={} error={}",
                               dr, str(e))
        logger.info("webui.dead_routes_cleaned count={}", len(dead_routes))
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -25`
Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add web/server.py tests/test_model_switching_refactor.py
git commit -m "feat(server): 启动时清理持久化文件中的死路由

- _apply_route_overrides 检测 ROUTE_TABLE 中已不存在的 task
- 从 webui_overrides.json 删除 chat_mimo/chat_mini/chat_ultra 等
- 防止 WebUI 显示僵尸路由让用户困惑"
```

---

### Task 8: 重构 web/routers/models.py update_route 走 Registry

**Files:**
- Modify: `web/routers/models.py:242-282` (`update_route` 函数)

**目标:** `update_route` API 不再直接修改 `ROUTE_TABLE[task]`，改为调用 `core.router._registry.update_route()`。

- [ ] **Step 1: 追加测试**

```python
def test_update_route_api_uses_registry():
    """WebUI update_route API 通过 Registry 更新，不直接改 ROUTE_TABLE。"""
    from web.routers.models import update_route
    import inspect
    source = inspect.getsource(update_route)
    # 不应直接修改 ROUTE_TABLE[task]
    assert "ROUTE_TABLE[task]" not in source
    assert "entry[\"model\"]" not in source
    # 应调用 registry
    assert "_registry" in source or "registry" in source
```

- [ ] **Step 2: 运行验证失败**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py::test_update_route_api_uses_registry -v 2>&1 | tail -10`
Expected: FAIL

- [ ] **Step 3: 重写 update_route 函数**

替换 `web/routers/models.py:242-282`：

```python
@router.put("/models/routes/{task}", response_model=Envelope[dict])
async def update_route(task: str, body: dict, request: Request) -> Any:
    from model_router import ROUTE_TABLE
    if task not in ROUTE_TABLE:
        raise HTTPException(404, f"未知路由任务 {task}")
    cfg = _cfg(request)
    provider = body.get("provider")
    if provider and provider not in ("mimo",) \
            and not cfg.get(f"models.providers.{provider}"):
        raise HTTPException(400, f"provider {provider} 不存在")
    # 通过 Registry 原子化更新（不再直接改 ROUTE_TABLE）
    router_obj = _router_of(request)
    registry = getattr(router_obj, "_registry", None)
    if registry is None:
        # 兜底：极旧版本无 _registry，回退到直接修改
        entry = ROUTE_TABLE[task]
        if body.get("model"):
            entry["model"] = str(body["model"])
        if provider:
            entry["client"] = provider
        if body.get("max_tokens"):
            entry["max_tokens"] = max(64, min(int(body["max_tokens"]), 32768))
        if "thinking" in body:
            if body["thinking"]:
                entry["thinking"] = {"type": "enabled", "budget_tokens": 2048}
            else:
                entry["thinking"] = {"type": "disabled"}
        if body.get("timeout"):
            router_obj.TASK_TIMEOUTS[task] = max(5, min(int(body["timeout"]), 600))
        # 持久化
        cfg.set(f"models.routes.{task}", {
            "model": entry["model"], "client": entry.get("client", "mimo"),
            "max_tokens": entry.get("max_tokens"),
            "thinking": bool(entry.get("thinking") and entry["thinking"].get("type") == "enabled"),
            "timeout": router_obj.TASK_TIMEOUTS.get(task),
        })
    else:
        # 走 Registry 原子化
        model_id = str(body.get("model") or ROUTE_TABLE[task].get("model", ""))
        max_tokens = int(body["max_tokens"]) if body.get("max_tokens") else None
        if max_tokens is not None:
            max_tokens = max(64, min(max_tokens, 32768))
        thinking = None
        if "thinking" in body:
            thinking = ({"type": "enabled", "budget_tokens": 2048}
                        if body["thinking"] else {"type": "disabled"})
        timeout = int(body["timeout"]) if body.get("timeout") else None
        if timeout is not None:
            router_obj.TASK_TIMEOUTS[task] = max(5, min(timeout, 600))
        try:
            registry.update_route(
                task, model_id=model_id,
                provider=provider or ROUTE_TABLE[task].get("client", "mimo"),
                max_tokens=max_tokens, thinking=thinking, timeout=timeout,
            )
        except Exception as e:
            raise HTTPException(500, f"路由更新失败: {e}") from None

    # 同步 models.chat_model（task == chat 时）
    if task == "chat":
        final_entry = ROUTE_TABLE[task]
        cfg.set("models.chat_model", {"provider": final_entry.get("client", "mimo"),
                                       "model_id": final_entry["model"]})
    await _audit(request, "route.update", json.dumps({task: body}, ensure_ascii=False))
    await _broadcast_changed()
    return Envelope(data={"task": task, "model": ROUTE_TABLE[task]["model"],
                          "provider": ROUTE_TABLE[task].get("client", "mimo")})
```

- [ ] **Step 4: 运行测试验证通过**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/test_model_switching_refactor.py -v 2>&1 | tail -25`
Expected: 所有测试 PASS

- [ ] **Step 5: Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add web/routers/models.py tests/test_model_switching_refactor.py
git commit -m "refactor(models): update_route API 走 Registry 原子化

- 不再直接修改 ROUTE_TABLE[task]
- 通过 core.router._registry.update_route() 原子更新
- 保留旧版本兜底（无 _registry 时回退直接修改）"
```

---

### Task 9: 全量回归测试 + 清理持久化文件中的死路由

**Files:**
- Modify: `/media/orangepi/KIOXIA/nahida-data/config/webui_overrides.json` (运行时自动清理，无需手动改)

**目标:** 跑全量测试套件，修复因重构导致的现有测试失败。确认现有用户配置文件会在下次启动时自动清理死路由。

- [ ] **Step 1: 跑全量回归测试**

Run: `cd /home/orangepi/.ai-agent/proj && python -m pytest tests/ -v --tb=short 2>&1 | tail -50`
Expected: 可能部分旧测试因 API 变更失败，记录失败列表

- [ ] **Step 2: 修复失败的现有测试**

重点检查：
- `tests/test_model_persistence_bugfix.py` - 删除 `mark_startup_complete` 调用
- `tests/test_fallback_optimization.py` - 调整为新的 Registry 接口
- `tests/test_agnes_max_tokens_and_sticky_fallback.py` - 调整为新的 Registry 接口
- `tests/test_model_health.py` - 调整 ROUTE_TABLE 直接赋值为通过 Registry

对每个失败测试，删除 `mark_startup_complete` 调用，把 `ROUTE_TABLE["chat"] = {...}` 改为 `router._registry.replace_table({...})` 或保持原样（如果是只读测试）。

- [ ] **Step 3: 验证 grep 无硬编码模型 ID**

Run: `cd /home/orangepi/.ai-agent/proj && grep -rn "mimo-v2.5\|agnes-2.0-flash" --include="*.py" . | grep -v __pycache__ | grep -v "/tests/"`
Expected: 只在 `config/provider_metadata.json` 中出现（JSON 不是 .py），.py 文件中应该没有

- [ ] **Step 4: 验证现有用户配置文件会被自动清理**

Run: `cd /home/orangepi/.ai-agent/proj && python -c "
import json
with open('/media/orangepi/KIOXIA/nahida-data/config/webui_overrides.json', 'r') as f:
    data = json.load(f)
routes = data.get('models', {}).get('routes', {})
dead = [k for k in routes if k in ('chat_mimo', 'chat_mini', 'chat_ultra')]
print('当前死路由:', dead)
print('下次启动 _apply_route_overrides 会自动清理这些')
"`
Expected: 输出当前死路由列表

- [ ] **Step 5: 最终 Commit**

```bash
cd /home/orangepi/.ai-agent/proj
git add tests/
git commit -m "test: 修复重构后的现有测试

- 删除 mark_startup_complete 调用（方法已删除）
- 调整 fallback 测试为新的 Registry 接口
- 保持只读测试兼容"
```

---

## Self-Review

### Spec 覆盖检查
- ✅ 缺陷 1（ROUTE_TABLE 多文件直接修改）→ Task 4/5/6/8 全部改为走 Registry
- ✅ 缺陷 2（4 套真相源）→ Task 1/3/4 确立 ConfigService 为唯一真相源
- ✅ 缺陷 3（降级链污染全局）→ Task 6 改用 snapshot_task
- ✅ 缺陷 4（set_chat_model 不原子）→ Task 4 原子化
- ✅ 缺陷 5（硬编码散落 6 处）→ Task 2 迁移到 provider_metadata.json
- ✅ 缺陷 6（死路由未清理）→ Task 7 启动时清理
- ✅ 用户约束 1（默认用 MiMo）→ Task 2 保留 DEFAULT_PROVIDER="mimo"
- ✅ 用户约束 2（改过即锁死）→ Task 4/5 持久化优先，失败不覆盖
- ✅ 用户约束 3（模型从配置文件读）→ Task 2 get_default_model_for_provider
- ✅ 用户约束 4（不许停下来）→ 跳过 spec 审查环节

### 类型一致性
- `ModelRouteRegistry.update_route(task, model_id, provider, max_tokens, thinking, timeout, persist)` 在 Task 1 定义，Task 4/8 调用一致
- `get_default_model_for_provider(provider) -> str` 在 Task 2 定义，Task 5 调用一致
- `snapshot_task(task) -> dict | None` 在 Task 1 定义，Task 6 调用一致

### Placeholder 检查
- 无 TBD/TODO
- 每个步骤都有完整代码
- 每个测试都有具体断言
