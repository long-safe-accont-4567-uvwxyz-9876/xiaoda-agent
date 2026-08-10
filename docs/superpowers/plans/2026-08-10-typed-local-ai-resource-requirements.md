# 类型化本地 AI 资源要求实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将本地 AI 设备推荐从歧义通用内存迁移到严格类型化 RAM/VRAM，并修复共享硬件多 backend 状态覆盖。

**Architecture:** 在设备注册表入口统一解析和验证资源要求，兼容层只接受旧通用零值。候选筛选分别读取主机 RAM 与候选设备 VRAM，运行配置使用两个独立估算字段；设备重扫按 backend 健康集合合并设备状态。

**Tech Stack:** Python 3.11、dataclasses、pytest、Ruff。

## Global Constraints

- 新选择严格使用类型化 RAM/VRAM。
- 旧通用正值自动选择必须拒绝并报告，不猜测资源种类。
- 不覆盖或提交工作区中的用户既有改动。

---

### Task 1: 类型化资源契约

**Files:**
- Modify: `local_ai/contracts.py`
- Test: `tests/test_local_ai_contracts.py`

**Interfaces:**
- Produces: `RuntimeProfile.estimated_ram: int`。
- Produces: `RuntimeProfile.estimated_vram: int`。

- [x] **Step 1: 编写拆分字段及旧 payload 拒绝测试**
- [x] **Step 2: 运行测试并确认因新字段不存在而失败**
- [x] **Step 3: 将 `estimated_memory` 替换为 RAM/VRAM 独立字段**
- [x] **Step 4: 运行契约测试并确认通过**

### Task 2: 严格资源解析和推荐

**Files:**
- Modify: `local_ai/devices/registry.py`
- Modify: `local_ai/devices/__init__.py`
- Test: `tests/test_local_ai_device_registry.py`

**Interfaces:**
- Produces: `InvalidResourceRequirementsError`。
- Consumes: `minimum_ram`、`recommended_ram`、`minimum_vram`、`recommended_vram`。

- [x] **Step 1: 编写旧正值、非法类型、RAM/VRAM 独立校验测试**
- [x] **Step 2: 运行测试并确认缺少异常类型而失败**
- [x] **Step 3: 实现严格解析、候选过滤、排序和运行配置输出**
- [x] **Step 4: 运行注册表与契约测试并确认通过**

### Task 3: 共享 GPU backend 状态合并

**Files:**
- Modify: `local_ai/devices/registry.py`
- Test: `tests/test_local_ai_device_registry.py`

**Interfaces:**
- Consumes: 同一 `ComputeDevice.id` 下的全部 `ExecutionBackend`。
- Produces: 由 backend 健康集合决定的 `DeviceState`。

- [x] **Step 1: 编写 peer provider 失败与消失的回归测试**
- [x] **Step 2: 确认原状态覆盖逻辑不满足测试**
- [x] **Step 3: 合并当前与消失 backend；任一健康即 AVAILABLE，全部不健康为 DEGRADED**
- [x] **Step 4: 运行相关回归并确认通过**

### Task 4: 验证与报告

**Files:**
- Modify: `task-3-report.md`
- Modify: `.superpowers/sdd/task-3-report.md`
- Modify: `.superpowers/sdd/progress.md`

**Interfaces:**
- Produces: 可复核的测试、静态检查、诊断和审查证据。

- [x] **Step 1: 运行 Local AI 相关完整测试集**
- [x] **Step 2: 运行 Ruff、compileall、差异空白和编辑器诊断**
- [x] **Step 3: 执行 v4 修复范围代码审查**
- [x] **Step 4: 记录结果并关闭 Task 3 完成门禁**
