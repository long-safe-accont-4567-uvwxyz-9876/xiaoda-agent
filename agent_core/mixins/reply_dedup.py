"""ReplyDedupMixin —— Phase 4 拆分自 message_processor.py。

包含跨对话回复去重逻辑：_dedup_buf（LRU 内存缓冲）与
_dedup_reply_against_recent（相似度检测 + 超阈值重试一次），
及 LRU 缓存类属性 REPLY_DEDUP_SESSION_CAP / _recent_replies。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared、agent_core.mixins.voice
及 config/core 叶子模块，不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any, ClassVar

from loguru import logger

from agent_core._shared import _current_request_ctx
from agent_core.mixins.voice import _get_temperature


class ReplyDedupMixin:
    """跨对话回复去重相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    # ── 跨对话回复去重：LRU 会话缓存容量 ────────────────────
    # Phase 4 拆分：REPLY_DEDUP_SESSION_CAP / _recent_replies 及方法
    # _dedup_buf / _dedup_reply_against_recent 自 message_processor.py 逐字节迁至本模块。
    # 其余去重常量（REPLY_DEDUP_MAX / REPLY_DEDUP_THRESHOLD / REPLY_DEDUP_RETRY_TIMEOUT）
    # 仍定义在 message_processor.py，方法内经 self.* 由 MRO 解析。
    # P0 修复（内存泄漏根因）：session_id 含日期(SES-YYYYMMDD-...)，每天新增 key。
    # 原实现 _recent_replies: dict 的 session key 永不清理，长期运行无限增长 →
    # 内存泄漏 → 渐进退化。改为 OrderedDict + LRU，超过 cap 淘汰最久未访问的 session。
    #
    # cap 取值评估（确保不导致去重能力受限）：
    #   - c2c 场景：通常 1-5 个活跃用户，每用户 1 session
    #   - 群聊场景：session_id=qq_group:{openid}:...，每群活跃用户一个 session
    #     多群 + 大群可能 100+ 活跃 session
    #   - 取 256 覆盖群聊大群场景，正常使用永不触发淘汰 → 去重能力不受限
    #   - 内存上限：256 session × 5 reply × ~200字符 ≈ 256KB，完全可控
    #   - 极端场景（>256 活跃 session）LRU 淘汰最久未访问者，该用户下次对话
    #     去重历史为空重新积累，是 graceful 行为而非功能损坏
    REPLY_DEDUP_SESSION_CAP = 256       # 最大缓存 session 数，LRU 淘汰
    _recent_replies: ClassVar["OrderedDict[str, list[tuple[str, str]]]"] = OrderedDict()  # user_id -> [(user_msg, reply), ...]

    @staticmethod
    def _extract_user_text(messages: Any) -> str:
        """从消息列表末尾向前取最近一条 user 文本（新回复所响应的输入）。

        兼容单字符串 content 与多块（图片+文本）content 两种结构。
        """
        if not messages:
            return ""
        for m in reversed(messages):
            if not isinstance(m, dict):
                continue
            if m.get("role") != "user":
                continue
            content = m.get("content")
            if isinstance(content, str):
                return content.strip()
            if isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    if isinstance(block, dict):
                        parts.append(str(block.get("text", "") or block.get("content", "") or ""))
                    elif isinstance(block, str):
                        parts.append(block)
                text = "".join(parts).strip()
                if text:
                    return text
        return ""

    def _dedup_buf(self, user_id: str) -> list[tuple[str, str]]:
        """获取用户的去重缓冲（LRU 维护 + 上限淘汰），元素为 (user_msg, reply)。

        2026-08-21：从元素字符串升级为交换对 (user_msg, reply)，去重窗口
        需要 user 消息做相似度匹配；LRU/上限语义不变。
        """
        _dd = self._recent_replies
        buf = _dd.setdefault(user_id, [])
        _dd.move_to_end(user_id)
        while len(_dd) > self.REPLY_DEDUP_SESSION_CAP:
            _dd.popitem(last=False)
        return buf

    async def _dedup_reply_against_recent(
        self, reply: str, messages: Any, task_type: Any, _model_cfg: Any,
        _cb_max_tokens: Any, user_openid: Any, session_id: Any, trace: Any,
    ) -> str:
        """跨对话回复去重：检测新回复与最近回复的相似度，重复则重试一次。

        根因修复（用户反馈"每段对话80%一样"，且要求"重试后相似度必须 <70%，只允许重试一次"）：
          1. 去 key 从 session_id 改为 user_id（稳定标识）：
             - 微信 adapter 不传 session_id（空串）→ 原 key 退化为 user_openid
             - QQ c2c session_id 每小时换（SES-YYYYMMDD-XXXXX）→ 内存缓存频繁失效
             → 改用 user_id（wechat_{openid}/qq_{openid}），跨 session 稳定
          2. 持久化去重：从 conversation_logs 查最近回复，替代易失内存缓存：
             - 服务重启后内存清空 → 去重历史丢失 → 相同输入生成相同回复
             - 从 DB 按 user_id 查询，确保重启后/换 session 后去重状态不丢失
          3. 内存缓存仍保留作为同进程快速路径：DB 写入 fire-and-forget，
             同进程连续请求时 DB 可能还没写入，内存缓存补足这个时序窗口

        2026-08-21 窗口根因修复（用户"相同内容一直返回相同回复/重复度80%"）：
          旧实现只对比"最近 1 条回复"（recent[:1] + DB limit=1），相同问题
          隔 5-10 轮再发时旧回复早已滚出窗口，去重对重复场景失效（生产日志
          09:51 与 11:53 两次"看看？"回复完全相同，dedup max_sim 仅 9.9）。
          现按交换对 (user_message, assistant_reply) 维护最近窗口：
          - 新回复先用「当前用户消息」与窗口内 user 消息做字面相似度匹配
            （>= REPLY_DEDUP_USER_SIM 视为同一问题重发），命中的旧回复为候选
          - 无 user 文本或无匹配候选时，退回"最近 1 条对比"（保持 08-05
            "在吗"类变体消息不误触发重试的收益）

        机制：
        1. recent = 内存缓存 ∪ 数据库最近交换对（最新在前，按 reply 去重）
        2. 候选回复与它算 rapidfuzz 相似度
        3. 超阈值则追加 system message 重试一次
        4. 重试后仍 >=70% → 取相似度较低版本（用户要求只重试一次）
        5. 无论哪个都更新内存缓存
        """
        from utils.similarity import ratio as text_ratio

        ctx = _current_request_ctx.get()
        _user_id = getattr(ctx, "user_id", "") or user_openid or "_default"
        _source = getattr(ctx, "source", "") or ""
        _cur_user_text = (getattr(ctx, "user_input", "") or "").strip()
        if not _cur_user_text:
            _cur_user_text = self._extract_user_text(messages)

        # 1. 合并内存缓存 + DB 交换对（(user_msg, reply)，最新在前，reply 去重）
        mem_recent = self._dedup_buf(_user_id)  # 内存缓存（LRU 维护）
        db_recent: list[tuple[str, str]] = []
        if self.db and _user_id != "_default":
            try:
                db_recent = await asyncio.wait_for(
                    self.db.get_recent_exchanges(
                        _user_id, source=_source, limit=self.REPLY_DEDUP_DB_WINDOW,
                    ),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                logger.warning("reply.dedup_db_timeout user_id={}", _user_id[:24])
            except Exception as e:
                logger.warning("reply.dedup_db_failed error={}", str(e)[:200])

        _seen: set[str] = set()
        merged: list[tuple[str, str]] = []
        for _um, _r in list(reversed(mem_recent)) + db_recent:
            if not _r or _r in _seen:
                continue
            _seen.add(_r)
            merged.append((_um or "", _r))
        merged = merged[: self.REPLY_DEDUP_DB_WINDOW]

        logger.info("reply.dedup_probe | user={} | "
                    "mem_cnt={} | db_cnt={} | "
                    "merged_cnt={} | user_input={} | reply_preview={}",
                    _user_id[:24], len(mem_recent), len(db_recent),
                    len(merged), _cur_user_text[:20], reply[:30])

        # 2. 候选回复：同一问题（user 消息相似）的旧回复；否则退回最近 1 条
        candidates: list[str] = []
        if _cur_user_text:
            candidates = [
                r for um, r in merged
                if um and text_ratio(_cur_user_text, um) >= self.REPLY_DEDUP_USER_SIM
            ]
        if not candidates and merged:
            candidates = [merged[0][1]]  # user 消息不同/无：只跟最近 1 条对比

        if not candidates:
            _buf = self._dedup_buf(_user_id)
            _buf.append((_cur_user_text, reply))
            while len(_buf) > self.REPLY_DEDUP_MAX:
                _buf.pop(0)
            return reply

        # 2. 计算与候选的最大相似度
        max_sim = max(text_ratio(reply, r) for r in candidates)
        logger.info("reply.dedup_check | user={} | "
                    "max_sim={:.1f} | candidates={}",
                    _user_id[:20], max_sim, len(candidates))

        if max_sim < self.REPLY_DEDUP_THRESHOLD:
            # 不重复，记录并返回（保持最近 N 条）
            _buf = self._dedup_buf(_user_id)
            _buf.append((_cur_user_text, reply))
            while len(_buf) > self.REPLY_DEDUP_MAX:
                _buf.pop(0)
            return reply

        # 3. 重复了，重试一次
        trace.warning("reply.duplicate_detected",
                      max_similarity=round(max_sim, 1),
                      recent_count=len(candidates),
                      preview=reply[:60])

        try:
            # 重试时只传 system + 最近 2 轮历史，截断重试参考让小妲换一种说法
            _retry_messages = [messages[0]] if messages else []  # system prompt
            _retry_messages += (messages or [])[-4:]  # 最近 2 轮（user+assistant 各 2）
            _retry_messages += [{
                "role": "system",
                "content": (
                    f"你刚才的回复与之前说过的内容相似度高达{max_sim:.0f}%，"
                    "几乎是一模一样的话。请用完全不同的措辞、句式和描写角度重新回复，"
                    "不要重复之前用过的任何描写（如'像被电流贯穿''手指死死抓着床单'等），"
                    "换一种全新的表达方式。"
                ),
            }]
            _retry_result = await asyncio.wait_for(
                self.router.route(
                    task_type, _retry_messages,
                    temperature=_get_temperature(_model_cfg),
                    max_tokens=_cb_max_tokens,
                    user_openid=user_openid, session_id=session_id,
                ),
                timeout=self.REPLY_DEDUP_RETRY_TIMEOUT,
            )
            _retry_reply = ""
            if isinstance(_retry_result, str):
                _retry_reply = self._clean_reply(_retry_result)
            else:
                _retry_reply = getattr(
                    _retry_result.choices[0].message, "content", "") or ""
                _retry_reply = self._clean_reply(_retry_reply)

            if _retry_reply and len(_retry_reply) > 20:
                _retry_sim = max(text_ratio(_retry_reply, r) for r in candidates)
                if _retry_sim < self.REPLY_DEDUP_THRESHOLD:
                    _buf = self._dedup_buf(_user_id)
                    _buf.append((_cur_user_text, _retry_reply))
                    while len(_buf) > self.REPLY_DEDUP_MAX:
                        _buf.pop(0)
                    logger.info("reply.dedup_retry_ok retry_sim={:.1f}", _retry_sim)
                    return _retry_reply
                if _retry_sim < max_sim:
                    reply = _retry_reply
                    max_sim = _retry_sim
                trace.warning("reply.dedup_retry_still_duplicate",
                              retry_sim=round(_retry_sim, 1),
                              threshold=self.REPLY_DEDUP_THRESHOLD,
                              hint="重试后仍超阈值，取相似度最低版本兜底")
        except asyncio.TimeoutError:
            logger.warning("reply.dedup_retry_timeout timeout={}",
                           self.REPLY_DEDUP_RETRY_TIMEOUT)
        except Exception as e:
            logger.warning("reply.dedup_retry_failed error={}", str(e)[:200])

        _buf = self._dedup_buf(_user_id)
        _buf.append((_cur_user_text, reply))
        while len(_buf) > self.REPLY_DEDUP_MAX:
            _buf.pop(0)
        return reply