# Device Registry Backend Binding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `DeviceRegistry.backend(provider, device_id=None)` 对零个、单个和多个 provider binding 做确定选择，并验证 CUDA 多卡健康状态影响推荐结果。

**Architecture:** 保留现有 `_backends` 作为 binding 唯一来源，在查询时先按 provider 收集全部 binding，再按严格整数 `options["device_id"]` 过滤。推荐逻辑继续消费设备上各自绑定的健康状态，不引入新的缓存或选择层。

**Tech Stack:** Python、pytest、ONNX Runtime 测试替身

## Global Constraints

- 严格执行 RED、GREEN、REFACTOR。
- 不修改无关模块，不提交代码。
- `device_id` 匹配必须使用严格 `int` 语义，排除字符串和布尔值。

---

### Task 1: Backend Binding 查询

**Files:**
- Modify: `tests/test_local_ai_device_registry.py`
- Modify: `local_ai/devices/registry.py:450-463`

**Interfaces:**
- Consumes: `DeviceRegistry._backends` 与 `ExecutionBackend.options`
- Produces: `DeviceRegistry.backend(provider: str, device_id: int | None = None) -> ExecutionBackend`

- [ ] **Step 1: 写入失败测试**
- [ ] **Step 2: 运行 backend 定向测试并确认因签名、歧义处理或严格匹配缺失而失败**
- [ ] **Step 3: 实现按 provider 收集、歧义报错与唯一严格整数匹配**
- [ ] **Step 4: 重跑 backend 定向测试并确认通过**

### Task 2: CUDA 双卡健康推荐

**Files:**
- Modify: `tests/test_local_ai_device_registry.py`
- Modify: `local_ai/devices/registry.py` only if the failing test proves recommendation logic is incorrect

**Interfaces:**
- Consumes: `DeviceRegistry.scan()`、`DeviceRegistry.backend()`、`DeviceRegistry.recommend()`
- Produces: CUDA 每卡独立健康查询与健康卡推荐回归保障

- [ ] **Step 1: 构造 CUDA 0 号卡探测失败、1 号卡探测成功的测试替身**
- [ ] **Step 2: 运行测试并确认缺失的按卡 backend 查询先失败**
- [ ] **Step 3: 使用最小生产改动让按卡查询与健康卡推荐通过**
- [ ] **Step 4: 运行相关 pytest、Ruff、compileall、diff check 与编辑器诊断**
