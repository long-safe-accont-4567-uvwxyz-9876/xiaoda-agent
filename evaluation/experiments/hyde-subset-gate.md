# 实验卡：HyDE 子集化门控（hyde-subset-gate）

状态：机制就绪（HYDE_SUBSET_MODE=non_exact），收益验证待真实语义嵌入环境
关联：研究文档 §5.2「exact 类禁 HyDE」/ §11 Phase 5 / §16 P4

## 假设

全局开启 HyDE 时 Recall@5 下降 25%（78.1%→53.1%，见 config_constants.py 注释），
根因是假设文档噪声污染词法可命中的查询。若仅对「语义型查询」（无精确标识符/
数字串/时间词/多跳连接词/标识符名词/中英混排特征）启用 HyDE，则：
- exact 类查询召回不受损（门控跳过）
- semantic 类查询的向量桥接收益有机会显现（先验未知，待测）

## 机制

- `HYDE_SUBSET_MODE=off`（默认）：行为与历史完全一致，仅 HYDE_ENABLED 总开关生效。
- `HYDE_SUBSET_MODE=non_exact`：`QueryTransformer.should_use_hyde()` 规则判定，
  exact 形态查询跳过假设文档生成，直接走普通向量检索。

## 判定标准（在冻结检索集上执行）

1. exact 类 case（exact_identifier/mixed_zh_code/temporal/group_scope/scope_isolation）
   Recall@5 与关闭 HyDE 基线无差异（这些查询本就不触发 HyDE——由门控保证）。
2. semantic 类 case（semantic_rewrite/coreference 等 SKIPPED 类别需真实嵌入环境执行）
   Recall@5 相对基线 ≥ +5% 才值得保留 non_exact 模式；否则回退 off。

## 环境前提

HashingEmbedder 下假设文档与真实答案无词法共享时哈希向量必然不相似——
语义桥接收益在此环境不可测量。必须使用远程 API embedding 或本地 bge
运行实测，并把结论回填本卡与 config_constants.py 注释。
