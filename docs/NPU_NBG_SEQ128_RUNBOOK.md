# NBG seq128 重编操作手册（x86 host 编译 → 香橙派板端部署）

> 适用读者：在 x86 Linux 主机上执行编译的工程师（或 AI Agent）。
> 目标：把 bge-large-zh-v1.5 嵌入模型的 NPU 固化包从固定 seq=512 重编为 **seq=128**，使
> 查询向量单条延迟从 ~1885ms 降至预期 **~470ms**（seq64 可再减半，见 §9）。
> 前置阅读：[npu_nbg_rebuild_seq128_plan.md](npu_nbg_rebuild_seq128_plan.md)（背景与实证数据）、
> 板端 `npu/bge_npu_kit/npu_input/README_bge_large_w8a16.md`（量化方案来龙去脉）。

---

## 0. 环境与职责划分

| 角色 | 设备 | 要做的事 |
|---|---|---|
| 编译机 host | x86 Linux（或 Windows+WSL2），≥16GB 内存，磁盘 ≥30GB 余量 | ONNX 导出、校准集生成、ACUITY 量化、NBG 打包 |
| 板端 | Orange Pi 4 Pro（a733 / VIP9000NANODI_PLUS） | vpm_run 冒烟、runner 直测、切换 `.env`、回滚预案 |

板端已就绪（不要在板上重编）：`scripts/npu/bge_npu_runner`（serve 模式协议 BGEVEC01，
CLS 池化 + L2 归一化输出 float32×1024 维）、`/opt/vpm_run/vpm_run` 冒烟器。

## 1. host 侧三件前置物

### 1.1 docker 镜像 `ubuntu-npu:v2.0.10.1`

含 ACUITY toolkit 6.30.22（`/root/acuity-toolkit-whl-6.30.22/bin/`）与 Vivante IDE 5.11.0 cmdtools。
获取二选一：

```bash
# 方式 A：厂商发布包（推荐，离线）
unzip docker_images_v2.0.x.zip
docker load < docker_images/ubuntu-npu-v2.0.10.1.tar

# 方式 B：registry
docker pull ubuntu-npu:v2.0.10.1
```

验证：`docker run --rm ubuntu-npu:v2.0.10.1 pegasus.py --help` 有用法输出即 OK。

### 1.2 AI SDK 辅助脚本（pegasus_*.sh）

板端 `work/ai-sdk/ZIFENG278-ai-sdk/scripts/` 是空目录（缺失原因见 01-setup-host.md Step 3）。host 上：

```bash
cd <工作根目录>          # 下文记为 $REPO_ROOT，对应 a733_npu_driver/
git clone https://github.com/ZIFENG278/ai-sdk work/ai-sdk/ZIFENG278-ai-sdk --depth 1
rm -rf work/ai-sdk/ZIFENG278-ai-sdk/models    # 只需要 scripts/ (~200KB)，models 是构建工作区会自动重建
```

要求：目录里必须有 `pegasus_setup.sh`（convert 脚本第 180 行强校验），以及
`pegasus_import.sh / pegasus_quantize.sh / pegasus_quantize_hybird.sh /
pegasus_inference.sh / pegasus_export_ovx_nbg.sh`。若上游仓库结构变化导致脚本名对不上，
以 convert_onnx_to_nbg.sh 中实际引用的文件名为准补齐。

### 1.3 模型与导出环境

```bash
python3 -m venv .venv-bge && source .venv-bge/bin/activate
pip install torch transformers onnx onnxruntime numpy tokenizers
# BAAI/bge-large-zh-v1.5 权重约 1.3GB；HuggingFace 直连或镜像均可
```

## 2. 导出 seq128 ONNX（host）

现网 bge-large-zh 的 onnx 是 **固定 [batch=1, seq=512]** 图（板端 `/mnt/usb2/nahida-data/models/bge-large-zh-v1.5/onnx/model.onnx` 实测），不能直接喂 128 数据。需重新导出：

`export_bge_onnx.py`：

```python
"""bge-large-zh-v1.5 → 固定 seq=128 ONNX（CLS 输出 + L2 归一化在前向内完成可选，
按现网约定：ONNX 输出 last_hidden_state (B,S,H)，池化/L2 由板端 runner 做）"""
import torch
from transformers import AutoModel, AutoTokenizer

SEQ = 128
tok = AutoTokenizer.from_pretrained("BAAI/bge-large-zh-v1.5")
model = AutoModel.from_pretrained("BAAI/bge-large-zh-v1.5").eval()

class Wrapper(torch.nn.Module):
    def __init__(self, m): super().__init__(); self.m = m
    def forward(self, input_ids, attention_mask, token_type_ids=None):
        out = self.m(input_ids=input_ids, attention_mask=attention_mask,
                     token_type_ids=token_type_ids)
        return out.last_hidden_state        # (B, S, H=1024)

dummy = dict(
    input_ids=torch.ones(1, SEQ, dtype=torch.long),
    attention_mask=torch.ones(1, SEQ, dtype=torch.long),
    token_type_ids=torch.zeros(1, SEQ, dtype=torch.long),
)
torch.onnx.export(
    Wrapper(model), tuple(dummy.values()), f"bge_large_zh_seq{SEQ}.onnx",
    input_names=["input_ids", "attention_mask", "token_type_ids"],
    output_names=["last_hidden_state"],
    opset_version=14, do_constant_folding=True,
    dynamic_axes=None,      # 固定 shape —— ACUITY 对静态图最稳
)
print("exported", f"bge_large_zh_seq{SEQ}.onnx")
```

要点：
- **opset 14**（ACUITY 6.30 支持良好；≥17 可能引入 LayerNormalization 高阶算子问题——见 docs/int8-quantization-strategy.md）；
- `token_type_ids` 保留为第三个输入以对齐旧校准集格式；BGE 用 BertTokenizer 时该输入全 0 不影响数值；
- 导出后用 onnxruntime 抽查一张 CPU 推理：同文本 seq128 vs transformers 原始输出余弦应 ≥0.999。

## 3. 校准集生成（host）

沿用板端既有格式：每个样本三个 `(1, SEQ)` int32 npy，目录下 `dataset.txt` 逐行列样本三元组。
板端 `calib/` 里是 64 组 512 版（(1,512) int32，mask sum≈311），**尺寸不符必须重新生成**。
校准语料建议直接取生产真实分布——从主库拉最近情景记忆/子 chunk 文本最贴近线上：

```bash
# 板端生成后拷到 host，或者 host 直接用导出的文本清单
sqlite3 agent.db "SELECT summary FROM episodic_memories ORDER BY id DESC LIMIT 500" > calib_texts.txt
```

`make_calib.py`：

```python
import numpy as np, pathlib
from tokenizers import Tokenizer

SEQ = 128; OUT = pathlib.Path("calib128"); OUT.mkdir(exist_ok=True)
tok = Tokenizer.from_file("/path/to/bge-large-zh-v1.5/tokenizer.json")

lines = []
for i, text in enumerate(open("calib_texts.txt")):
    text = text.strip()
    if not text: continue
    enc = tok.encode(text)
    ids = enc.ids[:SEQ]; mask = enc.attention[:SEQ]
    pad = SEQ - len(ids)
    ii = np.array(ids + [0]*pad + ([101] if len(ids)==0 else []), dtype=np.int32)[:SEQ]
    mm = np.array(mask + [0]*pad, dtype=np.int32)[:SEQ]
    tt = np.zeros(SEQ, dtype=np.int32)             # token_type_ids 全 0
    stem = f"sample_{i:03d}"
    for name, arr in (("input_ids",ii),("attention_mask",mm),("token_type_ids",tt)):
        np.save(OUT/f"{stem}_{name}.npy", arr.reshape(1,SEQ))
    lines.append(f"{stem}_input_ids.npy {stem}_attention_mask.npy {stem}_token_type_ids.npy")
(OUT/"dataset.txt").write_text("\n".join(lines)+"\n")
print(len(lines), "samples")
```

注意 ASCII 路径约束：convert 脚本以 ascii 读 dataset.txt，npy 文件名不要带中文；样本条数 100~300 条即可（旧集 64 组；覆盖短查询+长句混合分布最佳）。

## 4. NBG 编译（host，docker 内 ACUITY）

工作区布局（与 convert 脚本默认值一致）：

```
$REPO_ROOT/
├── scripts/host/convert_onnx_to_nbg.sh     # 已在 a733_npu_driver 仓库
├── work/ai-sdk/ZIFENG278-ai-sdk/scripts/   # §1.2 补齐
└── work/model-packages/                    # 产物输出 root（自动建）
```

执行：

```bash
cd $REPO_ROOT
bash scripts/host/convert_onnx_to_nbg.sh \
  --name bge_large_zh_sigmoid \
  --onnx /abs/path/bge_large_zh_seq128.onnx \
  --dataset /abs/path/calib128/dataset.txt \
  --quant int16 \
  --inputs input_ids,attention_mask,token_type_ids \
  --input-size-list 1x128i,1x128i,1x128i \
  --outputs last_hidden_state
```

参数说明与决策树：

| 参数 | 取值 | 说明 |
|---|---|---|
| `--quant` | 先试 `int16` | 纯 INT16 激活+权重，历史实测余弦 **0.9999**（保底合格线）；速度比 w8a16 略慢但远优于 512 版 |
| 同上 | 进阶 `pcq --hybrid` | 复刻 W8A16（INT8 per-channel 权重 + INT16 激活），速度最快；产出后必须过 §5 全向量 ≥0.9995 精度验收，不达标退回 int16 |
| `--input-size-list` | 注意 `i` 后缀表示 int32 | 与 runner `_tokenize` 打包 `<512i` → 重编后只需改 SEQ 常量为 128 |
| `--target` | 默认 VIP9000NANODI_PLUS_PID0X1000003B 不要动 | 板端 SoC |

产物：`work/model-packages/bge_large_zh_sigmoid/int16/network_binary.nb`（bge-large 预计 150~360MB）。
若用了 hybrid 产 W8A16：输出的 `.quantize` 表**务必备份留存**（这次教训：上一代 w8a16 的 quantize 文件没留盘，复刻只能重量化）。

## 5. host 侧精度验收（board 之前先挡一道）

用 pegasus inference 或先在 host onnxruntime 对拍：

1. 挑 20 条真实查询（长短混合）；
2. 分别用「seq128 onnx(CPU)」与「原始 transformers(float32)」编码，逐对余弦；
3. 达标线：int16 ≥0.999；W8A16 全向量 ≥0.9995（对齐 README_bge_large_w8a16.md 第三节口径，W8A16 曾因 QK^T 激活动态范围爆掉到 0.66~0.81——换 hybrid 后修好，换了序列长度后此风险需重新验证一遍）。

## 6. 板端传输与 vpm_run 冒烟

把产物包拷到 U 盘后挂板（scp/samba 均可）：

```bash
# 板上（示例路径沿现有布局）
NEW=/mnt/usb2/nahida-data/npu/bge_npu_kit/npu_input/bge_large_seq128_int16
mkdir -p $NEW && cp <u盘>/network_binary.nb $NEW/

# sample.txt 两行 network/input 即可（对照旧包模板写法）
cat > $NEW/sample.txt <<EOF
[network]
./network_binary.nb
[input]
./input_0.dat
./input_1.dat
EOF
# 生成一个 (2×128×4) 字节全零输入:
python3 -c "import struct; open('$NEW/input_0.dat','wb').write(struct.pack('<256i',*([101]+[0]*127+[1]*128)))"

cd $NEW && sudo /opt/vpm_run/vpm_run -s sample.txt -b 0 --save_txt 1
# 通过标准：正常退出无 ERR 行；output_*.txt 尺寸 = 128×1024×4 = 524288 B（每输入）
```

## 7. runner serve 协议回归（板上，判定切流的关键）

⚠️ runner 与 python 侧两侧的 seq 必须一致，本仓库已把 python 侧做成 env 可切（2026-08-27）：

1. C 侧：runner 启动命令已支持 `--seq 128` 运行参数（bge_npu_runner.c:47 `SEQ_DEFAULT` 是默认值，serve 命令显式传参即可覆盖；magic 握手与输出字节数 `need = n * HID * 4` 不变）；**若用 --seq 传参则 C 侧零改动**；
2. Python 侧：`memory/npu_embed.py` 的 `SEQ` 已支持 `NPU_SEQ` env 覆盖（与 `NPU_HID`/`NPU_N_IN` 同款模式），INPUT_BYTES 打包协议随动。切换只需在 `.env` 加一行：
   ```ini
   NPU_SEQ=128
   NPU_HID=1024     # 不变,显式写出防误读
   ```
3. 单飞直测（板上任意目录即可跑）：

```bash
python3 - <<'PY'
import sys, time; sys.path.insert(0,".")
from memory.npu_embed import NpuEmbeddingProvider
p = NpuEmbeddingProvider(model_dir="/mnt/usb2/nahida-data/models/bge-large-zh-v1.5",
                         nbg_path="<新包>/network_binary.nb")
assert p.load()
for n in (1, 8):
    t0=time.perf_counter(); v=p.encode_batch(["帮我写个python脚本"]*n); dt=(time.perf_counter()-t0)*1000
    print(f"n={n}: {dt:.0f}ms ({dt/n:.0f}ms/条)")
import numpy as np
print("norm:", np.linalg.norm(v[0]))   # 应 ≈1.0
PY
```

通过标准：单条 ≤600ms（int16 版预估）；批次线性 ≤ 每条均摊值 +10%。

## 8. 切流与回滚（板上）

`/home/orangepi/ai-agent/.env`：

```ini
# 新包就位后改两行；EMBED_MODE/BACKEND 不动
NPU_NBG=/mnt/usb2/nahida-data/npu/bge_npu_kit/npu_input/bge_large_seq128_int16/network_binary.nb
NPU_SEQ=128
```

- 重启生效：`sudo systemctl restart nahida-web`
- 灰度观察：`curl /api/v1/system/metrics` 看 `timer.embed_provider.p95`（上一轮已埋点）；并盯 `memory.retrieve_timeout_single` 是否清零。
- **回滚**：`.env` 的 `NPU_NBG=` 改回 512 包绝对路径、`NPU_SEQ=128` 行删除（或改 512）后重启即可；旧包 `bge_large_zh_sigmoid_pcq.w8a16/network_binary.nb`（357MB）保留勿删。向量库无需任何重建——同一模型下 seq128 与 512 输出的数值一致（pad 区被 attention_mask 屏蔽；余弦差异理论上 <1e-3，实测如偏差异常大再回头查 mask 打包 bug 而不是换库）。

## 9. 后续可选优化（本次不做，记录给下一轮）

- **seq64**：线上抽样 P95 token≈24，64 版可再砍半耗时（方法完全相同，`--input-size-list 1x64i,...`）;
- **双 NBG 常驻**：文档型长文本(记忆编码)继续走 512 包、查询走 128 包——InstanceManager 支持 profile.options.nbg_path 多实例，收益有限可暂缓;
- **只导 CLS**：若在 ONNX 导出阶段直接做 CLS+L2 收敛成输出 (B,H)，可省 128×1024×4→4096B 的带宽;属锦上添花。

## 11. 实机现状（2026-08-27 SSH 勘察，本节优先读）

目标机 Lenovo 笔记本（`lenovo@192.168.16.165`，Windows + **WSL2 Debian 13 (trixie)**），已勘察到：

**工具链大部分现成，比 §1 预估省两步：**

| §1 前置物 | 实况 | 动作 |
|---|---|---|
| docker 镜像 ubuntu-npu | ✅ 已有 `v2.0.10.2`（7.8GB，WSL 内） | 直接用，runbook 里 `--image ubuntu-npu:v2.0.10.2` |
| ai-sdk scripts（pegasus_*.sh） | ✅ 当年工作区完整存活：`~/bge_npu_kit/a733_npu_driver/`（含 `work/ai-sdk/.../scripts/`） | 无需 clone |
| ONNX 导出脚本 | ⚠️ 半缺口：当时靠一次性内联 python 改图（未落盘） | 用 `~/npu_seq128_kit/export_bge_onnx.py` 重导（见下） |

**WSL 家目录关键资产**（`/root/bge_npu_kit/`，约 937G 盘可用）：

- `model/bge-large-zh-fixed3_softmax3d.onnx`（1.3GB）：上一代 seq512 的改图产物——"fixed3"= 固定 shape、GELU→Sigmoid（INT8 精度配套）、softmax 改写为 3D 友好算子。**本次 seq128 重导必须复刻这三点**，否则 ACUITY 编译和量化精度都可能翻车；
- `host_scripts/make_bge_w8a16.py`：丢失已久的 W8A16 量化表合并脚本找到了——int16 表做底、fullconnect 权重换 pcq int8 scale、bias 换 i32；走 W8A16 路线时直接复用；
- `conv_bge_large_wsl.sh`：当年 convert 命令实锤——`--quant uint8 --image ubuntu-npu:v2.0.10.2 --inputs "input_ids attention_mask" --input-size-list "1,512#1,512"`（注意：最终包是 **2 输入**、无 token_type_ids；size 列表用 `#` 分隔，且带 `--size-with-batch "1#1"` 定制参数，该参数在 a733_npu_driver 定制版 convert 脚本里）；
- `calib_large/dataset.txt`+npy：上一代校准集；
- `host_scripts/check_cos.py` + `float_ref.txt`：精度对拍工具与 float 基准。

**由此修正的 seq128 执行路径**（替代 §2 默认描述）：

1. `export_bge_onnx.py --seq 128` 导出后，还要做同款三项图改造：
   - GELU → Sigmoid（与旧包一致，保持量化行为可比；若 W8A16 合并表按节点名匹配 fullconnect 层号，Transformer 结构不变则名字 pattern 不变）；
   - softmax / 其他 ACUITY 不友好算子按 fixed3_softmax3d 同款重写——最稳妥的办法是直接用 onnx 层面把旧 1.3GB 图的 **Embedding/位置编码张量 resize 到 128**（位置嵌入是可学习向量插值不可行，但 BGE 的 position_ids 是 0..511 连续——**截断式处理不成立，必须重新导出**）；导出时用 transformers 自动建 128 版新图，再单独替换 GELU→Sigmoid 节点即可，softmax 维度问题若 ACUITY 报错再按 3D softmax 补；
2. `--inputs "input_ids attention_mask"`（2 输入，删掉 token_type_ids 或全 0 保留视 calib 生成器而定，两侧保持一致即可）；
3. 板端 Python 侧协议不变（SEQ=128 经 `NPU_SEQ` env 注入，N_IN 仍为 2）。

**Windows ↔ WSL 协作注意**（实测踩坑）：SSH 落在 Windows cmd，UTF-16/GBK 输出易乱码——长命令建议写成 `.bat` 放 C:\clean 再执行；文件已双向就位：Windows `C:\clean\{4个套件文件}` ≡ WSL `/root/npu_seq128_kit/`。

## 12. 一页纸开工清单（拿到机器直接敲）

```bash
# ── Windows 端:进入 WSL ──
wsl -d Debian

# ── 以下在 WSL Debian 内(root) ──
cd ~/bge_npu_kit
docker images | grep ubuntu-npu                      # v2.0.10.2 就绪确认

# 1) 导出 seq128 onnx(venv 准备)
python3 -m venv ~/.venv-bge && . ~/.venv-bge/bin/activate
pip install torch transformers onnx onnxruntime tokenizers numpy
python ~/npu_seq128_kit/export_bge_onnx.py \
    --model-dir /path/to/bge-large-zh-v1.5 --seq 128 \
    --out ~/bge_npu_kit/model/bge-large-zh-seq128_sigmoid.onnx
# 2) GELU→Sigmoid 节点替换(小脚本,§11 第1步) + onnxruntime 自检 ≥0.999

# 3) 校准集(seq128, 复用真实记忆语料)
python ~/npu_seq128_kit/make_calib.py \
    --tokenizer <bge tokenizer.json> --texts ~/npu_seq128_kit/calib_texts.txt \
    --seq 128 --out ~/bge_npu_kit/calib128 --max-samples 200

# 4) 编译(int16 保底先跑通,再冲 w8a16)
cd ~/bge_npu_kit/a733_npu_driver
DOCKER_RUN_ARGS="--cpus 8 --memory 16g" \
  scripts/host/convert_onnx_to_nbg.sh \
    --name bge_large_zh_sigmoid \
    --onnx ~/bge_npu_kit/model/bge-large-zh-seq128_sigmoid.onnx \
    --dataset ~/bge_npu_kit/calib128/dataset.txt \
    --quant uint8 --image ubuntu-npu:v2.0.10.2 \
    --inputs "input_ids attention_mask" \
    --input-size-list "1,128#1,128" \
    --outputs last_hidden_state --size-with-batch "1#1"

# 5) 产物: work/model-packages/bge_large_zh_sigmoid/uint8/network_binary.nb
#    → U盘带回香橙派 → 按 §6 冒烟、§7 直测、§8 切流(.env 两行)
```

## 13. 已知坑位速查

| 症状 | 根因 | 处置 |
|---|---|---|
| convert 报 `missing AI SDK pegasus_setup.sh` | §1.2 没补齐（WSL 实机已自带，见 §11） | clone ai-sdk 或用 `~/bge_npu_kit` 内现成副本 |
| quantize 精度崩到 0.6~0.8 | pcq 纯 INT8 的 QK^T 老毛病 | 用 `--quant int16` 或按 §11 走 make_bge_w8a16.py 合并 W8A16 |
| vpm_run 段错误 | target ID / nb 架构不对 | 核对 `--target` 未改动 |
| runner 一直等 magic | runner SEQ 与包不一致 | §7 两处 SEQ 同步（C 侧 --seq 参数 + 板上 NPU_SEQ=128） |
| 向量 norm≠1 | mask 打包错误导致 pad 参与 softmax | 检查 `_tokenize` 的 attention 后缀 0 |
| SSH 下中文/输出乱码 | Windows cmd UTF-16/GBK 混杂 | 命令写 .bat 放 C:\clean 再执行；或 base64 转义 |
| ACUITY 报 softmax 维度不支持 | 导出图保持 2D softmax | 按 fixed3_softmax3d 先例改 3D，或先试 opset14 原生图 |
