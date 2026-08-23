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

5. TTLCache（B1：wechat 三份手搓 TTL 清理的统一替代）
   - ``TTLCache.prune_pairs``：过期剔除 + 最旧淘汰的唯一算法内核（原
     ``ChannelAdapterBase._session_cache_prune`` 的下沉，行为逐字节一致）
   - ``TTLCache`` 实例形态：绑定外部 (值表, 时间戳表) 双 dict，提供 set/get/pop

6. core 调用骨架（B2：qq/wechat 两适配器复制管道的沉淀层）
   - ``CoreProcessRequest``：跨适配器 process 请求描述（dataclass，通道特有字段经子类扩展）
   - ``ChannelAdapterBase._process_with_core()``：ACK → 会话 → 状态回调 →
     EventBus 绑定 → wait_for(process, 120s) → 超时/异常兜底 的模板方法，
     两通道语义差异全部经钩子方法消化（禁止在骨架内判别适配器类型）
   - ``ChannelAdapterBase._send_segments_paced()``：流式分段发送共享内核——
     段间打字节奏 random.uniform(0.8, 1.2)s、某段失败合并剩余段单条重发一次

本模块只依赖标准库 + loguru，不 import config / agent_core / 任何通道 SDK，
保证两个 adapter 均可安全 import 且无循环依赖。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

try:
    from utils.atomic_write import _restrict_file_permissions_windows, atomic_write
except (ImportError, AttributeError):
    atomic_write = None  # type: ignore[assignment]
    def _restrict_file_permissions_windows(path):  # type: ignore[no-redef]
        return

except Exception:
    logger.exception(".channel_adapter_base.unexpected")
    atomic_write = None  # type: ignore[assignment]
    def _restrict_file_permissions_windows(path):  # type: ignore[no-redef]
        return

# 流式回复被动配额上限（原 qq_bot_adapter 模块级常量搬移，QQ 侧保留同名别名）
STREAM_C2C_MAX_SEGMENTS = 4
STREAM_GROUP_MAX_SEGMENTS = 4


# ---------------------------------------------------------------------------
# 流式回复分片工具（A2：从 qq_bot_adapter 逐字节下沉，QQ/微信共用）
# 模块级函数，供 qq_bot_adapter / wechat_bot_adapter 等直接 import。
# ---------------------------------------------------------------------------

def _split_text_by_bytes(text: str, byte_limit: int = 7800) -> list[str]:
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

        safe_limit = int(byte_limit * 0.9)
        target_chars = _find_char_boundary(remaining, safe_limit)
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


def _adjust_boundary_for_code_block(text: str, start: int, end: int) -> int:
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
        return end
    next_fence = text.find('```', end)
    if next_fence == -1:
        return len(text)
    new_end = next_fence + 3
    if new_end - start > 6000:
        return end
    return new_end


def _adjust_boundary_for_url(text: str, start: int, end: int) -> int:
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
    search_start = max(0, end - 200)
    last_http = text.rfind('http://', search_start, end)
    last_https = text.rfind('https://', search_start, end)
    url_start = max(last_http, last_https)
    if url_start == -1:
        return end
    url_end = end
    stop_chars = set(' \t\n\r，。；！？「」『』（）()【】[]<>「」')
    while url_end < len(text) and text[url_end] not in stop_chars:
        url_end += 1
    if url_end - start > 6000:
        return end
    return url_end if url_end > end else end


def _split_text_for_streaming(text: str, chunk_size: int = 300) -> list[str]:
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
        end = _adjust_boundary_for_code_block(text, pos, end)
        end = _adjust_boundary_for_url(text, pos, end)
        if end <= pos:
            end = min(pos + chunk_size, text_len)
        segments.append(text[pos:end])
        pos = end
    return segments


def _cap_stream_segments(segments: list[str], is_group: bool,
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
        resplit = _split_text_by_bytes(merged_tail, 7800)
        segments = segments[:max_segs - 1] + resplit
        logger.info(resplit_event + " original={} final={} max_segs={}",
                    original_segment_count, len(segments), max_segs)
    else:
        segments = segments[:max_segs - 1] + [merged_tail]
        logger.info(capped_event + " original={} capped={}",
                    original_segment_count, max_segs)
    return segments


class TTLCache:
    """轻量 TTL + 容量上限缓存（B1：wechat 三份手搓清理的统一替代）。

    背景：wechat_bot_adapter 曾有三份劣化复制的手搓清理循环
    （``_remember_ctx`` / ``_user_lock`` / ``_prune_last_status_cache``），
    与 QQ 会话缓存的 ``_session_cache_prune`` 各自漂移（P1-1/P1-7 系列
    同一 bug 需人肉双修的先例）。本类是唯一算法内核：

    - ``prune_pairs(values, stamps, ttl=…, max_size=…)``：静态形态，对
      (值表, 时间戳表) 双 dict 执行「过期剔除 → 按时间戳最旧淘汰溢出项」，
      与原 ``ChannelAdapterBase._session_cache_prune`` 行为逐字节一致；
    - 实例形态：绑定外部两个 dict（dict 仍由调用方持有，测试/调试可继续
      直接读写原属性名），提供 set/get/pop 便捷操作。

    不变量：``set()`` 写入后立即执行清理，``len(values) <= max_size`` 恒成立
    （``max_size=None`` 表示不限容量，仅做 TTL 清理）。
    """

    __slots__ = ("values", "stamps", "ttl", "max_size")

    def __init__(self, values: dict, stamps: dict, *,
                 ttl: float, max_size: int | None = None) -> None:
        self.values = values
        self.stamps = stamps
        self.ttl = ttl
        self.max_size = max_size

    @staticmethod
    def prune_pairs(values: dict, stamps: dict, *,
                    ttl: float, max_size: int | None = None) -> None:
        """过期剔除 + 最旧淘汰的唯一实现（原 _session_cache_prune 内核下沉）。

        - 过期剔除：``now - ts > ttl`` 的条目连同时间戳一并删除；
        - 容量淘汰（max_size 给定时）：按时间戳升序淘汰最旧条目至不超上限。
        """
        now = time.time()
        expired = [k for k, t in stamps.items() if now - t > ttl]
        for k in expired:
            values.pop(k, None)
            stamps.pop(k, None)
        overflow = len(values) - (max_size if max_size is not None else len(values))
        if overflow > 0:
            sorted_keys = sorted(stamps.items(), key=lambda kv: kv[1])
            for k, _ in sorted_keys[:overflow]:
                values.pop(k, None)
                stamps.pop(k, None)

    def prune(self) -> None:
        """对绑定的双 dict 执行一次清理。"""
        self.prune_pairs(self.values, self.stamps, ttl=self.ttl, max_size=self.max_size)

    def set(self, key: str, value: Any, *, prune: bool = True) -> None:
        """写入并刷新时间戳；默认立即清理以维持容量上限。"""
        self.values[key] = value
        self.stamps[key] = time.time()
        if prune:
            self.prune()

    def get(self, key: str, default: Any = None, *, prune: bool = True) -> Any:
        if prune:
            self.prune()
        return self.values.get(key, default)

    def pop(self, key: str, default: Any = None) -> Any:
        self.stamps.pop(key, None)
        return self.values.pop(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.values

    def __len__(self) -> int:
        return len(self.values)


@dataclass
class CoreProcessRequest:
    """``ChannelAdapterBase._process_with_core`` 的跨适配器请求描述。

    公共字段覆盖三个通道（qq_c2c / qq_group / wechat_c2c）调用
    ``core.process`` 的共同入参；通道特有字段由子类扩展（如微信的
    context_token），禁止在骨架里用适配器类型判别。
    """

    text: str                 # 用户输入（已含附件描述拼接）
    user_id: str              # 规范化用户标识（qq_xxx / wechat_xxx）
    source: str               # 渠道标识：qq_c2c / qq_group / wechat_c2c
    user_openid: str          # 发送者 openid / from_user_id / member_openid
    session_id: str | None = None   # 预解析会话；None 时由骨架钩子 _resolve_session 决定


class ChannelAdapterBase:
    """IM 通道适配器共享基类（消息去重语义层 + 流式分片委托）。

    连接状态公开口径：外部代码（web/server、web/routers）一律通过
    is_connected / is_session_expired / has_init_failed / is_polling
    只读属性探测适配器状态，禁止直接 getattr 私有字段。
    """

    _MSG_ID_TTL = 3600

    @property
    def is_connected(self) -> bool:
        """通道网络层是否已连接。子类按各自连接模型覆写。"""
        return False

    @property
    def is_session_expired(self) -> bool:
        """会话凭证是否已确认过期（需重新登录/扫码）。默认否。"""
        return False

    @property
    def has_init_failed(self) -> bool:
        """AgentCore 等关键依赖初始化是否失败（故障可见性）。默认否。"""
        return False

    @property
    def is_polling(self) -> bool:
        """后台轮询任务是否存在且未结束。默认否。"""
        return False

    # ------------------------------------------------------------------
    # per-user session 缓存三件套（QQ/微信共用实现，原两侧各复制一份，
    # 同一 bug 需人肉双修——P1-7/P0 系列修复已发生漂移先例）
    # ------------------------------------------------------------------

    @staticmethod
    def _session_cache_prune(cache: dict, cache_ts: dict, *, ttl: float, max_size: int) -> None:
        """清理 session 缓存：1) 删除超过 TTL 的过期条目；2) 超过 max_size 按 FIFO 淘汰最旧。

        防多用户长期运行内存泄漏（原 P1-1，两侧各一份副本）。
        B1：算法内核下沉至 :meth:`TTLCache.prune_pairs`（行为逐字节一致），本方法保留
        为既有调用点/测试的稳定入口。
        """
        TTLCache.prune_pairs(cache, cache_ts, ttl=ttl, max_size=max_size)

    @classmethod
    def _session_cache_set(cls, cache: dict, cache_ts: dict, user_key: str, sid: str, *,
                           ttl: float, max_size: int) -> None:
        """统一缓存写入 + 立即执行 size cap（CodeRabbit F8，两侧各一份副本）。

        写入后立即淘汰 overflow，保证不变量 len(cache) <= max_size 始终成立。
        """
        cache[user_key] = sid
        cache_ts[user_key] = time.time()
        cls._session_cache_prune(cache, cache_ts, ttl=ttl, max_size=max_size)

    async def _get_or_create_session_cached(
        self,
        user_key: str,
        *,
        core: Any,
        cache: dict,
        cache_ts: dict,
        ttl: float,
        max_size: int,
        tmp_prefix: str,
        log_prefix: str,
        event_stem: str,
    ) -> str:
        """获取或创建会话 session_id 的统一实现（原 QQ _get_or_create_c2c_session
        与微信 _get_or_create_user_session 的合并体，语义逐行对齐）。

        - 内存缓存优先（TTL + FIFO 上限），避免每条消息都查 DB；
          根因：单连接 SQLite + WAL 下并发写阻塞读，get_session 超时 5s 触发雪崩。
        - P1-7：检测到 {tmp_prefix} 兜底 ID 时视为缓存失效跳过缓存——该 ID 不存在于
          sessions 表，继续缓存会导致上下文永久丢失。
        - 总 deadline 20s 共享给所有尝试（略大于 busy_timeout 15s），避免重试重置
          超时导致总延迟翻倍；DB 超时/锁时返回临时 session_id 保证消息不丢失，
          后续 process 仍能执行，仅持久化能力受影响。
        """
        self._session_cache_prune(cache, cache_ts, ttl=ttl, max_size=max_size)
        cached_sid = cache.get(user_key)
        cached_ts = cache_ts.get(user_key, 0)
        if (cached_sid
                and not cached_sid.startswith(tmp_prefix)
                and (time.time() - cached_ts < ttl)):
            return cached_sid

        deadline = time.monotonic() + 20.0

        async def _lookup() -> str:
            session = await asyncio.wait_for(
                core.get_session(user_key),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            if session:
                sid = session["id"]
                self._session_cache_set(cache, cache_ts, user_key, sid,
                                        ttl=ttl, max_size=max_size)
                return sid
            sid = await asyncio.wait_for(
                core.create_session(user_key),
                timeout=max(deadline - time.monotonic(), 0.1),
            )
            self._session_cache_set(cache, cache_ts, user_key, sid,
                                    ttl=ttl, max_size=max_size)
            return sid

        try:
            return await _lookup()
        except (TimeoutError, sqlite3.OperationalError) as e:
            logger.warning(log_prefix + "." + event_stem + "_db_error key={} error={}, retrying",
                           user_key[:16], str(e)[:100])
            # DB 锁/超时后用剩余时间重试一次（锁通常是短暂的）
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.error(log_prefix + "." + event_stem + "_deadline_exhausted key={}",
                             user_key[:16])
                return f"{tmp_prefix}{user_key[:16]}"
            try:
                return await _lookup()
            except (TimeoutError, sqlite3.OperationalError) as e2:
                logger.error(log_prefix + "." + event_stem + "_db_error_retry key={} error={}",
                             user_key[:16], str(e2)[:100])
                # 关键修复：DB 超时/锁时返回临时 session_id，保证消息不丢失；
                # 后续 process 仍能执行，仅持久化能力受影响
                return f"{tmp_prefix}{user_key[:16]}"
            except (KeyError, OSError, RuntimeError) as e3:
                logger.error(log_prefix + "." + event_stem + "_failed error={}", str(e3)[:200])
                return f"{tmp_prefix}{user_key[:16]}"
        except (KeyError, OSError, RuntimeError) as e:
            logger.error(log_prefix + "." + event_stem + "_failed error={}", str(e)[:200])
            return f"{tmp_prefix}{user_key[:16]}"

    # ------------------------------------------------------------------
    # B2：core 调用共享骨架（原 qq C2C / qq 群聊 / wechat 三处复制管道的沉淀层）
    # 时序即对外行为契约：ACK → 会话 → 绑定 → wait_for(process, 120s) → 兜底。
    # 两通道语义不同处一律做成钩子方法，骨架内禁止判别适配器类型。
    # ------------------------------------------------------------------

    #: agent.process 兜底超时秒数（QQ/微信共同行为契约，禁止单侧调整）
    CORE_PROCESS_TIMEOUT = 120

    #: 需要「兜底文案」的异常集合；TimeoutError 由骨架先行捕获，不在此列。
    #: 微信覆写为 (Exception,)（原实现捕获所有异常），QQ 沿用默认窄集。
    CORE_ERROR_TYPES: tuple[type[BaseException], ...] = (RuntimeError, OSError, ValueError)

    def _get_core(self) -> Any:
        """返回承载 ``process()`` 的 AgentCore。子类覆写（QQ=self.agent，微信=self._core）。"""
        raise NotImplementedError

    async def _send_ack(self, req: CoreProcessRequest) -> None:
        """处理前 ACK 钩子。失败策略由子类决定：上抛则走骨架错误兜底（QQ C2C），
        容忍吞掉则继续处理（qq 群聊 / 微信）。默认无 ACK。"""

    async def _resolve_session(self, req: CoreProcessRequest) -> str | None:
        """取/建会话钩子。默认透传 req.session_id（QQ 在加锁处理器内预解析，
        保持原「锁内先 session 后 ACK」时序）；微信覆写为查内存缓存/DB。"""
        return req.session_id

    def _make_status_callback(self, req: CoreProcessRequest) -> Any:
        """构造传给 process 的状态回调。默认 None（QQ 侧返回 no-op 闭包）。"""
        return None

    def _build_process_kwargs(self, req: CoreProcessRequest,
                              session_id: str | None) -> dict[str, Any]:
        """构造 process 关键字参数。session_id 仅在真值时传递——
        None/空串时省略该键（core.process 默认 session_id=""）。
        QQ 群聊经 _resolve_session 透传合成边界 qq_group:{群 openid}，
        由 core 侧拼装为按用户隔离的会话键；微信自行查缓存/DB 后传入。"""
        kwargs: dict[str, Any] = {
            "user_id": req.user_id,
            "source": req.source,
            "user_openid": req.user_openid,
            "status_callback": self._make_status_callback(req),
        }
        if session_id:
            kwargs["session_id"] = session_id
        return kwargs

    def _bind_bus_user(self, req: CoreProcessRequest) -> Any:
        """process 执行期间向 EventBus 挂载通道用户；返回解绑令牌。
        无事件总线语义的通道（微信）保持默认空操作并返回 None。"""
        return None

    def _unbind_bus_user(self, token: Any) -> None:
        """解除 :meth:`_bind_bus_user` 的绑定（token 为 None 时空操作）。"""

    async def _post_process_result(self, req: CoreProcessRequest, result: Any) -> Any:
        """成功取得 result 后、仍在兜底保护区内的收尾钩子（QQ：HITL 审批 +
        sticker 回复——其异常需落入错误兜底文案，故必须在骨架 try 内执行）。"""
        return result

    async def _on_core_timeout(self, req: CoreProcessRequest) -> None:
        """超时兜底钩子：记录失败状态 + 发送超时文案（子类实现各自文案/日志）。"""

    async def _on_core_error(self, req: CoreProcessRequest, exc: BaseException) -> None:
        """异常兜底钩子：会话失效等副作用 + 发送错误文案（子类实现）。"""

    async def _process_with_core(self, req: CoreProcessRequest) -> Any | None:
        """跨适配器 core 调用骨架（模板方法）。

        步骤（顺序即对外行为契约）：
          1. ACK（_send_ack；是否容忍失败由子类实现决定）
          2. 取/建会话（_resolve_session）
          3. EventBus 绑定（_bind_bus_user）→ wait_for(core.process(**kwargs),
             CORE_PROCESS_TIMEOUT=120) → finally 解绑
          4. 成功：_post_process_result（仍在保护区内）后返回 ProcessResult
          5. 超时/异常：_on_core_timeout / _on_core_error 兜底后返回 None
             （调用方据此跳过回复投递）
        """
        core = self._get_core()
        if core is None:
            return None
        try:
            await self._send_ack(req)
        except TimeoutError:
            await self._on_core_timeout(req)
            return None
        except self.CORE_ERROR_TYPES as e:
            await self._on_core_error(req, e)
            return None

        session_id = await self._resolve_session(req)
        try:
            token = self._bind_bus_user(req)
            try:
                result = await asyncio.wait_for(
                    core.process(req.text, **self._build_process_kwargs(req, session_id)),
                    timeout=self.CORE_PROCESS_TIMEOUT,
                )
            finally:
                self._unbind_bus_user(token)
            return await self._post_process_result(req, result)
        except TimeoutError:
            await self._on_core_timeout(req)
            return None
        except self.CORE_ERROR_TYPES as e:
            await self._on_core_error(req, e)
            return None

    async def _send_segments_paced(
        self,
        segments: list[str],
        send_one: Callable[[str], Awaitable[bool]],
        *,
        on_failure: Callable[[int, Any], Awaitable[Any]],
        log_prefix: str,
    ) -> bool:
        """按固定节奏逐片发送，并把首次失败原样交给通道恢复钩子。

        共享层只负责段间停顿和顺序发送。仅返回 ``True`` 表示成功；其他
        返回值与发送时抛出的原异常对象均不改写地传给
        ``on_failure(index, failure)``。恢复范围、重切上限和停止策略由各通道实现。
        """
        num_segments = len(segments)
        for index, segment in enumerate(segments):
            if index > 0:
                await asyncio.sleep(random.uniform(0.8, 1.2))
            try:
                result = await send_one(segment)
            except Exception as exc:
                logger.warning(
                    log_prefix + ".stream_segment_exception index={} total={} error={}",
                    index,
                    num_segments,
                    str(exc)[:200],
                )
                await on_failure(index, exc)
                return False
            if result is not True:
                logger.warning(
                    log_prefix + ".stream_segment_failed index={} total={}",
                    index,
                    num_segments,
                )
                await on_failure(index, result)
                return False
            logger.debug(
                log_prefix + ".stream_segment index={} total={} sent=True size={}",
                index,
                num_segments,
                len(segment),
            )
        return True

    def _init_dedup_state(self) -> None:
        """初始化消息去重缓存（由子类 __init__ 调用）。

        消息去重缓存：msg_id → 时间戳，保留最近 1 小时。
        """
        self._processed_msg_ids: dict[str, float] = {}
        self._MSG_ID_TTL = 3600

    def _is_duplicate_msg(self, msg_id: str) -> bool:
        now = time.time()
        expired = [k for k, ts in self._processed_msg_ids.items() if now - ts > self._MSG_ID_TTL]
        for k in expired:
            del self._processed_msg_ids[k]
        if msg_id in self._processed_msg_ids:
            return True
        self._processed_msg_ids[msg_id] = now
        return False

    def _split_text_by_bytes(self, text: str, byte_limit: int = 7800) -> list[str]:
        return _split_text_by_bytes(text, byte_limit)

    def _split_text_for_streaming(self, text: str, chunk_size: int = 300) -> list[str]:
        return _split_text_for_streaming(text, chunk_size)

    def _cap_stream_segments(self, segments: list[str], is_group: bool,
                             resplit_event: str, capped_event: str) -> list[str]:
        return _cap_stream_segments(segments, is_group, resplit_event, capped_event)


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
        content = json.dumps(data, ensure_ascii=False, indent=2)
        if atomic_write is not None:
            atomic_write(path, content, mode=0o600, encoding="utf-8")
        else:
            # fallback: 固定 tmp 方式（atomic_write 不可用时）
            tmp_path = path.with_suffix(".json.tmp")
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp_path, 0o600)  # Unix: 限制为仅用户可读写
            _restrict_file_permissions_windows(tmp_path)  # Windows: 用 ACL 补偿
            tmp_path.replace(path)
        if cursor_path is not None:
            try:
                if cursor_path.exists():
                    cursor_path.unlink()
                    logger.info("{}.stale_cursor_cleared path={}", event, cursor_path)
            except Exception as ce:
                logger.warning("{}.cursor_clear_failed error={}", event, str(ce)[:120])
        logger.info("{}.credentials_saved path={}", event, path)
    except Exception as e:
        logger.error("{}.credentials_save_failed error={}", event, str(e)[:200])
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
        logger.debug("{}.credentials_not_found path={}", event, path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.debug("{}.credentials_invalid_format path={}", event, path)
            return None
        if required_key and not data.get(required_key, ""):
            logger.debug("{}.credentials_missing_key path={} key={}", event, path, required_key)
            return None
        return data
    except Exception as e:
        logger.error("{}.credentials_load_failed error={}", event, str(e)[:200])
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
            logger.info("{}.credentials_cleared path={}", event, path)
        else:
            logger.debug("{}.credentials_clear_missing path={}", event, path)
    except Exception as e:
        logger.warning("{}.credentials_clear_failed error={}", event, str(e)[:200])


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
        with contextlib.suppress(OSError):
            env_path.chmod(0o600)
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
        with contextlib.suppress(OSError):
            env_path.chmod(0o600)


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
