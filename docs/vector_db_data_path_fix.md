# 向量数据库数据路径修复与验证记录

> 日期：2026-08-19
> 涉及文件：`config_paths.py`
> 关联修复：`_resolve_data_path` 探针逻辑 + `load_dotenv(override=...)`

## 背景

本机（OrangePi）有特殊的外部 U 盘环境：向量数据存放在
`/mnt/usb2/nahida-data/`，通过 `.env` 中的 `KIOXIA_DATA_DIR=/mnt/usb2/nahida-data`
配置。源码设计上，未配置外置盘时所有数据回退到系统盘用户目录 `~/.ai-agent/`。

## 问题 1：向量库静默回退到系统盘（已修复）

### 现象
服务运行时，向量数据库（`agent.db` / `agent_vec.db`）实际读写的是**系统盘空库**
（`/home/orangepi/ai-agent/db`），而非 U 盘上 1.1GB 的真实数据。用户自定义壁纸、
记忆检索等全部失效。日志中有 `config.data_path_readonly path=/mnt/usb2/nahida-data/media`。

### 根因
`config_paths.py` 的 `_resolve_data_path()` 用"写临时探针文件 + 删除"来判断外置盘是否可写：

```python
_probe = kioxia_path / ".write_probe"
_probe.write_text("probe")      # 写入成功
_probe.unlink(missing_ok=True)  # 删除
```

U 盘本身完全可写（btrfs，rw），写入也成功。但 **`unlink` 被 CodeBuddy 的 safe-delete /
genie-trash 拦截器拦截**——它试图把文件移到回收站 `/mnt/usb2/.Trash-1000`，而该回收站
目录权限拒绝（`PermissionDenied`）。这个异常被 `_resolve_data_path` 的
`except (OSError, PermissionError)` 捕获，**误判外置盘只读**，于是回退到系统盘 fallback。

后果：不只是壁纸，连向量数据库 `DATA_DIR` 也回退到了系统盘空库——用户向量数据完全没生效。

### 修复
重写探针逻辑：判定可写性只看"能写入 + 能读回"，删除探针的 `unlink` 被 safe-delete
拦截时**忽略异常**（用 `finally` 包裹，删除失败不影响可写性结论）：

```python
_probe = kioxia_path / ".write_probe"
try:
    _probe.write_text("probe", encoding="utf-8")
    if _probe.read_text(encoding="utf-8") != "probe":
        raise OSError("write not persisted")
except (OSError, PermissionError):
    raise OSError(f"Filesystem is read-only: {kioxia_path}") from None
finally:
    try:
        _probe.unlink(missing_ok=True)
    except (OSError, PermissionError):
        logger.debug("config.data_path_probe_cleanup_skipped path=%s", kioxia_path)
return kioxia_path
```

## 问题 2：`.env` 强制覆盖环境变量（已修复）

### 现象
`config_paths.py` 顶部 `load_dotenv(ENV_PATH, override=True)` 会用项目根 `.env` 里写死的
`KIOXIA_DATA_DIR=/mnt/usb2/nahida-data` **强制覆盖**系统/命令行环境变量。导致换机器部署时
无法用环境变量指定数据盘，本机 U 盘路径也被锁死。

### 修复
改为 `override=False`：`.env` 仅作为默认值填充，已存在的系统/命令行环境变量优先。

```python
load_dotenv(ENV_PATH, override=False)
```

## 验证结果（2026-08-19）

### 向量库读写验证（本机 U 盘环境）✅
1. 服务进程（PID 989410）持有 `/mnt/usb2/nahida-data/db/agent_vec.db`（可写句柄）
   及 `agent.db` 多个句柄，证明向量库被正确打开。
2. `agent_vec.db-wal` mtime 为当天 `21:06:47`，证明服务持续写入向量数据。
3. 用 sqlite 对 `agent_vec.db` 执行"建表→插入→检出→删除"完整循环，**成功**。
4. 设 `KIOXIA_DATA_DIR=/mnt/usb2/nahida-data` 时 `DATA_DIR` 正确解析为
   `/mnt/usb2/nahida-data/db`。

**结论：服务能成功读写 U 盘向量数据库，之前的修复生效。**

### `override=False` 验证 ✅
- 测试1（系统设 `KIOXIA_DATA_DIR=/tmp/test_alt_disk`）：`_KIOXIA_BASE` 正确变为
  `/tmp/test_alt_disk`，系统环境变量优先于 `.env`。
- 测试2（不设环境变量）：回退到 `.env` 的 `/mnt/usb2/nahida-data`，**本机行为不变**。

## 附：无外置 U 盘时数据存放位置

源码默认设计（删除 `.env` 中 `KIOXIA_DATA_DIR` 或 U 盘不存在时）：

| 数据 | 路径 |
|------|------|
| 向量库 DATA_DIR | `~/.ai-agent/data/db` |
| 文件 FILE_DIR | `~/.ai-agent/data/files` |
| 日志 LOG_DIR | `~/.ai-agent/data/logs` |
| 壁纸 MEDIA_DIR | `~/.ai-agent/media`（已显式固定系统盘，不参与 U 盘分支） |
| 配置 CONFIG_DIR | `~/.ai-agent/config` |

即：无 U 盘时所有数据落在系统盘用户目录 `~/.ai-agent/`，向量库在 `~/.ai-agent/data/db`。

## 当前本机实际状态（2026-08-19 清理后）

U 盘 `/mnt/usb2/nahida-data/db/` 已清理垃圾（空库、旧向量库、污染备份、7/8 月旧 bak、
探针残留），保留：
- `agent.db` + shm + wal（主对话库，活跃）
- `agent_vec.db` + shm + wal（向量库，活跃）
- `agent.db.bak_fix_model_path_20260816`（最新备份，保留）
- 各在用配置文件（user_profile_stats / xp_state / rate_limit_buckets.sqlite / ccr_cache 等）

服务状态：`nahida-web.service` active，端口 8080 HTTP 200，向量库读写正常。
