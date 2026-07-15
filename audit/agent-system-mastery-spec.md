# Agent 系统掌控力修复 Spec v1.0

> 诊断时间：2026-07-13
> 核心问题：Agent 对自身系统的掌控程度不够，任务放错位置、触发逻辑混乱、无法充分操控整个系统

---

## 一、问题全景：两个割裂的任务系统

当前系统里存在 **两套完全独立的任务/定时机制**，互相不通：

| 维度 | 笔记待办系统 (notebook_entries) | 定时问候系统 (greeting_schedules) |
|------|------|------|
| **数据表** | `notebook_entries` (kind='task') | `greeting_schedules` |
| **入口** | `/note` 斜杠命令、`auto_note_after_message` LLM自动提取 | Web UI `/schedule` 页面、REST API `/schedule/greetings` |
| **触发器** | `NudgeEngine._check_reminders()` (仅QQ通道，10min窗口) | `GreetingScheduler._tick()` (每30s轮询，Web+QQ) |
| **重复逻辑** | ❌ 无。只有单个 `due_date` 时间戳，触发后标记completed，永不重复 | ✅ 有。`days` 字段支持按周几触发，`type='fixed'` 每天按时触发 |
| **UI管理** | `/note` 命令文本输出，无法编辑/删除 | ScheduleView.vue 完整CRUD + 启停 + DND + 试发 |
| **触发质量** | 差。10min窗口易漏，only QQ，完成后不复重 | 好。30s tick精度，支持DND，支持Web+QQ双通道 |

**核心矛盾：用户说"提醒我/帮我记住"时，LLM 自动提取的 TASK 进了 notebook_entries（弱系统），而不是 greeting_schedules（强系统）。**

---

## 二、6个具体问题逐一诊断

### P0-1：Auto-TASK 走错表 — 最致命

**现象**：用户说"每天9点提醒我喝水"，LLM 提取 `TASK: 提醒喝水 @ 9:00`，写入 `notebook_entries` 而非 `greeting_schedules`。

**代码路径**：
```
NotebookManager.auto_note_after_message()
  → LLM 返回 "TASK: 提醒喝水 @ 9:00"
  → self.schedule_task(title, priority=1, due_at=due_at)
  → notebook.insert_notebook("task", title, ...)
  → INSERT INTO notebook_entries (kind='task', due_date=单次时间戳)
```

**根因**：`AUTO_NOTE_PROMPT_TEMPLATE` 只知道 `TASK:` 指令，不知道系统里有 `greeting_schedules` 这张表，也不知道定时问候页面的存在。LLM 没有被"告知"它应该把定时任务放进哪个系统。

**影响**：
- 任务进了弱系统，无法每日重复
- Web UI 看不到这些任务
- 只能通过 `/note` 文本命令查看

---

### P0-2：notebook_entries 缺少重复/每日逻辑

**现象**："每天9点提醒我"只记了今天的9:00时间戳，明天不触发。

**代码路径**：
```python
# NotebookManager.schedule_task()
async def schedule_task(self, title, priority=0, due_at=0.0):
    return await self.notebook.insert_notebook("task", title, importance=float(priority), due_date=due_at)
```

**根因**：`notebook_entries` 表只有单个 `due_date REAL` 字段，没有 `repeat_type`、`repeat_days`、`repeat_interval` 等字段。设计上就不是给周期任务用的。

**对比 greeting_schedules**：
```sql
CREATE TABLE greeting_schedules (
    type TEXT CHECK(type IN ('fixed','random')),  -- fixed=每天按时, random=随机时段
    time TEXT DEFAULT '',                          -- HH:MM
    days TEXT DEFAULT '[1,2,3,4,5,6,7]',          -- 周几触发
    count_per_day INTEGER DEFAULT 1,              -- 每天触发次数
    ...);
```

---

### P0-3：NudgeEngine 提醒机制极弱

**现象**：任务提醒只在 QQ 通道触发，Web UI 完全收不到；10分钟窗口容易漏；完成后直接 completed 不重复。

**代码路径**：
```python
# NudgeEngine._check_reminders()
async def _check_reminders(self):
    tasks = await self._db.notebook.get_due_tasks(window_seconds=600)  # 10分钟窗口
    tasks = tasks[:1]  # 最多只取1个
    for task in tasks:
        sent = await self._send_proactive(msg, "reminder")  # 仅QQ
        if sent:
            await self._db.notebook.remind_task(task["id"])
            await self._db.notebook.complete_task(task["id"])  # 完成后永不重复
```

**问题清单**：
1. `window_seconds=600` — 如果NudgeEngine的60s tick正好跳过了窗口，任务就漏了
2. `tasks[:1]` — 同一时刻只提醒1个任务
3. 仅 QQ 通道，Web UI 用户完全看不到
4. `complete_task` — 一次性消费，"每天"任务变成"今天一次"
5. 受 `MIN_PROACTIVE_INTERVAL=3600` 限制，两次提醒间隔至少1小时

---

### P1-4：GreetingScheduler 只做"问候"不做"提醒"

**现象**：`greeting_schedules` 的 `type` 只有 `fixed` 和 `random`，都是用来发创意问候的。没有 `reminder` 类型。

**代码路径**：
```python
# GreetingScheduler.fire()
async def fire(self, schedule, reason="manual_test"):
    text = await self._generate(hint)  # 总是调用LLM生成创意问候
    # ... 广播
```

**根因**：设计时只考虑了"定时问候"场景，没有考虑"定时提醒/任务"场景。`prompt_hint` 字段也只是作为问候的"灵感线索"，不是提醒内容。

---

### P1-5：LLM 不知道自己的系统长什么样

**现象**：Agent 不知道 Web UI 有定时问候页面，不知道可以通过 API 创建 schedule，不知道 `greeting_schedules` 的存在。

**代码路径**：
- `AUTO_NOTE_PROMPT_TEMPLATE` 中只有 `TASK:` 和 `INSIGHT:` 两种输出格式
- `prompt_builder.py` 的 system prompt 没有提及定时系统
- LLM 没有任何"系统自省"能力来知道自己能操控什么

**分类问题**：`prompt_builder.py` 把 "提醒我/设置提醒" 归类为 `tool` 场景，但实际没有对应的 tool handler 来创建 schedule。它只是被 NotebookManager 的后台 auto_note 捕获，走了一条弱路径。

---

### P2-6：Web UI 缺少"定时任务"与"定时问候"的统一视图

**现象**：ScheduleView.vue 只管理问候计划，没有任务提醒功能。用户在 Web UI 里看不到从对话中自动提取的定时任务。

---

## 三、修复方案：统一任务到 greeting_schedules

### 核心思路

**把 greeting_schedules 从"定时问候系统"升级为"统一定时调度系统"**，同时承载：
- 创意问候（type='fixed'/'random'，LLM 生成内容）
- 任务提醒（type='reminder'，直接发送固定文本）

### 3.1 数据库：新增 reminder 类型

```sql
-- greeting_schedules 表的 type 约 CHECK 约束扩展
ALTER TABLE greeting_schedules ... -- type CHECK 改为 IN ('fixed','random','reminder')

-- reminder 类型字段语义：
-- type='reminder' 时：
--   prompt_hint = 提醒内容（如"喝水"、"开会"）
--   time = 触发时间 HH:MM
--   days = 触发周几 [1,2,3,4,5,6,7]（每天则全选）
--   channels = 投递通道 ['web'] 或 ['web','qq']
--   enabled = 是否启用
```

### 3.2 GreetingScheduler：reminder 直接投递固定文本

```python
# GreetingScheduler.fire() 修改
async def fire(self, schedule, reason="manual_test"):
    if schedule.get("type") == "reminder":
        # reminder 类型：直接发送 prompt_hint，不调用LLM
        text = f"{address_term}～提醒你一下，{schedule['prompt_hint']}，别忘了哦～"
    else:
        # fixed/random 类型：LLM 生成创意问候
        text = await self._generate(hint)
    # ... 广播逻辑不变
```

### 3.3 NotebookManager：TASK 转发到 greeting_schedules

```python
# auto_note_after_message() 中的 TASK 处理改为：
elif last_line.startswith("TASK:"):
    task_str = last_line[5:].strip()
    if task_str and '<' not in task_str:
        title, due_at, is_daily, days = self._parse_task_with_repeat(task_str)
        if title:
            # 不再写入 notebook_entries，改为创建 greeting_schedule
            await self._create_reminder_schedule(title, due_at, is_daily, days)
```

新增 `_parse_task_with_repeat()`：
- "每天9点提醒我喝水" → title="喝水", time="09:00", days=[1..7], is_daily=True
- "明天14:00开会" → title="开会", time="14:00", days=[明天周几], is_daily=False
- "每周一9点周会" → title="周会", time="09:00", days=[1], is_daily=False

新增 `_create_reminder_schedule()`：
```python
async def _create_reminder_schedule(self, title, time_hm, days, channels=None):
    """在 greeting_schedules 表创建 type='reminder' 的定时提醒"""
    await self._db.execute(
        "INSERT INTO greeting_schedules "
        "(type, time, days, prompt_hint, channels, enabled, next_fire_times, created_at) "
        "VALUES ('reminder', ?, ?, ?, ?, 1, '[]', ?)",
        (time_hm, json.dumps(days), title, json.dumps(channels or ["web"]), time.time())
    )
```

### 3.4 AUTO_NOTE_PROMPT 升级：LLM 知道系统有定时功能

```python
AUTO_NOTE_PROMPT_TEMPLATE = """你是{agent_name}。刚刚和{address_term}进行了一轮对话。

{address_term}说了：
"{user_message}"

人家回应了：
"{assistant_reply}"

人家已经记下的关于{address_term}的认知：
{existing_notes}

请在下面选择一个行动（只需要返回格式，不需要解释）：

如果这轮对话让你对{address_term}有了新的了解——发现了他的性格特征、生活习惯、偏好倾向、
情感模式或价值观，且已有笔记里没有记过，请返回：INSIGHT: 简短描述
例如：INSIGHT: 性格急躁不喜欢等待

如果{address_term}明确说「提醒我」「帮我记一下」「别忘了」或给了具体时间，
请务必返回：TASK: 任务标题 @ 时间 [@ 重复模式]
例如：TASK: 提醒吃饭 @ 19:00 @ 每天
例如：TASK: 开会 @ 明天14:00
例如：TASK: 周会 @ 周一09:00 @ 每周
例如：TASK: 喝水 @ 9:00 @ 每天

【重要】定时任务会自动注册到系统的定时提醒页面，{address_term}可以在Web UI的
「定时问候」页面查看、编辑或删除。每天的任务会每天按时触发，不会遗漏。

不该记的：
- 日常寒暄 → PASS
- 没有揭示特征的简单问答 → PASS
- 重复内容 → PASS
- 常识性聊天 → PASS

如果只是普通闲聊，或这件事已经在已有笔记里记过了，请返回：PASS"""
```

### 3.5 prompt_builder：system prompt 注入系统能力声明

在 system prompt 中增加一段"系统能力说明"：

```markdown
## 系统能力

你可以操控以下系统功能：
- **定时提醒**：当用户要求提醒或定时任务时，系统会自动创建定时提醒，
  用户可在Web UI「定时问候」页面管理。支持每天/每周/一次性触发。
- **笔记/洞察**：你对用户的新发现会自动记录为笔记。
- **斜杠命令**：/note 查看笔记和待办，/status 查看系统状态，/help 查看所有命令。
```

### 3.6 NudgeEngine：移除 _check_reminders（职责已转移）

`_check_reminders()` 的职责完全由 `GreetingScheduler` 承接（它更精确、更多通道、支持重复）。
删除 `NudgeEngine._check_reminders()`，避免两套系统重复提醒。

### 3.7 Web UI：ScheduleView 支持 reminder 类型

- 列表中显示 reminder 条目，与 fixed/random 并存
- reminder 条目直接显示 prompt_hint 作为"提醒内容"
- 新增"添加提醒"按钮，表单更简单（只需填：内容 + 时间 + 重复方式）
- 支持删除/启停（已有）

### 3.8 API：schedule router 扩展

```python
# _validate_schedule() 扩展
if stype not in ("fixed", "random", "reminder"):
    raise HTTPException(400, "type 必须是 fixed、random 或 reminder")

# reminder 类型验证
if stype == "reminder":
    if not body.get("prompt_hint"):
        raise HTTPException(400, "reminder 类型必须提供 prompt_hint")
    _check_hm(body.get("time", ""), "time")
    rec["time"] = body["time"]
```

### 3.9 notebook_entries 中已有 task 的迁移

提供一次性迁移脚本，将 `notebook_entries` 中 `kind='task'` 且 `status='active'` 的条目：
1. 解析 content 和 due_date
2. 转换为 `greeting_schedules` 的 reminder 条目
3. 标记原 notebook 条目为 `status='migrated'`

---

## 四、修改文件清单

| 文件 | 改动 | 优先级 |
|------|------|--------|
| `db/database.py` | greeting_schedules 的 type CHECK 扩展为 ('fixed','random','reminder') | P0 |
| `web/greeting_scheduler.py` | fire() 增加 reminder 分支，直接投递固定文本 | P0 |
| `memory/notebook_manager.py` | TASK 处理改为创建 greeting_schedule；新增 _parse_task_with_repeat/_create_reminder_schedule | P0 |
| `memory/notebook_manager.py` | AUTO_NOTE_PROMPT_TEMPLATE 升级，增加重复模式语法和系统能力说明 | P0 |
| `web/routers/schedule.py` | _validate_schedule 支持 reminder 类型 | P1 |
| `emotion/nudge_engine.py` | 移除 _check_reminders()，职责转移给 GreetingScheduler | P1 |
| `prompt_builder.py` | system prompt 注入"系统能力"段落 | P1 |
| `web/frontend/src/views/ScheduleView.vue` | 支持 reminder 条目显示和"添加提醒"功能 | P1 |
| `agent_core/core.py` 或 `core/bootstrap.py` | NotebookManager 需要持有 db 引用以创建 greeting_schedule | P1 |
| 新文件 `scripts/migrate_tasks_to_schedules.py` | 一次性迁移脚本 | P2 |

---

## 五、触发质量提升细节

### 当前问题：NudgeEngine 触发不稳定
- 60s tick + 600s 窗口 = 可能漏
- MIN_PROACTIVE_INTERVAL=3600 = 两次提醒至少隔1小时
- 完成后永不重复

### 修复后：GreetingScheduler 承接所有定时触发
- 30s tick + 精确时间匹配（±1min 窗口）= 几乎不漏
- 每天重置 fired_today = 每天都能触发
- DND 补发 = 被免打扰拦截后不丢失
- max_per_day 上限 = 防止骚扰
- Web + QQ 双通道 = 全覆盖

---

## 六、Agent 系统掌控力提升路线图

本次修复是第一步（统一任务系统）。后续还有：

### 第二步：Agent 工具化
- 让 Agent 能主动调用 REST API 操作系统功能
- 不只靠 LLM 后台 auto_note，而是 Agent 在对话中就能说"好的，我帮你设置了每天9点的提醒"
- 需要给 LLM 暴露 tool_call 接口（如 `create_reminder`, `list_reminders`, `delete_reminder`）

### 第三步：系统自省
- Agent 能回答"你现在能做什么"（列出所有系统能力）
- Agent 知道自己有哪些页面、哪些 API、哪些斜杠命令
- prompt_builder 注入动态的"系统能力清单"

### 第四步：跨系统联动
- 笔记 ↔ 定时提醒 ↔ 记忆 ↔ 画像 打通
- 例如：画像发现"经常熬夜"→ 自动建议设置"23:00提醒睡觉"
- 例如：笔记记录"明天考试"→ 自动在当天早上设提醒

---

## 七、验收标准

1. ✅ 用户说"每天9点提醒我喝水" → greeting_schedules 表出现 type='reminder' 条目
2. ✅ 该条目在 Web UI 的定时问候页面可见、可编辑、可删除
3. ✅ 每天到了9:00，GreetingScheduler 触发提醒消息推送到 Web UI
4. ✅ 提醒内容是"爸爸～提醒你一下，喝水，别忘了哦～"而非 LLM 生成的创意问候
5. ✅ 用户说"明天14:00开会" → 创建一次性提醒，触发后自动禁用
6. ✅ notebook_entries 中不再新增 kind='task' 的条目（由 greeting_schedules 接管）
7. ✅ NudgeEngine._check_reminders() 已移除，不再有重复提醒冲突
8. ✅ 已有 notebook task 条目已迁移到 greeting_schedules
