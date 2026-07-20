# TODO — 待处理事项

> 本文件记录代码审查与回归测试中发现的、不影响合并但需要后续跟进的问题。
> 维护原则：每项必须说明影响范围、复现方式、建议方案、优先级与预估工作量。

## 来源

- **批次**：report-issues-batch-fix（2026-07-20 完成 15/15 Task）
- **审查来源**：Task 15 调用 `requesting-code-review` skill 派发的后台代理返回报告
- **已直接修复**：3 项 Minor
  - `emotion/emotion_enum.py` 第 3 行文件级 docstring "9 种核心情绪" → "16 种核心情绪"
  - `.gitignore` 移除 `docs/juejin-article.md` 排除条目（让 Task 13 的 17 处修复进入版本控制）
  - `README.md` 数值同步：515 → 517 模块、134,943 → 135,511 行
- **待跟进**：4 项 Minor，记录如下

---

## Minor #1：README "测试代码 ~41,000 行" 缺少 auto-updated 标记且脚本未覆盖

- **影响范围**：[README.md](file:///home/orangepi/ai-agent/README.md) 第 636 行 `| 测试代码 | \~41,000 行 |`
- **复现方式**：
  ```bash
  grep -n "测试代码" README.md
  python scripts/count_project_stats.py --check-readme   # 不报错说明脚本未覆盖此项
  ```
- **根因**：Task 4 更新数值时手动写入，`count_project_stats.py` 未实现测试代码行数统计函数，因此 `--check-readme` 无法校验此项
- **影响**：未来测试代码增减后 README 不会自动检测到数值过时
- **建议方案**：
  1. 在 `scripts/count_project_stats.py` 中新增 `count_test_code_stats()` 函数（递归扫描 `tests/` 目录，按 `.py` 文件累计行数，区分含/不含注释）
  2. 在 `--check-readme` 中加入对应校验项
  3. README 数值旁加 `<!-- auto-updated by scripts/count_project_stats.py -->` 标记
  4. 补充单元测试 `tests/test_count_project_stats.py` 覆盖新函数
- **优先级**：Low（不影响功能）
- **预估工作量**：~30 分钟（含测试）

---

## Minor #2：README "Web UI 视图 19 个" 与 "15 个功能视图" 统计口径需明确

- **影响范围**：
  - [README.md](file:///home/orangepi/ai-agent/README.md) 第 266 行：`15 个功能视图：Chat / Agents / Models / Tools / MCP / Workflows / Plugins / Insight / Schedule / Mail / Media / Health / Dashboard / Settings / Disclaimer`
  - [README.md](file:///home/orangepi/ai-agent/README.md) 第 641 行：`| Web UI 视图 | 19 个 |`
- **复现方式**：
  ```bash
  grep -n "功能视图\|Web UI 视图" README.md
  ls web/frontend/src/views/*.vue | wc -l   # 实际 19 个
  ```
- **根因**：两种口径并存但未在 README 中明确区分
  - **15 个功能视图**：用户可见的主功能页面（Chat / Agents / Models / Tools / MCP / Workflows / Plugins / Insight / Schedule / Mail / Media / Health / Dashboard / Settings / Disclaimer）
  - **19 个 .vue 视图文件**：包含 4 个辅助视图（LoginView / SetupWizardView / SponsorView / UserProfileSetupView）
- **影响**：读者可能困惑两种数字为何不同
- **建议方案**：
  1. 在 `count_project_stats.py` 中新增 `count_webui_views()` 函数（扫描 `web/frontend/src/views/` 下 `.vue` 文件数）
  2. 在 README 中明确标注两个口径：`19 个 .vue 视图文件（其中 15 个为主功能视图）`
  3. 数值旁加 auto-updated 标记
  4. 补充单元测试
- **优先级**：Low
- **预估工作量**：~20 分钟（含测试）

---

## Minor #3：docs/API.md 与 docs/ARCHITECTURE.md 行尾格式变化淹没内容变化

- **影响范围**：[docs/API.md](file:///home/orangepi/ai-agent/docs/API.md) 与 [docs/ARCHITECTURE.md](file:///home/orangepi/ai-agent/docs/ARCHITECTURE.md)
- **复现方式**：
  ```bash
  git diff --stat docs/API.md docs/ARCHITECTURE.md
  # 显示：1035 insertions(+), 1028 deletions(-)

  git diff -w --stat docs/API.md docs/ARCHITECTURE.md
  # 显示：11 insertions(+), 4 deletions(-)
  ```
- **根因**：编辑器或 git autocrlf 设置导致行尾从 CRLF 转 LF（或反之），git diff 将每行视为变更
- **影响**：Code review 中难以分辨真正的内容变化（实际仅 11/4 行内容变更，但 diff 显示 1035/1028 行）
- **建议方案**：
  1. 添加 `.gitattributes` 规则 `*.md text eol=lf` 统一行尾
  2. 或在 PR 描述中明确说明"行尾已规范化为 LF"
  3. 后续 review 使用 `git diff -w` 忽略空白字符差异
- **优先级**：Low（不影响功能，仅影响审查体验）
- **预估工作量**：~5 分钟

---

## Minor #4：docs/IMPROVEMENT_PLAN.md 第 322 行 "9 种核心情绪" 描述

- **影响范围**：[docs/IMPROVEMENT_PLAN.md](file:///home/orangepi/ai-agent/docs/IMPROVEMENT_PLAN.md) 第 322 行
- **复现方式**：
  ```bash
  grep -n "9 种核心情绪\|9 类核心情绪" docs/IMPROVEMENT_PLAN.md
  # L311: > **实现更新（2026-07）**：本节原计划"9 种核心情绪 + TTS 风格层映射"两层结构。
  #       实际落地时...已扩展为 **16 种**...下文"9 种"相关描述保留作为历史决策记录。
  # L322: 设计原则：**核心枚举宁少勿多**...采用"9 种核心情绪 + TTS 风格层映射"两层结构...
  ```
- **审查员判断**：Minor #5 — IMPROVEMENT_PLAN.md 第 322 行以下仍保留"9 种核心情绪"描述
- **实际状态**：**审查员误判，非真实问题**
  - L311 已经明确加注"实现更新（2026-07）"说明：实际已扩展为 16 种，下文"9 种"相关描述保留作为历史决策记录
  - L322 的 "9 种" 是**有意的历史决策保留**，符合 Task 13 的修复风格（保留历史决策描述并加注实现更新说明）
  - Task 13 已正确处理此情况
- **建议方案**：无需修改。在 Task 15 反馈中已说明此为误判。
- **优先级**：N/A（非真实问题）
- **预估工作量**：0

---

## 后续维护建议

新增 Minor 问题时按上述格式追加，并标注：
- 来源（哪个 Task / 哪次审查）
- 影响范围
- 复现方式
- 建议方案
- 优先级
- 预估工作量

完成的问题在标题前加 `[DONE]` 标记并保留历史记录，不要删除——便于后续追溯决策依据。

---

## 审查员误判处理原则

当审查员反馈与实际代码状态不符时：
1. **不要盲从**：先验证反馈是否真实存在
2. **如实记录**：在 TODO.md 中说明"审查员判断"与"实际状态"的差异
3. **保留证据**：引用具体行号和 grep 输出，便于后续追溯
4. **遵循 receiving-code-review skill**：技术严谨地推回不正确的反馈，而非表演性同意
