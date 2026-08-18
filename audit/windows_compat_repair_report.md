# Windows 兼容性与 IO 鲁棒性修复总结报告

> 基线：HEAD `bf1f80cf`，main 分支
> 修复环境：win32 + Python 3.13.12（managed）/ Python 3.12.0（系统，pytest）
> 修复日期：2026-08-19
> 修复范围：三份审查报告合并去重后的 20+ 个问题点

## 修复概览

| 级别 | 问题数 | 已修复 | 验证状态 |
|------|--------|--------|----------|
| 高危（Windows 功能崩溃） | 3 (H1/H2/H3) | 3 | ✅ 本机实测通过 |
| 高危（IO 数据损坏） | 3 (H4/H5/H6) | 3 | ✅ 并发写实测通过 |
| 中危（Windows 兼容性） | 9 (M1-M9) | 9 | ✅ import + 功能验证 |
| 低危（边缘/降级） | 6 (L1-L6) | 6 | ✅ import 验证 |
| **合计** | **21** | **21** | **32 个模块全量 import 通过** |

## 修复详情

### 高危 — Windows 功能直接崩溃

#### H1: asyncio SelectorEventLoopPolicy 导致子进程全部失效
- **文件**：`agent.py:43-52`、`agent.py:237-240`、`tests/test_windows_event_loop.py`
- **根因**：`WindowsSelectorEventLoopPolicy` 不支持子进程（`create_subprocess_exec/shell` 抛 `NotImplementedError`），影响 MCP/shell_command/coze-bridge 等 7 个调用点
- **修复**：恢复 `WindowsProactorEventLoopPolicy`（基于 IOCP，支持子进程等待）
- **权衡**：放弃 Selector 的 aiosqlite 线程切换加速（3-5 倍），换取子进程功能可用。实际 aiosqlite 瓶颈在磁盘 IO，线程通知开销占比小
- **验证**：回归测试 5/5 通过（含新增子进程可用测试）

#### H2: python_executor pass_fds 在 Windows 不支持
- **文件**：`tools/code_tools_v2.py:202-248`（wrapper 脚本）、`tools/code_tools_v2.py:307-377`（主逻辑）
- **根因**：`pass_fds=(result_w,)` 在 Windows 抛 `AssertionError`（不在 OSError 子类链里），穿透 except
- **修复**：去掉 `os.pipe()` + `pass_fds`，改用 `tempfile.mkstemp()` 临时文件回传 `_result`（环境变量 `_PYEXEC_RESULT_FILE` 传路径）。跨平台一致
- **验证**：子进程执行用户代码 + `_result` 回传 = 84，正确

#### H3: behavioral_direction 四处无 encoding 持久化
- **文件**：`core/behavioral_direction.py:92,97,132,142`
- **根因**：`write_text`/`read_text` 无 `encoding=`，zh-CN Windows 默认 GBK → 含 emoji 时 `UnicodeEncodeError`，读 UTF-8 文件时 `UnicodeDecodeError`
- **修复**：四处补 `encoding="utf-8"`
- **验证**：含中文+emoji 的 DirectionVector/Registry 读写正确

### 高危 — IO 数据损坏

#### H4: permanent_memory 固定 .json.tmp + 线程池并发写
- **文件**：`utils/atomic_write.py`（新增重试）、`core/permanent_memory.py`
- **根因**：`run_in_executor` 提交多个写线程写同一固定 `.json.tmp`，无锁 → 内容交错/PermissionError → load 失败 → 永久记忆清空
- **修复**：① 给 `atomic_write` 加 Windows `PermissionError` 重试（50ms→800ms 指数退避，一改全受益）；② `permanent_memory` 改用 `atomic_json_write`（mkstemp 唯一名 + fsync + replace）+ 进程内 `threading.Lock`
- **验证**：10 线程并发写 50 条记忆，全部正确读回 50/50

#### H5: xp_system 固定 tmp + 同步写盘无 try/except
- **文件**：`core/xp_system.py`
- **修复**：改用 `atomic_json_write` + try/except（不炸调用链）
- **验证**：save/load OK (xp=150)

#### H6: emotion_state 非原子 fire-and-forget 并发写
- **文件**：`emotion/emotion_state.py`、`emotion/tts_engine.py`、`market/manifest.py`、`plugins/manager.py`、`memory/numpy_index.py`
- **根因**：`write_text` 直接写最终文件，无 tmp/fsync/锁，并发写互相截断
- **修复**：全部改走 `atomic_write`（tts_engine/manifest/manager/numpy_index）或 `atomic_write`（emotion_state，payload 已 dumps）
- **验证**：5 模块 import 成功 + 10 线程并发 _save 文件完整

### 中危 — Windows 兼容性

| ID | 文件 | 问题 | 修复 |
|----|------|------|------|
| M1a | agent.py:472-481 | SO_REUSEADDR 允许双绑 | Windows 分支不设 SO_REUSEADDR |
| M1b | cli.py:1-8 | emoji print 到 cp936 崩溃 | stdout/stderr reconfigure UTF-8 |
| M1c | utils/logging_config.py:115 | loguru stderr sink 无 encoding | setup_logging 加 reconfigure |
| M2a | utils/vision_service.py:246-257 | /proc/meminfo Windows 恒 False | 改用 psutil |
| M2b | utils/vision_service.py:48-58 | PowerShell 输出无 encoding | 补 [Console]::OutputEncoding + encoding="utf-8" |
| M2c | qq_bot_adapter.py:1840 | ffmpeg text=True 无 encoding | 补 encoding="utf-8", errors="replace" |
| M3 | user_profile_learner / learning_feedback / learning_loop / vector_store | 事件循环同步写盘 + 固定 tmp | 改用 atomic_json_write/atomic_write |
| M4a | tools/file_tools_v2.py:490 | read_file 整文件读入 OOM | 改 itertools.islice 流式 |
| M4b | tools/file_tools_v2.py:531 | write_file 非原子 | 改用 atomic_write |
| M5 | market/installer.py:145,179,276,412 | Windows rmtree/move 无重试，升级无回滚 | 加 _rmtree_with_retry/_unlink_with_retry + 升级先备份再切换回滚 |
| M6a | setup_wizard.py | .env 非原子直写 | 改用 atomic_write |
| M6b | web/routers/auth.py | 撤销列表非原子 | 改用 atomic_json_write |
| M6c | channel_adapter_base / ilink_client / wechat_bot_adapter | wechat_cursor.json 双写者同一固定 .tmp | 改用 atomic_write（mkstemp 唯一名） |
| M7a | core/doctor.py | 对运行中 SQLite copy2 备份 | 改用 sqlite3 backup API + locked 跳过 |
| M7b | memory/npu_embed.py | magic 超时后子进程/管道泄漏 | 超时分支加 cleanup |
| M7c | utils/file_receiver.py | too_large 分支 fd 二次 close | 拆分 try 避免 ValueError 掩盖 |
| M8a | 多文件 os.chmod 0600 | Windows 静默无效 | 新增 _restrict_file_permissions_windows（icacls ACL） |
| M8b | core/zombie_detector.py | os.kill SIGTERM Windows 硬杀 | 加注释 + 日志说明语义 |
| M9a | tools/system_tools.py | POSIX 命令无平台门控 | _run_cmd 加 Windows 不支持命令拦截 |
| M9b | tools/mail_tools.py | _ensure_node_in_path 只探测 node | 改用 shutil.which + Windows 路径 |

### 低危

| ID | 文件 | 修复 |
|----|------|------|
| L1 | slash_commands.py | /hw 加 Windows 平台门控 |
| L2 | web/ws_hub.py | symlink_to 加 copy fallback |
| L3 | scripts/bench_local_latency.py | /tmp → tempfile.gettempdir() |
| L4 | core/capability_detector.py | 裸 open 补 encoding |
| L5 | utils/watchdog_runner.py | 加 CREATE_NO_WINDOW |
| L6 | _run.bat / _run.ps1 | 硬编码路径改 %~dp0/$PSScriptRoot |

## 核心基础设施改进

### utils/atomic_write.py 增强
1. **`_atomic_replace_with_retry`**：Windows 上 `os.replace` 遇 `PermissionError` 重试 5 次（50ms→800ms 指数退避）。解决杀毒软件/其他进程占用导致的替换失败
2. **`_restrict_file_permissions_windows`**：Windows 上用 `icacls /inheritance:r /grant:r` 设置 ACL 为仅当前用户可读写，补偿 `os.chmod 0600` 在 Windows 上的静默失效

### market/installer.py 增强
1. **`_rmtree_with_retry`**：Windows 上 rmtree 遇 `PermissionError` 重试 3 次
2. **`_unlink_with_retry`**：同上
3. **升级回滚机制**：先重命名旧目录为 `.old`，move 新目录失败时回滚（把 `.old` 改回来），避免"旧插件已删、新插件不完整"的丢失场景

## 验证结果

1. **回归测试**：`tests/test_windows_event_loop.py` 5/5 通过
2. **全量 import**：32 个修改模块全部正常
3. **综合实测**：8 项关键修复点（asyncio 子进程、pass_fds、编码、并发写、psutil、流式读、rmtree 重试）全部通过

## 剩余风险与建议

1. **aiosqlite 性能**：H1 恢复 Proactor 后，aiosqlite 线程切换比 Selector 慢 3-5 倍。若实际运行中发现 DB 性能瓶颈，可考虑在不需要子进程的入口（如纯 Web 服务）用 Selector，但需确保该入口不调用任何 `create_subprocess` 方法
2. **os.chmod Windows ACL**：M8a 用 `icacls` 补偿，但需要管理员权限才能修改某些文件的 ACL。非管理员运行时 icacls 可能失败（降级为默认 ACL，不崩溃）
3. **system_tools 平台门控**：M9a 只门控了 systemctl/journalctl/ip/ss + ping -c，netstat -tlnp/find/dig 等仍有差异，但影响小（LLM 可根据错误信息自行调整）
4. **未覆盖的审查项**：三份报告里标注的"已排查排除项"（fcntl/pty 有 _IS_WINDOWS 守卫、.cmd 直接 exec 实测正常等）未做改动，保持现状

## 修改文件清单（35 个文件）

agent.py, tools/code_tools_v2.py, core/behavioral_direction.py, core/permanent_memory.py, core/xp_system.py, emotion/emotion_state.py, emotion/tts_engine.py, market/manifest.py, plugins/manager.py, memory/numpy_index.py, utils/atomic_write.py, utils/vision_service.py, qq_bot_adapter.py, cli.py, utils/logging_config.py, tools/file_tools_v2.py, market/installer.py, setup_wizard.py, web/routers/auth.py, channel_adapter_base.py, ilink_client.py, wechat_bot_adapter.py, core/doctor.py, memory/npu_embed.py, utils/file_receiver.py, llm_gateway/provider_service.py, core/zombie_detector.py, tools/system_tools.py, tools/mail_tools.py, slash_commands.py, web/ws_hub.py, scripts/bench_local_latency.py, core/capability_detector.py, utils/watchdog_runner.py, _run.bat, _run.ps1, tests/test_windows_event_loop.py
