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
from typing import Any

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
    _recent_replies: "OrderedDict[str, list[str]]" = OrderedDict()  # session_id -> [reply1, ...]

    def _dedup_buf(self, user_id: str) -> list[str]:
        """获取用户的去重缓冲（LRU 维护 + 上限淘汰）。

        根因修复（用户反馈"每段对话80%一样"）：去 key 从 session_id 改为 user_id。
          - 微信 adapter 根本没传 session_id（空串）→ 原 key 退化为 user_openid
          - QQ c2c 的 session_id 每小时换一次（SES-YYYYMMDD-XXXXX）→ key 频繁失效
            → 内存缓存命中失败 → 去重对最易重复的场景完全失效
          - 改用 user_id（wechat_{openid} / qq_{openid}），跨 session 稳定，重启前不换
        LRU 维护不变：OrderedDict + move_to_end + popitem(last=False) 上限淘汰，
        防止长期运行内存泄漏（原 session_id 含日期每天新增 key 的根因）。
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
          3. 内存缓存仍保留作为同进程快速路径：DB 写入是 fire-and-forget，
             同进程内连续请求时 DB 可能还没写入，内存缓存补足这个时序窗口

        机制：
        1. recent = 内存缓存 ∪ 数据库最近回复（合并去重，最新在前）
        2. 新回复与之比较 rapidfuzz 相似度
        3. 超阈值则追加 system message 要求"完全不同的表达"重试一次
        4. 重试后仍 >=70% → 返回相似度最低的版本（用户要求只重试一次，不无限重试）
        5. 无论用哪个，都更新内存缓存（DB 由 background_tasks 写入）
        """
        from utils.similarity import ratio as text_ratio

        # 根因修复：用 user_id（稳定标识）作为去 key，替代不稳定的 session_id
        ctx = _current_request_ctx.get()
        _user_id = getattr(ctx, "user_id", "") or user_openid or "_default"
        _source = getattr(ctx, "source", "") or ""

        # 1. 合并内存缓存 + 数据库最近回复（持久化去重）
        # 治本（2026-08-05）：用户明确要求"去重只跟上一条消息对比，不是跟全部历史对比"。
        # 根因：原先与最近 5 条历史逐一对比，用户反复发相似消息（"在吗""我要亲亲"）时
        #   agnes 生成相似回复 → 高相似度 → 触发去重重试 → 第二次 LLM 调用 → 总耗时 20s+。
        # 修复：只取最近 1 条回复对比（limit=1），从源头消除"与多条历史重复"的误判面，
        #   从而大幅降低触发重试的概率，保持主 LLM 调用单次 8s 内的健康耗时。
        mem_recent = self._dedup_buf(_user_id)  # 内存缓存（LRU 维护）
        db_recent: list[str] = []
        if self.db and _user_id != "_default":
            try:
                db_recent = await asyncio.wait_for(
                    self.db.get_recent_replies(_user_id, source=_source,
                                               limit=1),
                    timeout=3.0,
                )
            except asyncio.TimeoutError:
                logger.warning("reply.dedup_db_timeout user_id={}", _user_id[:24])
            except Exception as e:
                logger.warning("reply.dedup_db_failed error={}", str(e)[:200])

        # 合并：内存缓存 + DB（去重，保持最新在前，只保留最近 1 条用于对比）
        _seen: set[str] = set()
        recent: list[str] = []
        for r in list(mem_recent) + db_recent:
            if r and r not in _seen:
                _seen.add(r)
                recent.append(r)
        recent = recent[:1]

        logger.info(f"reply.dedup_probe | user={_user_id[:24]} | "
                    f"mem_cnt={len(mem_recent)} | db_cnt={len(db_recent)} | "
                    f"merged_cnt={len(recent)} | reply_preview={reply[:40]}")

        # 无历史回复，直接记录并返回
        if not recent:
            _buf = self._dedup_buf(_user_id)
            _buf.append(reply)
            if len(_buf) > self.REPLY_DEDUP_MAX:
                del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
            return reply

        # 计算与最近回复的最大相似度
        max_sim = max(text_ratio(reply, r) for r in recent)
        logger.info(f"reply.dedup_check | user={_user_id[:20]} | "
                    f"max_sim={max_sim:.1f} | merged_cnt={len(recent)}")

        if max_sim < self.REPLY_DEDUP_THRESHOLD:
            # 不重复，记录并返回（保持最近 N 条）
            _buf = self._dedup_buf(_user_id)
            _buf.append(reply)
            if len(_buf) > self.REPLY_DEDUP_MAX:
                del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
            return reply

        # 重复了，重试一次
        trace.warning("reply.duplicate_detected",
                      max_similarity=round(max_sim, 1),
                      recent_count=len(recent),
                      preview=reply[:60])

        try:
            # 治本：重试时只传 system + 最近 2 轮历史，不传完整历史。
            # 根因是模型看到历史里的重复回复跟风——截断历史让模型没有重复参考，
            # 从源头防止生成重复回复。历史仍在数据库，主路径不受影响。
            _retry_messages = [messages[0]] if messages else []  # system prompt
            _retry_messages += messages[-4:]  # 最近 2 轮（user+assistant 各 2）
            _retry_messages += [{
                "role": "system",
                "content": (
                    f"你刚才的回复与之前说过的内容相似度高达{max_sim:.0f}%，"
                    "几乎是一模一样的话。请用完全不同的措辞、句式和描写角度重新回复，"
                    "不要重复之前用过的任何描写（如'像被电流贯穿''手指死死抓着床单'等），"
                    "换一种全新的表达方式。"
                ),
            }]
            # 尊重 WebUI temperature 设定，不篡改（用户明确要求不许自动调整 temperature）
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
                _retry_sim = max(text_ratio(_retry_reply, r) for r in recent)
                if _retry_sim < self.REPLY_DEDUP_THRESHOLD:
                    # 重试成功：相似度 <70%，用重试回复
                    _buf = self._dedup_buf(_user_id)
                    _buf.append(_retry_reply)
                    if len(_buf) > self.REPLY_DEDUP_MAX:
                        del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
                    logger.info("reply.dedup_retry_ok retry_sim={:.1f}", _retry_sim)
                    return _retry_reply
                # 重试后仍 >=70%（用户要求"重试后必须 <70%，不然就是bug"）
                # 只允许重试一次，取相似度较低的版本作为兜底，并告警便于排查
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
        _buf.append(reply)
        if len(_buf) > self.REPLY_DEDUP_MAX:
            del _buf[: len(_buf) - self.REPLY_DEDUP_MAX]
        return reply
