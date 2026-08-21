# Rust 混合架构 PoC 调研报告与实现 — perf/rust-hybrid-poc

> 结论先行：**混合架构方向可行且已验证**，但收益边界比通用宣传窄得多。
> 本 PR 用本机（香橙派 aarch64，2417 真实节点）实测数据划出了「值得下沉」的
> 精确边界，并落地了首个下沉模块（扩散通道 CPU 热点，**7.3x 加速、等价性
> 零分差、默认关闭、回退安全**）。

---

## 一、多方调研

### 1.1 本机运行时数据（2026-08-21，生产库 agent.db 实测）

对贴出的「Python FastAPI + Rust 底层」提案，先用量化数据检验其每个论断：

| 提案论断 | 本机实测 | 判定 |
|---|---|---|
| 「嵌入向量计算是 CPU 瓶颈」 | embed 3.5s = NPU 外部进程排队 + API 重试，非 Python CPU | ❌ Rust 无收益 |
| 「jieba 分词慢」 | `lcut_for_search` 单次 **0.09ms** | ❌ 非瓶颈 |
| 「向量检索/余弦慢」 | numpy 批量 2400×1024 仅 **4.9ms**（BLAS 已是原生码） | ❌ 非瓶颈 |
| 「KG 召回慢」 | 3s = LLM 实体提取网络调用 | ❌ 非 CPU |
| 「记忆打分排序是解释器开销」 | `_direct_channel`+`_compute_idf` = **53ms/查询纯 Python 循环** | ✅ 唯一符合下沉特征 |

检索端到端 avg 8.2s 中，真正属于「Python 解释器开销」的只有 ~5%。
**通用宣传的「Rust 快 10-100x」不能直接套用——先测瓶颈构成再动手。**

### 1.2 生产案例佐证（外部证据）

Rust 下沉在「解释器开销主导」的热路径上确实成立：

- **Hugging Face tokenizers**：Rust 为原始实现，1GB 文本分词 <20s（服务器 CPU）
- **orjson**：序列化比标准库 json 快 **10-13x**（twitter.json 11.1x / github.json 13.6x）
- **ruff**：官方口径比 Flake8 快 **10-100x**，用户实测 150-1000x

共同点：这些案例的下沉对象都是**逐字符/逐token 的解释器循环**——与本 PoC
选中的 `_direct_channel`（2400 次 json.loads + set 运算 + 子串扫描）同构。

### 1.3 关键反证实验：FFI 边界开销（本 PR 最重要的发现）

同一份 Rust 实现，两种调用模式实测（2417 节点，含 FFI 开销）：

| 模式 | 每查询耗时 | 对比 Python 52.6ms |
|---|---|---|
| 无状态调用（每次传全量节点数据） | **83.3ms** | **0.6x，净亏** |
| 常驻索引（NodeIndex 数据驻留 Rust 侧） | **7.2ms** | **7.3x** |

原因：无状态模式每次跨 FFI 拷贝 2400×(id+keys_json+text+weight)，
序列化开销 ~83ms 超过全部计算收益。**推论：Python↔Rust 混合架构中，
「数据驻留 + 微参数调用」是正收益的前提条件；传大数组的无状态调用
模式在任何规模下都不划算。** 这直接验证了原提案避坑点第 4 条并给出量化边界。

### 1.4 工具链验证（aarch64 本机）

- rustc 1.98.0 stable（RsProxy 镜像），零第三方 crate（手写 JSON 数组解析，
  免 serde_json），release 构建 **12 秒**，产物 539KB
- PyO3 0.23 在 aarch64 Linux + Python 3.11 正常工作
- 注意：`.so` 必须去掉 `lib` 前缀；项目根同名 Cargo 目录会遮蔽模块，
  产物需装入 site-packages（CI 打包需同步处理）

## 二、实现内容

```
rust_core/                    # 新增：PyO3 crate（零第三方依赖）
├── Cargo.toml                # pyo3 0.23, release+lto
└── src/lib.rs                # NodeIndex(常驻索引) + direct_channel + cosine_topk(备选)
memory/rust_hybrid.py         # 新增：接入层（开关/回退/规模门控）
memory/spreading_activation.py # 修改：recall() Step3+4 可选走 Rust 路径
tests/test_rust_hybrid_poc.py # 新增：7 项等价性测试（真实数据+边界用例）
docs/rust-hybrid-poc.md       # 本报告
```

### 语义契约

Rust `direct_channel` 与 Python `_compute_idf + _direct_channel` 逐位一致：
IDF 公式 ln(N/(1+df))、weight_bias floor 0.35、双向子串 len>=4 计分 0.6 系数、
keys 字段损坏按空集处理。等价性测试覆盖：基础命中、双向子串、损坏 JSON、
缺字段、Unicode 大小写、引擎集成、开关门控，**最大分差 0.00e+00**。

### 安全设计

- **默认关闭**：`RUST_HYBRID_ENABLED=0`（默认）时行为与主线完全一致
- **三重门控**：环境变量 + 模块可导入 + 节点数 ≥500（小规模 Python 已够快）
- **回退安全**：模块缺失/.so 架构不符/运行时异常 → 静默回退纯 Python，
  检索功能永不因本模块失败中断
- **索引一致性**：Rust 索引随 alive_nodes 节点数变化自动重建（<10ms）

## 三、验收数据

| 项目 | 结果 |
|---|---|
| 等价性（4 条真实查询 × 2417 节点） | 最大分差 0.00e+00 |
| 加速比（常驻索引 vs Python） | **7.3x**（52.6ms → 7.2ms） |
| 无状态 FFI 反证 | 0.6x 净亏（已记录为架构约束） |
| 扩散通道回归（87 项） | 全部通过 |
| 全量回归 | **4877 passed**, 10 skipped |
| 构建时间（aarch64 release） | 12s |

## 四、后续路线（若合并后观察达标）

1. **构建链**：maturin wheel 化进 CI 与安装包（当前手动 cp .so 到 site-packages）
2. **下一批候选下沉位**（均需先过「解释器开销 >30%」门槛）：
   - `_semantic_rerank` 的 rapidfuzz 批量调用（3ms，收益低，暂缓）
   - 上下文压缩的长文本裁剪循环（待测量）
   - `cosine_topk` 已预置（无 numpy 场景的降级路径加速）
3. **不建议下沉**（数据已证伪）：embed/NPU 调度、KG LLM 调用、jieba、
   sqlite-vec 检索——它们是 IO/外部进程/BLAS，不是 Python CPU
4. **不建议全量重构**：本项目 211 个 API 端点 + 40+ 工具 + 三通道 bot 的
   业务面重写风险远大于收益；混合架构按热点渐进迁移即可

## 五、开启方式（合并后）

```bash
# .env 追加
RUST_HYBRID_ENABLED=1        # 默认 0
# 可选：RUST_HYBRID_MIN_NODES=500
```
