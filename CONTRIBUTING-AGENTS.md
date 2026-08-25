# AGENTS.md — 维护者贡献规则(对人类与编码 agent 一体适用)

> 本文件面向所有在本仓库工作的编码代理(Claude Code / Codex / Cursor / Copilot 等)
> 与人类贡献者。规则由仓库内门禁**自动执行**,不依赖阅读自觉;此处解释规则
> 存在的原因和被拦时的正确处置。Claude 系另见 CLAUDE.md(项目知识)。

## 核心原则:债务必须显式登记,不许沉默累积

本仓库曾积累 1446 条 lint 违规、1136 处宽 except、多个 2000+ 行巨型文件,
全部源于"每次只加一点、没人拦"的慢性堆积。现行的棘轮体系把每一类债务
变成**具名的、有数字的、超限即红的台账**:新增债务不是被禁止,而是要求你
主动上调基线并在提交说明里写明理由——沉默欠债的通道已关闭。

## 门禁清单(push 时自动执行,`git config core.hooksPath scripts/git-hooks` 启用)

| 门禁 | 基线 | 被拦时的正确处置 |
|---|---|---|
| `check_ruff.sh` | 146 冻结 | 先 `.venv/bin/python -m ruff check --fix .`;确属必要新增才上调基线+提交说明给理由 |
| `check_broad_except.sh` | 1157 | 新增宽捕获须收窄异常类型;确需宽捕获则上调基线+说明 |
| `check_lazy_imports.py` | 1418 | import 上提模块顶;确属打破循环/副作用的延迟导入→上调基线+说明 |
| `check_giant_files.sh` | 后端 py 900 / 前端 TS·Vue 900 / 测试 py 1500 | 超阈值即拆分;确属合理(i18n 键值表等)登记 `giant_file_allowlist.txt`——进清单就必须在 test_giant_file_ratchet.py 钉基线,否则周审计一致性检查会红 |
| `check_todo_ratchet.sh` | 3 冻结 | 别留 TODO/FIXME/HACK——当场还债,或上调基线+登记是什么债为何不还 |
| i18n key 一致性 | — | `npm run check:i18n` 查明细,补齐两侧字典 |

另有 `tests/test_giant_file_ratchet.py`:赦免清单内文件的行数基线,
**只许随拆分下调**。下调流程:拆分合入 → 改基线为实测值 → 提交说明注明。

## 被门禁拦住时的红线

1. **禁止用 `--no-verify` 绕过**来"先把功能推上去"。紧急跳过仅限生产事故
   热修,且必须在下一个工作时段补跑全套门禁并在提交说明标注跳过原因。
2. **禁止机械上调基线让红灯变绿**。基线上调没有理由说明 = 审计会话会找你。
3. **禁止把大文件挪进 `tests/` 或加目录排除来躲门禁**——排除目录本身
   的改动会被 review 盯上。

## 已知债务台账(动到这些文件前先看)

- `scripts/giant_file_allowlist.txt`:24 个超阈值文件,每个都是一笔登记在案的债;
  其中 qq_bot_adapter/wechat/setup 优先级最高(拆分蓝图见
  docs/giant_files_split_plan_2026-08-22.md 与 docs/tech_debt_audit_2026-08-25.md)。
- TODO 基线 3 处:wechat CDN 上传 / conflict_supersession v0.7 接线 / (1 处为正则关键词非真债)。

## 门禁自身也在看守之下

`scripts/gate_integrity.txt` 锁定全部门禁脚本与基线文件的 sha256,
每周审计比对:篡改门禁(放宽阈值/清空基线)会被 hash 比对暴露。
正当修改门禁后运行 `bash scripts/update_gate_hashes.sh` 重生成台账,
并在提交说明注明理由。

## 周审计(绕过提交门禁也会被发现)

`debt-audit.timer`(每周一 09:00)独立重跑全部棘轮 + 全集测试,结果落盘
`data/debt_audit.log`。绕过 pre-push 的债务存活期 ≤ 一周。

## 快速自检(push 前本地可全部预跑)

```bash
bash scripts/check_ruff.sh && bash scripts/check_broad_except.sh \
  && bash scripts/check_giant_files.sh && bash scripts/check_todo_ratchet.sh \
  && .venv/bin/python scripts/check_lazy_imports.py
```
