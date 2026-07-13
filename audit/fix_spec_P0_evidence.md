# P0 Fix Spec — Evidence 文档

**生成日期**: 2026-07-12

---

## E-01: FSRS 遗忘公式 S'_f 的标准实现

**声明**: FSRS-4.5/5 的 post-lapse stability 公式为 `S'_f = w11 * D^(-w12) * ((S+1)^w13 - 1) * e^(w14*...)`，其中 w11 是初始稳定性缩放因子，不是当前 S。

**来源**:  
- [(FSRS Algorithm Wiki)](https://github-wiki-see.page/m/open-spaced-repetition/fsrs4anki/wiki/The-Algorithm): "The stability after forgetting: S'_f(D,S,R) = w11·D^{-w12}·((S+1)^{w13} - 1)·e^{w14·...}"  
- [(Expertium — FSRS Technical Explanation)](https://expertium.github.io/Algorithm.html): "min(…, S) is necessary to ensure that post-lapse stability can never be greater than stability before the lapse."  
- FSRS-4.5 默认参数: w11=2.18, w12=0.05, w13=0.34, w14=1.26

---

## E-02: ContextVar token reset 模式

**声明**: Python ContextVar 的正确用法是 `token = var.set(value)` + `var.reset(token)`，不应使用 `var.set(None)` 替代 reset。

**来源**:  
- [(Python docs — ContextVar)](https://docs.python.org/3/library/contextvars.html): "Returns a Token object that can be used to restore the variable to its previous value via the ContextVar.reset() method."  
- [(PEP 567)](https://peps.python.org/pep-0567/): "ContextVar.reset(token) is used to reset the variable in the current context to the value it had before the set() operation that created the token."  
- Python 3.14+ 支持 token 作为 context manager: `with var.set(value): ...`

---

## E-03: QQ C2C 被动回复消息限制

**声明**: QQ 官方 API 限制 C2C 被动消息每个消息最多回复 4 次（2026-06-22 更新，原为 5 次），60 分钟有效。

**来源**:  
- [(QQ Bot 官方文档 — 发送消息)](https://bot.qq.com/wiki/develop/api-v2/server-inter/message/send-receive/send.html): "被动消息（回复类）有效时间为 60 分钟，每个消息最多回复 5 次，超时或超频会发送（回复）失败" + "C2C发消息接口新增 is_wakeup 字段使用该能力，同时被动消息（回复类）由 60 分钟 5 次 调整为 60 分钟 4 次"  
- 错误码 22009 = msg limit exceed（消息发送超频）

---

## E-04: SQLite WAL 跨进程安全

**声明**: SQLite WAL 模式本身支持并发读写，跨进程安全应依赖 `PRAGMA busy_timeout` + WAL 自动重试，而非 Python asyncio.Lock。

**来源**:  
- [(SQLite WAL Mode)](https://www.sqlite.org/wal.html): "WAL mode permits concurrent readers and one writer"  
- `PRAGMA busy_timeout=5000` 让 SQLite 在锁冲突时自动重试最多 5 秒

---

## E-05: asyncio.create_task 无 loop 保护

**声明**: `asyncio.create_task()` 在无运行事件循环时抛出 RuntimeError，应先调用 `asyncio.get_running_loop()` 检测。

**来源**:  
- [(Python docs — asyncio)](https://docs.python.org/3/library/asyncio-task.html): "Raise RuntimeError if there is no running event loop."

---

## E-06: health 信号值域归一化最佳实践

**声明**: 评分系统发射信号时应归一化到 [0, 1] 区间，确保信号值域与阈值判定在同一空间。

**来源**:  
- 项目内部 `behavioral_health.py` 的 HealthLevel IntEnum 值域为 1-5  
- `intervention_loop.py` 的阈值按 0-1 设计  
- FSRS 的 R (Retrievability) 值域为 0-1，difficulty 为 1-10——FSRS 在计算中始终使用归一化的 R 值
