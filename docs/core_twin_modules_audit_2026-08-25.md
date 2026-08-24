# core/ 孪生模块合并审计（2026-08-25）

> 技术债扫描 P2 项产出。起因：`ls core/` 发现疑似同域孪生命名
> （degradation 三兄弟、meta_cognition 双胞胎）。逐对核验后结论如下。

## 一、degradation 三模块 —— 已由并行会话解决 / 属合理分层

| 模块 | 状态 |
|---|---|
| `core/degradation.py` | ✅ 死模块，已由 `1af8fc24` 删除并同步文档对账 |
| `core/degradation_detector.py` | 在用。感知层：Axis/Severity/MetricDeviation/DegradationReport 多轴指标偏移检测 |
| `core/degradation_strategy.py` | 在用。响应层：DegradationLevel 分级 + LevelChangeEvent + 降级策略执行 |

detector(检测) 与 strategy(响应) 是感知-决策分层而非重复实现，
二者经 `wire_auto_trigger()` 显式接线（strategy.py:415）。**无需合并**。

## 二、meta_cognition 双胞胎 —— 假孪生，一个是死模块

| 模块 | 行数 | 职责 | 生产引用 |
|---|---|---|---|
| `core/meta_cognition.py` | 95 | `AgentSelfState` 数据类（confidence/fatigue/error_rate/memory_pressure 健康分） | **零**。仅 `tests/test_phase1_5_modules.py:217-241` 引用 |
| `core/metacognition_lite.py` | 249 | 5 阶段推理时反幻觉+漂移检测管线（Anticipate→Plan→Monitor→Reflect→Regulate） | `core/agent_introspection.py` |

二者除名字相近外**职责零重叠**：前者是运行时自省状态快照，
后者是 LLM 推理质量元认知（MaR 论文参考实现）。不可合并。

### 处置建议（P3 优先级）

~~本次审计不动手删除~~ → **已删除**（2026-08-25 下一轮清坟执行：
`git rm core/meta_cognition.py` + 移除 tests/test_phase1_5_modules.py 的
TestMetaCognition 四用例）。其健康分思路若仍有价值，
agent_introspection 已有更完整的实现可承接。

## 三、顺带发现

- 全仓 grep 未发现其他 `_lite` / 同前缀孪生命名。
- `core/` 当前 60 个模块中，本审计后确认的同域冗余已清零；
  后续膨胀控制建议：新模块入 core/ 前先 grep 是否存在既有同义词实现
  （degradation/meta/cognition/dream 都是历史重灾区命名）。
