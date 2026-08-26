# workflow v1/v2 双轨收敛 Checklist（2026-08-25）

> 技术债对账（docs/tech_debt_audit_2026-08-25.md §四）产出。
> 前置事实（均为 2026-08-25 实测核对）：
> - M0-M5 已全部落地（见 workflow-v2-promotion-plan.md §7 状态标注）；
> - 前端已消费 v2：api/index.ts:251-267 `/workflow-runs/{get,cancel,reviews,decide}`；
> - 双路由并挂：web/server.py:1255（v1 `workflows_router` + v2 `workflows_v2_router`）；
> - 灰度开关：DB config `workflow_v2.enabled`（默认 **false**）+ `pilot_wf_ids`
>   白名单（schema v28 `wf_config` KV 表）；未开放时 `POST /runs` 503。
>
> 本文只覆盖立项书 §6 之后的**终局收敛**（试点期之后的下线路径），
> 不重复 M0-M5 内容。每项打勾前不动下一项。

## 阶段一：试点验证（当前所处阶段）

- [ ] `workflow_v2.enabled` 保持 false，`pilot_wf_ids` 加入 2-3 个真实工作流
- [ ] 试点流各跑 ≥10 次真实运行，对照 v1 语义（v1 "执行"=wf_*.md 注入 prompt
      由 LLM 自由发挥）记录输出质量差异，确认 v2 确定性执行**不劣于** v1 的
      LLM 自由发挥（这是转正的核心判据，不是"跑通"）
- [ ] `scripts/migrate_v1_workflows.py --dry-run` 对真实工作区 rc=0（M3 已验过
      一次，工作区有新增工作流后重跑）
- [ ] `GET /workflow-metrics` 观察试点期失败码分布：UNSUPPORTED_NODE /
      REVIEW_REJECTED 占比异常即回试点

## 阶段二：全局开启

- [ ] 试点判据通过后 `workflow_v2.enabled=true`，观察 1 周
- [ ] 前端 `v2-status` 查询无 503 残留；启动按钮全部可用

## 阶段三：v1 写路径冻结与清退（关键项，容易漏）

- [ ] **停止 v1 保存时的 `skills/wf_{name}.md` 生成**（web/routers/workflows.py
      保存逻辑）——v1 "执行"依赖这些 md 注入 system prompt；v2 接管后它们
      仍是每次对话的 prompt 污染源。全局开启满 1 周无回退诉求后：
      生成侧下线 + 存量 `workspace/skills/wf_*.md` 逐个确认后清除
- [ ] v1 CRUD 保留为定义作者（立项书 §6 作者模型）或迁到 v2 revision 模型——
      **二选一决策**：维持作者模型则 v1 路由永久保留（不算债）；迁 revision
      模型则 v1 编辑器重构另立专项
- [ ] 前端 api/index.ts 的 v1 CRUD 端点（:242-250）随上一条决策同步

## 阶段四：观察与档案化

- [ ] 收敛决策与日期回写本文件 + workflow-v2-promotion-plan.md §8 决策表
- [ ] 若维持双轨（v1 定义 + v2 执行）为终态：在 ARCHITECTURE.md 写明该分层
      是有意设计，防止后续会话误判为"未清理完的双轨"再立清理专项

## 回退预案（任一阶段触发即执行）

- 灰度期：`pilot_wf_ids` 移除问题流 / `enabled=false`
- 已迁移流：`migrate_v1_workflows.py rollback <wf_id> <prev_rev>`
- v1 语义兜底：`skills/wf_*.md` 清除前必须确认可随时重新生成（v1 保存路径
  在线），否则不清除
