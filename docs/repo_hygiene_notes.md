# 仓库卫生备忘（Repo Hygiene Notes）

> 记录 2026-08-16 的 .gitignore 清理，以及 git 内置大模型文件的迁移建议。
> 本文档只做记录与建议，不包含任何已执行的历史改写（历史保持原样）。

## 1. 问题描述

本项目运行时会在工作区内产生以下产物，此前未被 .gitignore 覆盖，
导致 `git status` 永远脏乱，且有误提交风险：

- SQLite WAL 模式运行时文件：`db/agent.db-shm`、`db/agent.db-wal`、
  `db/agent_vec.db-shm`、`db/agent_vec.db-wal`（现有 `*.db` 规则匹配不到 `.db-shm/.db-wal`）。
- 顶层 `media/` 目录（captures / image / tts / upload / video / wallpapers），
  全部为运行时产生，git 中无任何已追踪文件。
- `db/agent_vec_brute/`：暴力向量索引的运行时 .npz 缓存
  （`memory/vector_store.py` 中 `{db_stem}_brute/` 约定，由 `VECTOR_BRUTE_ENABLED=1` 开启）。
- `db/ccr_cache/`：上下文压缩（CCR）的运行时 JSON 缓存
  （`memory/context_compressor.py`：`CACHE_DIR = DATA_DIR / "ccr_cache"`）。

另一个遗留问题是 git 中已追踪的
`models/bge-small-zh-v1.5/onnx/model.onnx`（94,851,877 字节 ≈ 90.5 MiB）。
本次**未**做任何历史改写；以下仅记录可选迁移方案。

## 2. ONNX 模型移出 git 的候选方案

现状：模型为 Xenova/bge-small-zh-v1.5 的 ONNX 导出（fp32，512 维），
由 `memory/local_embed.py` 加载；路径解析在
`memory/vector_store.py::_default_local_model_dir()`
（优先级：env `LOCAL_EMBED_MODEL_DIR` > 项目内 `models/bge-small-zh-v1.5/` > 空）。
`db/database.py`（约 1383 行）会把 `builtin:bge-small-zh-v1.5` 以
`ownership="bundled"` 种子进 `installed_models` 表，目录指向项目内路径。

项目内已具备的下载基础设施（可直接复用，无需新造轮子）：

- `local_ai/downloads/manager.py`：DownloadManager（任务状态机、断点续传、
  `.part` 清理、sha256 校验，见 `local_ai/downloads/verifier.py`）。
- `local_ai/catalog/hf_repo.py`：经 hf-mirror.com 使用官方 huggingface_hub
  （`hf_hub_download`）检视/下载 HuggingFace 文件。
- `local_ai/catalog/modelscope.py`：ModelScope API 检视（国内镜像备选）。
- `local_ai/models/registry.py` + `db/db_local_ai.py`：`installed_models`
  注册表，`builtin:*` 条目禁止删除/变更。

当前缺口：install-linux.sh / install-windows.ps1 / setup_wizard.py /
SETUP.md / README.md 均**没有** bge 模型的下载步骤；模型缺失时
`local_embed.load()` 直接抛 `FileNotFoundError`，无自动下载兜底。
（Windows 安装包靠打包时内置模型，属构建期产物。）

### 方案 A：构建/安装时下载（推荐）

把 `models/bge-small-zh-v1.5/onnx/model.onnx` 移出 git（`git rm --cached` +
加 .gitignore 白名单恢复其余 tokenizer/config 小文件），改为：

1. 复用 DownloadManager/catalog 在安装脚本（install-linux.sh /
   install-windows.ps1）或 setup_wizard 中下载 Xenova/bge-small-zh-v1.5 的
   `onnx/model.onnx`（约 90 MiB，hf-mirror 国内可达）到
   `models/bge-small-zh-v1.5/`；
2. 首次启动兜底：`_default_local_model_dir()` 解析不到目录时触发下载
   （带进度事件，可挂 local_ai 的 EventSink）；
3. 调整 `db/database.py` 的 bundled 种子逻辑：目录存在才 seed，或 seed 为
   "pending" 状态待下载完成。

- 优点：git 仓库瘦身 90.5 MiB（未来 clone 不再拉取）；模型可升级/换量化
  变体不改 git；复用现成下载管线。
- 缺点：离线/内网部署需预置模型或离线包；需要一次性的 `git rm --cached`
  提交（旧 blob 仍在历史中，体积不会回退，只是不再增长）；首次启动有
  网络依赖。

### 方案 B：Git LFS

为 `models/bge-small-zh-v1.5/onnx/model.onnx` 启用 Git LFS 追踪。

- 优点：文件继续留在 git 工作流中，检出语义不变；对现有代码零改动。
- 缺点：需要 LFS 服务端配额与客户端安装；已污染的历史 blob 依然保留
  （不做 filter-repo 改写的话仓库体积不回退）；Windows 打包等场景仍需
  处理 LFS 拉取。

### 方案 C：保留现状

什么都不做，90.5 MiB 单文件继续内置于 git。

- 优点：零改动、零风险；离线打包（Windows 安装包内置模型）最省事。
- 缺点：每次 clone 拉取 90.5 MiB；任何未来模型更新都会让历史继续膨胀；
  属于技术债，建议在新 clone 策略或仓库拆分时再处理。

## 3. 本次 .gitignore 改动清单（2026-08-16）

新增规则（未删除任何既有规则）：

| 规则 | 位置/说明 |
| --- | --- |
| `*.db-shm` | SQLite WAL 运行时共享内存文件（补 `*.db` 匹配不到的变体） |
| `*.db-wal` | SQLite WAL 运行时预写日志文件 |
| `/media/` | 顶层运行时媒体目录（锚定根目录，不影响 `web/media/` 既有规则） |
| `db/agent_vec_brute/` | 暴力向量索引 .npz 运行时缓存 |
| `db/ccr_cache/` | 上下文压缩运行时 JSON 缓存 |

验证结果（`git check-ignore -v`）：

```text
.gitignore:12:*.db-shm            db/agent.db-shm
.gitignore:13:*.db-wal            db/agent_vec.db-wal
.gitignore:22:/media/             media/tts
.gitignore:67:db/agent_vec_brute/ db/agent_vec_brute
.gitignore:68:db/ccr_cache/       db/ccr_cache
```

`git status --porcelain` 中上述目录/文件已全部不再出现在 `??` 未跟踪列表。
web 侧已追踪的墙纸（`web/frontend/public/assets/wallpapers/*`）不受影响。

既有规则核查：`credentials/`、`.env`、`*.log`、`web/media/`、`*.db` 等均已
存在且与新增规则无冲突。注：`.gitignore` 内 `*.log` 与 `.dbg/` 各出现两次
（历史遗留重复，语义一致、无冲突），按要求本次未清理。

## 4. 本次未处理的条目（供人工判断）

以下 `??` 未跟踪条目不属于运行时产物，本次未加入 .gitignore：

- `market/url_policy.py` — 新源码模块（疑似待提交业务代码）。
- `tests/test_market_url_policy.py`、`tests/test_mcp_server_command_allowlist.py`、
  `tests/test_non_master_executor_gate.py`、`tests/test_non_master_tools_no_execute.py`、
  `tests/test_rate_limit_login_hardening.py`、`tests/test_rate_limit_xff_hardening.py`、
  `tests/test_sensitive_file_permission_correction.py`、
  `tests/test_whitelist_shell_metachar.py` — 新测试源码。
- `security_audit_report.md` — 生成的审计报告文档，是否入库由维护者决定。
