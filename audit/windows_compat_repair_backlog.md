# Windows 兼容性与 IO 鲁棒性修复 Backlog（三份审查报告合并去重）

> 合并来源：
> - 报告 A：Windows 跨平台兼容性审查（H1/H2/M1-M3/L1-L6）
> - 报告 B：文件系统/IO 鲁棒性审查（H1-H3/M1-M9/低危）
> - 报告 C：Windows 兼容性与鲁棒性问题 9 条（#1-#9）
>
> 基线：HEAD `bf1f80cf`，main 分支。本机 win32 + Python 3.13.12。
> 实测确认基线：`WindowsSelectorEventLoopPolicy + create_subprocess_exec → NotImplementedError`；
> `Popen(pass_fds=...) → AssertionError: pass_fds not supported on Windows.`

## 优先级矩阵

### 高危 — Windows 功能直接崩溃

| ID | 位置 | 问题 | 来源 | 修复方向 |
|----|------|------|------|----------|
| H1 | agent.py:51-52 | WindowsSelectorEventLoopPolicy 导致 7 个 create_subprocess 调用点 NotImplementedError | A-H1 / C-#1 | 恢复 ProactorEventLoop 默认策略，更新注释 |
| H2 | tools/code_tools_v2.py:328 | pass_fds Windows 不支持 → AssertionError 逃逸 | A-H2 / C-#4 | Windows 分支去掉 pass_fds，改 stdout 约定回传 _result |
| H3 | core/behavioral_direction.py:92,97,132,142 | 四处 write_text/read_text 无 encoding= | C-#2 | 四处补 encoding="utf-8" |

### 高危 — IO 数据损坏/崩溃

| ID | 位置 | 问题 | 来源 | 修复方向 |
|----|------|------|------|----------|
| H4 | core/permanent_memory.py:178-193 | 固定 .json.tmp + 线程池并发写，无锁无唯一后缀 | B-H1 | 改用 utils.atomic_write 或 tempfile.mkstemp + 进程内锁 |
| H5 | core/xp_system.py:320-330 | 固定 tmp + 事件循环同步全量写盘，无 try/except | B-H2 | 改用 utils.atomic_write + to_thread + try/except |
| H6 | emotion/emotion_state.py:234-244 | 非原子 fire-and-forget 并发写 | B-H3 | 改走 utils.atomic_write；同类：tts_engine:414、manifest:310、manager:391、numpy_index:292 |

### 中危 — Windows 兼容性

| ID | 位置 | 问题 | 来源 | 修复方向 |
|----|------|------|------|----------|
| M1a | agent.py:466-510 | SO_REUSEADDR 端口探测 Windows 允许双绑 → watchdog 重启双监听 | C-#3 | Windows 分支用 SO_EXCLUSIVEADDRUSE 或去掉 SO_REUSEADDR |
| M1b | cli.py:729,743 | 🌿 emoji print 到 cp936 → UnicodeEncodeError 崩溃 | C-#5 | sys.stdout.reconfigure(encoding="utf-8") |
| M1c | utils/logging_config.py:74-77 | loguru stderr sink 无 encoding，重定向丢日志 | C-#9 | stderr sink 显式 encoding |
| M2a | utils/vision_service.py:249,287-291 | /proc/meminfo Windows 恒 False → 视觉模型永远 api_fallback | C-#6 | 改用 psutil |
| M2b | utils/vision_service.py:48-58 | PowerShell 输出无 encoding/errors | C-#7(部分) | 补 [Console]::OutputEncoding=UTF8 + encoding="utf-8", errors="replace" |
| M2c | qq_bot_adapter.py:1840 | ffmpeg text=True 无 encoding，中文路径报错 UnicodeDecodeError | C-#7 | 补 encoding="utf-8", errors="replace" |
| M8a | 多文件 os.chmod 0600 | Windows 上静默无效，凭证文件不受保护 | A-M1 | Windows 分支加 ACL 限制或日志警告 |
| M8b | core/zombie_detector.py:205 | os.kill SIGTERM Windows 是 TerminateProcess 硬杀 | A-M3 | Windows 分支文档化或改 job object |
| M9a | tools/system_tools.py 全文件 | systemctl/ip/ss/ping -c 无平台门控 | A-L6 / C-隐含 | 注册时按 sys.platform 门控 |
| M9b | tools/mail_tools.py:146,151 | _ensure_node_in_path 只探测 node 无 node.exe | A-L5 | Windows 探测 node.exe + shutil.which |

### 中危 — IO 鲁棒性

| ID | 位置 | 问题 | 来源 | 修复方向 |
|----|------|------|------|----------|
| M3 | user_profile_learner:85、learning_feedback:305、learning_loop:68、vector_store:120 | 事件循环同步写盘 + 固定 tmp + 无重试 | B-M1 | 改走 utils.atomic_write |
| M4a | tools/file_tools_v2.py:491 | read_file 整文件读入内存后切片 → OOM | B-M2 | 改 itertools.islice 流式 |
| M4b | tools/file_tools_v2.py:515-533 | write_file 直接 "w" 覆盖非原子 | B-M2 | 改走 utils.atomic_write |
| M5 | market/installer.py:145,179,276,412 | Windows rmtree/move 无重试，升级无回滚 | B-M3 / A-L2 | 加 Windows 重试退避 + 回滚 |
| M6a | setup_wizard.py:289-292 | .env 直接 "w" 覆盖非原子 | B-M4 | 改 utils.atomic_write |
| M6b | web/routers/auth.py:199-202 | 撤销列表 read-modify-write 非原子 | B-M5 | 改 utils.atomic_write |
| M6c | channel_adapter_base:289 + ilink_client:990 + wechat_bot_adapter:299 | wechat_cursor.json 双写者同一固定 .tmp | B-M6 | 统一走同一锁/同一写函数 |
| M7a | core/doctor.py:171-183 | 对运行中 SQLite copy2 备份 + VACUUM | B-M7 | 改 sqlite3 backup API 或检测占用跳过 |
| M7b | memory/npu_embed.py:186-215 | magic 超时后不 kill/不关管道 → 泄漏 | B-M8 | 超时分支加 cleanup |
| M7c | utils/file_receiver.py:225-243 | too_large 分支 unlink 失败 → 二次 close 掩盖异常 | B-M9 | 修复异常处理顺序 |

### 低危

| ID | 位置 | 问题 | 来源 | 修复方向 |
|----|------|------|------|----------|
| L1 | slash_commands.py:472-506 | /hw 的 /sys /proc 读取 Windows 降级 | A-L1 | 加平台门控改善体验 |
| L2 | web/ws_hub.py:336 | symlink_to Windows 需管理员权限 | A-L3 | Windows fallback 到 copy |
| L3 | scripts/bench_local_latency.py:106 | 硬编码 /tmp | A-L4 | 改 tempfile.gettempdir |
| L4 | core/capability_detector.py:234 | 无 encoding 的裸 open | C-#8 | 补 encoding |
| L5 | utils/watchdog_runner.py:251-255 | 无 CREATE_NO_WINDOW → pythonw 黑窗 | A-L1(报告B) | 加 creationflags |
| L6 | _run.bat / _run.ps1 | 硬编码 f:\naxida\_api_commit.py | C-#M4 | 改 %~dp0 相对路径 |
| L7 | memory/npu_embed.py:180 | sudo + 无扩展名 ELF | A-L2 | graceful 降级（已降级，文档化） |
| L8 | utils/xiaoda_acp.py:153,321 | coze-bridge 无扩展名 ELF | A-L3 | graceful 降级（已降级） |
| L9 | cli_client.py:61-63 | 全平台 systemctl cat | A-L4 | 前置 sys.platform 门控 |
| L10 | scripts/bench_npu_retrieval.py:60 | 硬编码 /opt/vpm_run + sudo | A-L6 | Linux 专属脚本，文档化 |

## 已排查排除项（避免误报）

- web/ws_hub.py fcntl/pty/termios：有 `_IS_WINDOWS` 守卫，Windows 分支用 subprocess 管道
- utils/watchdog_runner.py：win32 用 taskkill /F /T，SIGBREAK 替代 SIGTERM
- core/capability_detector.py:144-147：/sys//dev 有 platform_os == "Linux" 守护
- local_ai/devices/stats.py、system_probe.py：PowerShell 调用已正确设 encoding
- db/database.py:39-52：win32 分支用 ctypes GetVolumePathNameW
- agent.py:395-407：/proc 读取被 OSError 捕获，安全
- ~/.ai-agent 走 os.path.expanduser/Path.home()，Windows 正确
- .cmd/.bat 直接 create_subprocess_exec：现代 Windows CreateProcess 有内建处理，非 bug
- 无 os.fork/pwd/grp/resource/getuid、无 multiprocessing、无直接 select 模块

## 修复原则

1. **复用 utils/atomic_write.py**：已有 mkstemp+fsync+replace 实现，IO 鲁棒性修复统一走它
2. **平台分支用 os.name/sys.platform**：已有模式（watchdog_runner、ws_hub）
3. **graceful 降级**：Linux 专属功能在 Windows 不崩溃，明确返回"不支持"
4. **每项修复后本机实测**：Python 3.13.12，改一项验一项
5. **不钉值/不 hard-code**：从机制上修复，不写死当前正确值
