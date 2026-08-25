# 分支全量 Review 台账 —— 2026-08-25

perf/rust-hybrid-poc 领先 origin 27 提交的多轮审查收束记录。
方法：每轮 2-4 个子 agent 并行（测试实测 / AST 静态比对 / 线上活体验证 / fresh-context 对抗复查），
重点为后端功能代码，裁决均以实际运行为证。

## 一、逐提交裁决

| 提交 | 内容 | 裁决 |
|---|---|---|
| `4a7721d4` | 后端三小修（ndcg/probes scope/Envelope） | ✅ 三处均为真修复；实测缓存命中修复前必 500 |
| `728dfd1d` | WebUI 玻璃提亮 + dist | ✅ 线上产物与提交一致 |
| `b92c14b0` `588ff31d` `6c00eedb` | P0-1~P0-4 记忆类型学/结构化工具流/群聊buffer/门禁 | ✅ 符合 48a57795 规格预期：双灰度默认关闭有证据、取消回收无双重发送竞态、三端契约同源 |
| `ff65841e`(+`3ec7edff`) | RAG治理+PromptA/B+情绪统一（18k 行） | ✅ 可留主干：主路径字节冻结完好、A/B 生产隔离结构性成立、情绪五层契约锁 |
| `1af8fc24` | degradation.py 清坟 | ✅ 零引用复验成立 |
| `48423e73` | lint 批次（309 文件） | ✅ 可信任为纯清理：AST 全量对比零夹带、import 重排无一翻转加载序；唯 NUDGE_ENABLED 等 truthy 集放宽属有意变更 |
| `b87b204a` | env_flag 收敛 | ✅ 默认值逐一保持、零收窄、特例完好；注意与父提交是原子对（中间态 import 即炸） |
| 其余 docs/gates/test 棘轮类 | 文档与门禁 | 低风险，未逐行审 |

## 二、本轮落地修复（4 提交）

1. **`a900a97c`** 终审尾巴四连偿——discover 缓存命中补 local-ort 注入+刷新去重 / probe_vector 只读化（record_access 开关）/ ndcg ideal 截断回退修复 / 守卫测试（后被勘误，见 3）
2. **`15c34e7e`** 对抗复查三收口——`installed=[]` 兜底（原 UnboundLocalError 可炸整个 discover）/ refreshing 标志防搁浅（create_task 前置+invalidate 复位）/ 守卫测试合并加固。**含勘误**：a900a97c 的守卫测试在提交窗口被并行会话覆盖为其演进版，声称的位置参/分叉校验当时不在库
3. **`9beac192`** 工具 schema 情绪词表对齐——tts_tools/_builtin_manifest/TOOLS.md（仓库+运行时副本）三处旧 15 词表全部改 EMOTION_VOCAB_SLASH 单一事实源派生，taxonomy 契约测试新增 4 断言防回流
4. **应急**：web/dist 一度在磁盘丢失（build 中途失败被清 outDir）致前端 404，已重建恢复

## 三、遗留尾巴（按优先级）

| 级别 | 事项 | 归属 |
|---|---|---|
| 中 | `web/routers/retrieval.py:87` 九个 RAG 键自建真值表缺 `"off"`，与 config_constants 对同一键真值表分叉 | 并行会话辖区（该文件在其 WIP 区） |
| 低 | intent.decompose 未设 `system_slot=True`，stage 校验空转 | 并行会话已有双槽重构在途 |
| 低 | golden 分类评估 90% 阈值纯手动、无自动化执行点 | 建议挂调度或手动跑留基线 |
| 低 | 探针与真实流量共享 query-cache namespace（300s TTL 内 sqlite-vec 单点故障可假阳性）；FSRS 懒迁移写不受 record_access 门控；评测页 prompt 模式仍 touch 记忆（注释声明有意） | 备案，暂不动 |
| 信息 | NUDGE_ENABLED/WECHAT_ILINK_ENABLED truthy 集合放宽为有意行为变更 | 发布说明补记 |

## 四、运行时验证记录

- 06:05 重启：startup.health all_ok total=11；discover 冷 2411ms→命中 10ms；探针 ok=true
- 09:54 全量重启（含并行会话 WIP）：预检 `import web.server` 通过后重启；all_ok total=11；agents=5；discover 冷 1692ms→命中 10ms 结构一致；vector 探针冷 18347ms（NPU embed 冷载）→热身 20ms 只读路径生效；前端新 dist 200；journal 近期零 traceback

## 五、方法论备注

- 双工作流并行时，**未跟踪文件不设防**：a900a97c 的守卫测试在被 git add 前被对方覆盖，导致提交信息与内容失实。教训：提交前 `git diff --cached` 逐文件过目，未跟踪新文件的归属要先约定
- 机械 lint 大批次的风险面 = F841 删除副作用 / F811 删错层 / E712 numpy 语义 / import 重排破坏 config 导入链；本次经 AST 对比法全部排除，该验证套路可复用
