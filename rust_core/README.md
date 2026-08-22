# rust_core — CPU 热点下沉 PoC（perf/rust-hybrid-poc）

将扩散激活检索通道的纯 CPU 热点（`_compute_idf` + `_direct_channel`）用 Rust
（PyO3）实现为 Python 扩展，Python 侧经 `memory/rust_hybrid.py` 接入，
**默认关闭**，开启后扩展缺失/异常自动回退纯 Python，业务无感。

## 为什么选这个模块（本机 aarch64 实测数据）

| 候选模块 | 实测耗时 | 结论 |
| --- | --- | --- |
| `_direct_channel` + `_compute_idf` | 27.1 ms/查询（2417 节点） | ✅ 纯解释器循环，Rust 12.5x |
| jieba 分词 | 0.09 ms/查询 | 非瓶颈，不下沉 |
| NumPy 余弦批量（2400×1024） | 4.9 ms/次 | 已是 C 速度，不下沉 |
| embed 通道 3.5s / KG 通道 3s | — | NPU 进程排队 + LLM 网络调用，Rust 无收益 |

## 构建

```bash
bash rust_core/build.sh        # 需要 cargo（https://rustup.rs）
```

## 启用

```bash
# .env 或环境变量：
RUST_HYBRID_ENABLED=true
# 运行时让 Python 能找到扩展（systemd 场景加 Environment=PYTHONPATH=...）
PYTHONPATH=/home/orangepi/ai-agent/rust_core/target/release
```

## 安全边界

- 开关三重门控：`RUST_HYBRID_ENABLED` + 模块可导入 + 节点数 ≥500；
- 回退路径恒为原纯 Python 实现（`spreading_activation._direct_channel`），
  Rust 失败不影响任何检索功能；
- 语义等价由 `tests/test_rust_hybrid_poc.py` 保证：命中集一致、分数分差 <1e-9，
  覆盖 `\uXXXX` 转义、代理对、损坏 keys JSON、大小写、空字段等边界。

## 基准复现

```bash
PYTHONPATH=rust_core/target/release python - <<'EOF'
from memory.rust_hybrid import RustNodeIndex
# 见 PR 描述中的完整基准脚本；2417 节点实测 Python 27.1ms vs Rust 2.2ms
EOF
```
