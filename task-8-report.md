# 任务 8 实施报告

## 范围

- 新增 `local_ai/downloads/__init__.py`，导出下载管理接口。
- 新增 `local_ai/downloads/manager.py`，实现下载任务生命周期、状态持久化与模型注册。
- 新增 `local_ai/downloads/transport.py`，实现可替换传输契约及 ModelScope HTTP Range 下载。
- 新增 `local_ai/downloads/verifier.py`，异步执行文件 SHA256 校验。
- 新增 `tests/test_local_ai_downloads.py`，覆盖 Task 8 简报要求的下载行为。
- 未修改 Task 8 之外的现有文件，未提交任何改动。

## 审查修复

读取并逐项验证 `task-8-review-local-ai.md` 后，确认其中 1 个 Critical 和 3 个 Warning 均成立。修复前的定向证据包括：partial/final/discard symlink 测试失败、Range 返回 200 时缺少单调事件断言、严格 registry 下并发 `start()` 可重复注册，以及完整 partial 和 Content-Range 契约没有覆盖。

本轮继续按 RED→GREEN 修复：

- symlink：增加 partial、final、父目录和 discard 边界测试；创建、统计、打开、校验、替换和删除边界拒绝 leaf symlink，父目录仍通过解析后 containment 检查阻断逃逸。
- 单调进度：Range 请求被 200 忽略并重写时，对外 `bytes_downloaded` 取已发布进度与当前有效字节的最大值，保证完整事件序列不倒退。
- 并发 `start()`：任务锁内重新读取当前任务，第二个调用等待第一个完成后幂等返回 `COMPLETED`，不再重复注册或回退到 `FAILED`。
- Range 契约：206 同时校验 `Content-Range` 起点和总大小；不匹配时显式关闭旧流并从 0 安全重启。
- 完整 partial：磁盘 partial 长度等于 manifest size 时先校验 SHA256，成功则直接原子完成，不发送会得到 416 的 Range 请求；失败则清除后重下。
- HTTP 资源：streamed 错误响应在 `raise_for_status()` 抛出前显式关闭；协商失败的成功响应也通过传输层关闭回调释放。

修复前新增测试按预期 RED；最终下载专项测试由 8 个扩展为 16 个并全部通过。

## TDD 过程

### RED

先创建 8 个行为测试，覆盖：

- 已有目标目录内 `.part` 文件时，从其真实长度发送 Range 请求。
- 服务端对 Range 请求返回 200 时清空旧部分文件并安全重下，不重复追加字节。
- 每次状态和进度变化发送 `local_ai_download_updated`，事件包含完整 `DownloadTask` payload，字节进度单调递增。
- 暂停保留部分文件，恢复后从部分文件继续并完成。
- 取消默认保留部分文件，`discard_partials=True` 时删除部分文件。
- 重启恢复持久化任务，将中断的 `DOWNLOADING` 转为可恢复的 `PAUSED`，并按磁盘部分文件重算进度。
- SHA256 不匹配时转移到目标目录的 `.quarantine`，任务进入 `QUARANTINED`，绝不注册模型。
- 校验成功后先原子移动成最终文件，再调用既有 `ModelRegistry.register()` 契约。

首次运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_downloads.py -q
```

结果在测试收集阶段按预期失败：`ModuleNotFoundError: No module named 'local_ai.downloads'`。失败原因是 Task 8 生产接口尚不存在，确认测试先于实现。

### GREEN

最小实现包括：

- `DownloadManager.create(model, destination) -> DownloadTask` 创建任务并立即原子持久化任务与完整 catalog model 快照。
- `start()`、`pause()`、`resume()`、`cancel()` 和 `recover()` 使用既有 `DownloadTask`、`TaskState` 与 `CancelToken` 契约。
- 每个 catalog 文件使用目标目录本地 `<filename>.part`，续传偏移始终来自磁盘真实大小。
- HTTP transport 在偏移大于零时发送 `Range: bytes=<offset>-`；只有 206 的 Content-Range 起点和总大小匹配 manifest 才追加，返回 200 或协商不匹配则安全重写。
- 每个数据块前后执行取消检查，按总任务字节数更新速度、ETA 和单调进度。
- 文件大小与 SHA256 均通过后使用 `os.replace()` 原子落为最终文件；哈希失败则使用 `os.replace()` 移入 `.quarantine`。
- 全部文件原子落盘后构造 `InstalledModel`，通过既有 registry 注册；隔离、失败、暂停或取消状态均不注册。
- 状态文件使用每次唯一临时文件名再 `os.replace()`，避免并发暂停与下载进度写入竞争同一个临时文件。
- catalog 相对路径拒绝绝对路径、`..`、父目录逃逸和 leaf symlink，避免下载文件逃逸目标目录。

首轮 GREEN 得到 `1 failed, 7 passed`，失败暴露并发持久化共用临时文件导致 `FileNotFoundError`。改为唯一临时文件名后，暂停测试进一步暴露夹具在第一个数据块前暂停时尚未创建部分文件；测试改为先准备真实部分文件，以准确验证“暂停保留已有 partial”的需求。随后目标测试 `8 passed`。

### REFACTOR

- 将传输、文件校验和生命周期编排拆分到三个职责单一模块。
- 将任务变更统一收敛到 `_update()`，确保持久化与完整事件 payload 同步发生。
- 将目标路径解析集中到 `_safe_file_path()`，所有 partial、final 和删除路径共享同一安全策略。
- 未增加数据库迁移、Web API 或 Task 8 简报未要求的并发下载调度。

## 验证

最终运行：

```bash
.venv/bin/python -m pytest tests/test_local_ai_downloads.py tests/test_local_ai_contracts.py tests/test_local_ai_model_registry.py -q
.venv/bin/python -m ruff check local_ai/downloads tests/test_local_ai_downloads.py
.venv/bin/python -m compileall -q local_ai/downloads tests/test_local_ai_downloads.py
git diff --check -- local_ai/downloads tests/test_local_ai_downloads.py
```

结果：

- Task 8、contracts 与 registry 相关回归：`131 passed in 11.51s`。
- Ruff：`All checks passed!`。
- Python 编译检查：退出码 0。
- 差异空白检查：退出码 0。
- 编辑器诊断接口受工作区路径映射限制返回访问拒绝，未将其记为成功；使用 Ruff、compileall 与 pytest 完成静态及运行验证。

## 需求覆盖

- Range：partial 长度决定 Range 起点；覆盖 200 回退、206 Content-Range 校验、错误区间安全重启和完整 partial 本地完成。
- Progress：完整任务事件、单调字节进度、速度与 ETA 在下载块检查点更新；Range 200 回退事件序列也保持单调。
- Pause/Resume：`CancelToken` 主动检查中止当前流，partial 保留并可继续。
- Cancel：支持保留或显式丢弃 partial。
- Restart：任务和 catalog 快照原子持久化，`recover()` 恢复中断状态与磁盘进度。
- Hash：逐文件 SHA256；不匹配隔离且不注册。
- Atomic move：校验后 `.part` 原子替换为最终文件，全部文件就绪后才注册。
- Event：统一发送 `local_ai_download_updated`，`task` 字段为完整 `DownloadTask.to_dict()`。
- Concurrency：同任务并发 `start()` 只下载和注册一次，所有调用幂等返回完成态。
- Path safety：partial、final、父目录和 discard symlink 均有回归测试，不写入或注册 destination 外部内容。

## 最终判定

- Spec：PASS。Task 8 brief 明示的生命周期、Range 续传、200 安全重写、单调字节记账、暂停/恢复、取消、重启恢复、哈希隔离、原子移动和事件契约均有通过的行为测试。
- 质量：PASS。审查中的全部 Critical/Warning 已逐项复现、修复并进入回归保护；未发现遗留的 Critical 或 Warning。

## 改动保护

- 开始前记录工作区已有 11 个已修改文件和 8 组未跟踪路径，包含 contracts、catalog、registry、storage 与数据库相关在途改动。
- 执行过程中未运行 reset、checkout、restore、clean、stash、commit、rebase、merge 或 push。
- 仅新增 `local_ai/downloads/`、`tests/test_local_ai_downloads.py` 和本报告。
- 最终 HEAD 仍为 `a54221ac61b045dab7ff4d884ffe009e67282afe`，分支仍为 `feat/principal-session-scope-isolation`。
- 按要求未提交。
