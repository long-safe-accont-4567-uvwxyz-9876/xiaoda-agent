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

4. 流式回复分片工具（A2：从 qq_bot_adapter 逐字节下沉，QQ/微信共用）
   - 模块级常量 ``STREAM_C2C_MAX_SEGMENTS`` / ``STREAM_GROUP_MAX_SEGMENTS``
     （原 ``QQ_C2C_MAX_SEGMENTS`` / ``QQ_GROUP_MAX_SEGMENTS`` 的搬移，QQ 侧保留同值别名）
   - ``_split_text_by_bytes``：按字节上限（默认 7800）切分，闭合截断的 markdown 代码块
   - ``_split_text_for_streaming``：按字符数（默认 300）流式切片，不切断代码块/URL
   - ``_adjust_boundary_for_code_block`` / ``_adjust_boundary_for_url``：切片点边界调整
   - ``_cap_stream_segments``：按被动回复配额（C2C/群聊各 4 片）截断，C2C 尾部合并后
     按字节上限重切

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

# 流式回复被动配额上限（原 qq_bot_adapter 模块级常量搬移，QQ 侧保留同名别名）
STREAM_C2C_MAX_SEGMENTS = 4
STREAM_GROUP_MAX_SEGMENTS = 4


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

    # ------------------------------------------------------------------
    # 流式回复分片工具（A2：从 qq_bot_adapter 逐字节下沉，QQ/微信共用）
    # ------------------------------------------------------------------

    def _split_text_by_bytes(self, text: str, byte_limit: int = 7800) -> list[str]:
        """按字节上限分割文本，每段不超过 byte_limit 字节。

        P1-6 修复：C2C 流式第 4 段合并后可能远超 QQ API 8000 字节上限，
        合并后需调用本方法按字节再分割，逐片发送。

        - 短文本（≤ byte_limit 字节）返回单片
        - 长文本按字节上限切片，优先在换行处切分
        - 闭合截断处未结束的 markdown 代码块（```）

        Args:
            text: 原始文本
            byte_limit: 字节上限，默认 7800（留 200 字节余量给 8000 字节 QQ API 上限）

        Returns:
            分割后的文本段列表（每段 ≤ byte_limit 字节）
        """
        if not text:
            return []
        encoded = text.encode('utf-8')
        if len(encoded) <= byte_limit:
            return [text]

        from utils.text_utils import _find_char_boundary

        segments: list[str] = []
        remaining = text
        while remaining:
            encoded = remaining.encode('utf-8')
            if len(encoded) <= byte_limit:
                segments.append(remaining)
                break

            # 留 10% 余量避免边界字符是多字节字符
            safe_limit = int(byte_limit * 0.9)
            target_chars = _find_char_boundary(remaining, safe_limit)
            # 优先在换行处切分（向前查找，找到的换行符包含在当前段）
            search_end = min(len(remaining), target_chars + 100)
            best_pos = remaining.rfind('\n', max(0, target_chars - 200), search_end)
            if best_pos == -1 or best_pos < target_chars // 2:
                best_pos = target_chars

            chunk = remaining[:best_pos]
            while len((chunk.rstrip() + ('\n```' if chunk.count('```') % 2 else '')).encode('utf-8')) > byte_limit:
                best_pos -= 1
                chunk = remaining[:best_pos]
            tail = remaining[best_pos:]
            if chunk.count('```') % 2 != 0:
                chunk = chunk.rstrip() + '\n```'
                tail = '```\n' + tail
            remaining = tail
            segments.append(chunk)

        return segments

    def _split_text_for_streaming(self, text: str, chunk_size: int = 300) -> list[str]:
        """将文本切片为流式发送的段。

        - 短回复（< 400 字符）返回单片
        - 长回复按 chunk_size 切片，避免切断 markdown 代码块和 URL
        - chunk_size 默认 300，建议范围 200-400

        Args:
            text: 原始文本
            chunk_size: 每片字符数，默认 300

        Returns:
            切片后的文本段列表
        """
        if not text:
            return []
        if len(text) < 400:
            return [text]

        segments: list[str] = []
        pos = 0
        text_len = len(text)
        while pos < text_len:
            end = min(pos + chunk_size, text_len)
            if end >= text_len:
                segments.append(text[pos:])
                break
            # 调整切片点：避免切断代码块和 URL
            end = self._adjust_boundary_for_code_block(text, pos, end)
            end = self._adjust_boundary_for_url(text, pos, end)
            # 防止零长度段或回退
            if end <= pos:
                end = min(pos + chunk_size, text_len)
            segments.append(text[pos:end])
            pos = end
        return segments

    def _adjust_boundary_for_code_block(self, text: str, start: int, end: int) -> int:
        """若切片点位于 markdown 代码块内部，向后调整到代码块结束。

        通过统计 [start, end) 范围内的 ``` 数量判断是否在代码块内部。
        若为奇数，表示切片点在代码块内部，需要向后查找下一个 ``` 并调整到其后。

        Args:
            text: 完整文本
            start: 当前段起始位置
            end: 原始切片点

        Returns:
            调整后的切片点
        """
        segment = text[start:end]
        fence_count = segment.count('```')
        if fence_count % 2 == 0:
            return end  # 不在代码块内
        # 在代码块内，找到下一个 ```
        next_fence = text.find('```', end)
        if next_fence == -1:
            return len(text)  # 没有闭合，剩余全部作为一段
        new_end = next_fence + 3
        # 防止单段过大（超过 6000 字符则放弃调整）
        if new_end - start > 6000:
            return end
        return new_end

    def _adjust_boundary_for_url(self, text: str, start: int, end: int) -> int:
        """若切片点位于 URL 中间，向后调整到 URL 结束。

        在 end 之前的窗口内查找最近的 http:// 或 https://，
        若 URL 延伸到 end 之后，则将 end 调整到 URL 结束位置。

        Args:
            text: 完整文本
            start: 当前段起始位置
            end: 原始切片点

        Returns:
            调整后的切片点
        """
        if end >= len(text):
            return end
        # 在 end 之前的窗口内查找最近的 http:// 或 https://
        search_start = max(0, end - 200)
        last_http = text.rfind('http://', search_start, end)
        last_https = text.rfind('https://', search_start, end)
        url_start = max(last_http, last_https)
        if url_start == -1:
            return end
        # URL 结束位置：第一个空白或中英文标点
        url_end = end
        stop_chars = set(' \t\n\r，。；！？「」『』（）()【】[]<>「」')
        while url_end < len(text) and text[url_end] not in stop_chars:
            url_end += 1
        # 防止单段过大
        if url_end - start > 6000:
            return end
        return url_end if url_end > end else end

    def _cap_stream_segments(self, segments: list[str], is_group: bool,
                             resplit_event: str, capped_event: str) -> list[str]:
        """按被动回复配额截断流式分片，超出的尾部合并到最后一片。

        C2C 合并后按字节上限重切（避免单条超 QQ API 8000 字节上限）；
        群聊走 split_for_group_passive，每段 ≤4000 字节，最多 4 片不会触发本分支。
        """
        max_segs = STREAM_GROUP_MAX_SEGMENTS if is_group else STREAM_C2C_MAX_SEGMENTS
        if len(segments) <= max_segs:
            return segments
        original_segment_count = len(segments)
        merged_tail = "".join(segments[max_segs - 1:])
        if not is_group:
            resplit = self._split_text_by_bytes(merged_tail, 7800)
            segments = segments[:max_segs - 1] + resplit
            logger.info(resplit_event + " original={} final={} max_segs={}",
                        original_segment_count, len(segments), max_segs)
        else:
            segments = segments[:max_segs - 1] + [merged_tail]
            logger.info(capped_event + " original={} capped={}",
                        original_segment_count, max_segs)
        return segments


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
