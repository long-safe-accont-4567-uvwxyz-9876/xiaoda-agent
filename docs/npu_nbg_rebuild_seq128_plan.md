# NPU 嵌入延迟治本专项 — NBG seq128 重编可行性盘点

日期：2026-08-27
背景：生产检索查询向量走 `InstanceManager → VIPEmbeddingRuntime → 裸 NpuEmbeddingProvider`（NBG 固定 seq=512），单条实测 **1885ms**（1 条与 8 条逐条累计均 ~1.9s/条，纯线性）。真实用户查询 token 长度实测 ≤39（`tokenizers` 抽样 6 例，max=39）——**98% 的算力花在 pad 上**。

## 已实证的事实（本机实测）

| 实验 | 结果 | 结论 |
|---|---|---|
| runner 喂 seq=128 输入给现有 NBG | 接受但耗时仍 1885ms | **推理时长与实际输入长度无关**，seq 在 NBG 编译期固化，无运行期捷径 |
| CPU onnx bge-large（fixed[1,512] 图）单条 | ~7.6s（全核 8 线程） | threshold 分流方案无效，CPU 更慢 |
| 向量血统判定（rowid 13431 "话题: 对话格式控制"） | 本地 bge-large float vs 库 = **0.9995**；bge-m3 vs 库 = 0.057 | 现行向量库 1024 维全部由本地 bge-large-zh-v1.5 产出；换 remote bge-m3 需全库重建 |
| remote bge-m3 单条延迟 | 263~282ms 稳定 | 若接受全库重建（有 `_auto_rebuild` 机制），换 bge-m3 remote 是免工具链的替代路线 |

## 重编 NBG(seq128) 的三件前置物——现状

工作流：`bge_npu_kit/a733_npu_driver/scripts/host/convert_onnx_to_nbg.sh`（已验证可读，支持 --hybrid 混合量化，ACUITY_PATH=/root/acuity-toolkit-whl-6.30.22/bin）

| # | 前置物 | 现状 | 缺口动作 |
|---|---|---|---|
| 1 | docker 镜像 `ubuntu-npu:v2.0.10.1`（内含 ACUITY 6.30.22 编译器） | **未加载**（docker images 空）。获取：厂商发布包 `docker_images_v2.0.x.zip` 内 tar 约 11GB，或 registry pull | 拿到离线 tar 后 `docker load`；系统盘剩 15G、U 盘剩 54G → 加载目标放 U 盘 |
| 2 | host 侧 ai-sdk 脚本（pegasus_*.sh） | 目录 `work/ai-sdk/ZIFENG278-ai-sdk/scripts/` 存在但**为空**（0 字节） | 按 docs/01-setup-host.md Step 3：`git clone https://github.com/ZIFENG278/ai-sdk work/ai-sdk/ZIFENG278-ai-sdk --depth 1`（需 ~200KB） |
| 3 | bge-large 动态/seq128 ONNX 导出脚本 | **缺失**。README 提及的 `make_bge_w8a16.py`（量化表合并）也不在本机 | 新写导出脚本：transformers 加载 BAAI/bge-large-zh-v1.5 → torch.onnx.export(dynamic_axes 或固定 seq=128) → 沿用 W8A16 混合量化（pcq 权重块 + int16 激活块），校准集可复用 `calib/sample_*.npy`（需重生成 128 版） |

辅助资产均在位：校准样例目录 `calib/`、板端 vpm_run 验证器 `/opt/vpm_run/`、量化策略研究文档（research/int8-root-cause 等）、精度验收方法（README_bge_large_w8a16.md 第三节：W8A16 全向量余弦 ≥0.9995 达标线）。

## 预期收益

seq512→seq128 计算量比约 4:1，按现 1885ms 线性折算 **~470ms/条**；seq64 约 **~240ms/条**。叠加 EmbedCache（重复查询归零）与批量 miss-only 缓存（已上线），查询侧 P95 可从 1.9s 进入亚秒级。

## 执行顺序建议（拿到镜像后半天可完成）

1. host 上 clone ai-sdk scripts（#2）
2. 写并跑 ONNX 导出脚本（#3），产出 `model_seq128.onnx`
3. 重新生成 128 版校准 npy；跑 convert_onnx_to_nbg.sh --quant pcq --hybrid
4. 板端 vpm_run 冒烟 + 全向量余弦对拍 float32（≥0.9995）
5. 用 runner 直测新 NBG 单条/批量延迟
6. `.env` 改 `NPU_NBG=` 指向新包重启；旧 512 包保留可随时回滚
7. 灰度观察 timer.embed_provider 直方图（P0-2 已埋点）

风险：ACUITY 对 fixed seq128 图的算子覆盖与 512 版一致（同模型只改序列维，历史经验低风险）；混合量化表需重新生成而非复用——w8a16 合并逻辑如缺可先用 `--quant int16`（余弦 0.9999 但速度略降）作为保底版本。
