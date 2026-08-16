"""channel_adapter_base.py —— QQ / 微信 Bot 适配器共享语义层（A1 遗留项去重）。

两个 IM 通道适配器（qq_bot_adapter.py / wechat_bot_adapter.py）存在逐字节相同的
语义层逻辑，抽取到本模块统一维护：

1. ``ChannelAdapterBase``
   - ``_MSG_ID_TTL`` 类属性与 ``_init_dedup_state()``：去重缓存初始化（两边 __init__ 各自调用）
   - ``_is_duplicate_msg()``：msg_id 级精确去重（逐字节搬移自 qq_bot_adapter 原实现，
     wechat 原实现与其逐字节相同）。仅拦截同一 msg_id 的精确重复，不做内容级去重。

2. JSON 凭证文件通用工具（wechat 凭证生命周期复用）
   - ``save_json_credentials``：原子写入 + 0600 权限 + 可选陈旧游标清理
   - ``load_json_credentials``：损坏文件/缺失必需键容错返回 None
   - ``clear_json_credentials``：删除凭证文件，不存在时静默跳过
   - 全部带 debug/error 日志；文件格式与路径由调用方决定（行为不变）。

3. .env 文件行 upsert 与逗号分隔 env 解析（qq_bot_adapter master openid 复用）
   - ``upsert_env_file_line``：在 .env 文件中插入/更新 ``key=value`` 行（utf-8-sig）
   - ``parse_env_csv``：解析环境变量为去空白的逗号分隔列表

本模块只依赖标准库 + loguru，不 import config / agent_core / 任何通道 SDK，
保证两个 adapter 均可安全 import 且无循环依赖。
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger


class ChannelAdapterBase:
    """IM 通道适配器共享基类（消息去重语义层）。"""

    # 消息去重缓存 TTL：1 小时。仅做 msg_id 级去重（同一 msg_id 的精确重复才拦截），
    # 不做内容级去重（用户主动重发的内容不拦截）。
    _MSG_ID_TTL = 3600

    def _init_dedup_state(self) -> None:
        """初始化消息去重缓存（由子类 __init__ 调用）。

        消息去重缓存：msg_id → 时间戳，保留最近 1 小时。
        """
        self._processed_msg_ids: dict[str, float] = {}
        self._MSG_ID_TTL = 3600  # 1 小时

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        now = time.time()
        # 清理过期项
        expired = [k for k, ts in self._processed_msg_ids.items() if now - ts > self._MSG_ID_TTL]
        for k in expired:
            del self._processed_msg_ids[k]
        # 检查重复
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids[msg_id] = now
        return False


# ---------------------------------------------------------------------------
# JSON 凭证文件通用工具（wechat 模块级三函数复用）
# ---------------------------------------------------------------------------

def save_json_credentials(
    path: Path,
    data: dict[str, Any],
    *,
    cursor_path: Path | None = None,
    event: str = "channel_adapter",
) -> None:
    """原子写入 JSON 凭证文件（0600 权限），可选清理同目录陈旧游标。

    - 目录以 0700 创建
    - 原子写入 + 限制权限：先写临时文件 chmod 0600，再 replace 覆盖，
      避免明文 token 权限过宽、且失败时留下半截文件。
    - 用 os.open 以 0600 模式创建文件再写入，消除"先写后 chmod"的权限窗口
      （写入瞬间临时文件不短暂暴露为 umask 默认权限）。
    - 写入新凭证意味着新会话开始，清除陈旧游标（cursor_path），避免服务端按
      旧游标重放上一会话的历史积压消息（串话/重复回复根因之一）。

    Args:
        path: 凭证文件路径
        data: 要落盘的凭证字典
        cursor_path: 同会话游标文件路径（None 表示不清理）
        event: 日志事件前缀（如 "wechat_bot"）

    Raises:
        写盘失败时记录 error 日志后原样上抛。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp_path = path.with_suffix(".json.tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp_path, 0o600)
        tmp_path.replace(path)
        if cursor_path is not None:
            try:
                if cursor_path.exists():
                    cursor_path.unlink()
                    logger.info(f"{event}.stale_cursor_cleared path={{}}", cursor_path)
            except Exception as ce:
                logger.warning(f"{event}.cursor_clear_failed error={{}}", str(ce)[:120])
        logger.info(f"{event}.credentials_saved path={{}}", path)
    except Exception as e:
        logger.error(f"{event}.credentials_save_failed error={{}}", str(e)[:200])
        raise


def load_json_credentials(
    path: Path,
    *,
    required_key: str | None = "bot_token",
    event: str = "channel_adapter",
) -> dict | None:
    """加载 JSON 凭证文件；不存在/损坏/缺少必需键时返回 None。

    Args:
        path: 凭证文件路径
        required_key: 必须存在的非空键（None 表示不做键校验）
        event: 日志事件前缀（如 "wechat_bot"）

    Returns:
        凭证字典，或 None（文件不存在 / 内容非 dict / 损坏 / 缺少必需键）
    """
    if not path.exists():
        logger.debug(f"{event}.credentials_not_found path={{}}", path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.debug(f"{event}.credentials_invalid_format path={{}}", path)
            return None
        if required_key and not data.get(required_key, ""):
            logger.debug(f"{event}.credentials_missing_key path={{}} key={required_key}", path)
            return None
        return data
    except Exception as e:
        logger.error(f"{event}.credentials_load_failed error={{}}", str(e)[:200])
        return None


def clear_json_credentials(path: Path, *, event: str = "channel_adapter") -> None:
    """删除凭证文件（不存在时静默跳过，异常只记 warning 不上抛）。

    Args:
        path: 凭证文件路径
        event: 日志事件前缀（如 "wechat_bot"）
    """
    try:
        if path.exists():
            path.unlink()
            logger.info(f"{event}.credentials_cleared path={{}}", path)
        else:
            logger.debug(f"{event}.credentials_clear_missing path={{}}", path)
    except Exception as e:
        logger.warning(f"{event}.credentials_clear_failed error={{}}", str(e)[:200])


# ---------------------------------------------------------------------------
# .env 文件行 upsert 与逗号分隔 env 解析（qq master openid 复用）
# ---------------------------------------------------------------------------

def upsert_env_file_line(env_path: Path, key: str, value: str) -> None:
    """在 .env 文件中插入或更新 ``key=value`` 行（保持 utf-8-sig 编码）。

    与 qq_bot_adapter._save_master_openid 原实现逐字节等价：
    - 文件不存在：直接写入 ``key=value\\n``
    - 文件存在：按行查找 ``key=`` 前缀（strip 后匹配）替换该行；
      未找到则在文件末尾追加 ``\\nkey=value\\n``

    Args:
        env_path: .env 文件路径
        key: 变量名
        value: 变量值
    """
    if not env_path.exists():
        env_path.write_text(f"{key}={value}\n", encoding="utf-8-sig")
    else:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
        found = False
        for i, line in enumerate(lines):
            if line.strip().startswith(f"{key}="):
                lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            lines.append(f"\n{key}={value}\n")
        env_path.write_text("".join(lines), encoding="utf-8-sig")


def parse_env_csv(var: str) -> list[str]:
    """解析环境变量为去空白的逗号分隔列表。

    与 qq_bot_adapter._parse_master_ids 原实现逐字节等价：
    ``raw.strip().split(",")`` 后逐项 strip 并过滤空项。

    Args:
        var: 环境变量名

    Returns:
        去空白的非空值列表（变量未设置或为空时返回空列表）
    """
    raw = os.getenv(var, "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()]
