import os
import time
import asyncio
from utils.similarity import ratio as text_ratio
import httpx
from loguru import logger

from db.database import DatabaseManager
from model_router import ModelRouter
from utils.http_pool import get_shared_client

# LLM 思考过程特征词 — 包含这些词的行不是有效的本能
_LLM_THINKING_KEYWORDS = {
    "首先", "好的", "格式要求", "用户要求", "任务是", "我需要", "让我分析",
    "根据对话", "从对话中可以看出", "分析给定的对话", "提取可复用",
    "模式描述", "置信度", "每行一个", "现在请",
    # 心理学/操控类描述（LLM 过度解读用户行为）
    "操控", "诱导", "情感依赖", "合理化", "矛盾心理", "利用", "暗示",
    "承认并", "正当化", "控制欲", "心理", "依赖", "妥协",
    # 思考链泄漏标记
    "0uits", "0udge", "思维链", "reasoning", "思考过程",
}

# prompt 示例内容 — 防止 LLM 直接复制示例
_PROMPT_EXAMPLE_FRAGMENTS = {
    "用户喜欢用中文交流", "用户是开发者", "经常需要代码调试",
    "用户偏好浓香入味的菜品", "用户倾向于直接处理问题",
}

# 无效本能模式（正则）— 拒绝过短、模板化、或非用户偏好类内容
import re as _re
_INVALID_INSTINCT_PATTERNS = [
    _re.compile(r"^用户行为模式"),     # 模板化标题
    _re.compile(r"^用户提问"),          # 单次行为非偏好
    _re.compile(r"^\d+\..*uts"),        # 思考链碎片
    _re.compile(r"模型退化|训练数据|上下文过载"),  # 模型自我描述
]

EXTRACT_PROMPT = """从以下对话中分析用户的偏好和行为模式。只输出结果，不要解释。

做两件事：
1. 提取可复用的新偏好/行为模式（如果有的话）
2. 如果用户在否定/纠正已有的判断（如"我不是这样的"、"你记错了"、"那个不对"、"别这样了"、"你搞反了"），指出被否定的内容

严格格式（每行一条，用 | 分隔，第一列标明类型）：
NEW | 模式描述 | 置信度
CORRECT | 被否定的内容 | 动作(demote或archive)

示例（不要输出这些，仅作格式参考）：
NEW | 用户偏好浓香入味的菜品 | 0.8
CORRECT | 用户喜欢被打断时继续说 | archive

如果没有新偏好或没有否定，对应行省略不输出。

对话内容：
用户：{user_input}
助手：{reply}

提取结果："""


class InstinctManager:
    """本能管理器，调用免费模型维护与提取本能规则。"""

    def __init__(self, db: DatabaseManager, router: ModelRouter) -> None:
        self.db = db
        self.router = router
        self._available = db is not None
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = "https://api.siliconflow.cn/v1"
        self._free_model = "THUDM/GLM-4-9B-0414"  # 非思考模型，避免 Z1 思考碎片污染本能提取
        self._disabled = False          # backend=off：完全禁用提取
        self._backend = "auto"          # auto/local/api/off（local=走主 LLM）
        self._backup_free_api_key = ""  # backend=local 时的 key 备份

    def set_free_model_client(self, api_key: str, base_url: str, model: str) -> None:
        """配置硅基流动免费模型客户端"""
        self._free_api_key = api_key
        self._free_base_url = base_url
        self._free_model = model

    def set_backend(self, backend: str) -> None:
        """热更新后端选择：off=禁用；local=禁用免费模型走主 LLM；api/auto=恢复免费模型。"""
        if backend not in ("auto", "local", "api", "off"):
            return
        self._backend = backend
        if backend == "off":
            self._disabled = True
            return
        self._disabled = False
        if backend == "local":
            self._backup_free_api_key = self._free_api_key
            self._free_api_key = ""
        else:
            if self._backup_free_api_key:
                self._free_api_key = self._backup_free_api_key
        logger.info("instinct.backend_set backend={} disabled={}", backend, self._disabled)

    async def _call_free_model(self, messages: list, temperature: float = 0.6,
                                max_tokens: int = 800) -> str | None:
        """调用硅基流动免费模型"""
        if not self._free_api_key:
            return None
        try:
            # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
            client = get_shared_client()
            response = await client.post(
                f"{self._free_base_url}/chat/completions",
                json={
                    "model": self._free_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {self._free_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(15.0),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            # 修复 P2 Bug 8: free_model_failed 频繁告警（免费模型限流/网络抖动是常态）
            # 已有降级到 router 的兜底，降级为 debug 避免告警风暴
            logger.debug("instinct.free_model_failed", error=str(e)[:200], error_type=type(e).__name__)
            return None

    async def init(self) -> None:
        """创建 instincts 表"""
        if not self._available:
            return
        # 逐条执行 DDL，避免 executescript() 在 vfat 上触发隐式 commit 导致 database is locked
        await self.db._conn.execute("""
            CREATE TABLE IF NOT EXISTS instincts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_session TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        await self.db._conn.execute("CREATE INDEX IF NOT EXISTS idx_instincts_status ON instincts(status)")
        await self.db._conn.execute("CREATE INDEX IF NOT EXISTS idx_instincts_confidence ON instincts(confidence)")
        await self.db._conn.execute("CREATE INDEX IF NOT EXISTS idx_instincts_last_used ON instincts(last_used_at)")
        await self.db._conn.commit()
        logger.info("instinct.table_ready")

    async def extract_instincts(self, user_input: str, reply: str, session_id: str) -> None:
        """对话结束后异步提取 Instinct，使用 LLM 分析对话提取可复用模式"""
        if not self._available or self._disabled:
            return
        # 防御性加固：用 str.replace 替代 str.format
        # 根因：user_input/reply 可能含 {} / {0} 等字符（JSON/Python 代码/正则），
        # .format() 会抛 IndexError/KeyError 导致 extract_instincts 失败（与
        # profile_learner.insight_failed 同类 bug）。
        prompt = (
            EXTRACT_PROMPT
            .replace("{user_input}", user_input)
            .replace("{reply}", reply)
        )
        messages = [{"role": "user", "content": prompt}]

        # 优先调用硅基流动免费模型，失败则降级到 router
        _llm_t0 = time.time()
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=800)
        _free_ms = int((time.time() - _llm_t0) * 1000)
        if result is None:
            try:
                # 修复 P0-2 同类 bug：降级路由加 10s 超时保护
                # 根因：原代码 router.route 无超时，主模型卡住会让 extract_instincts
                # 阻塞 30-53s（日志 bg.task_slow name=extract_instincts elapsed=30-53s）。
                # instinct 提取是后台任务，不应阻塞这么久；超时则放弃本次提取。
                # task_type 用 memory_encoding（后台任务），让 route() 的 _chat_idle 机制
                # 使其自动让路于主 chat，避免和主对话并发竞争 agnes API（并发排队根因）。
                _route_t0 = time.time()
                result = await asyncio.wait_for(
                    self.router.route(
                        task_type="memory_encoding",
                        messages=messages,
                        temperature=0.3,
                        max_tokens=800,
                    ),
                    timeout=10.0,
                )
                _route_ms = int((time.time() - _route_t0) * 1000)
                if _route_ms > 5000:
                    logger.warning(f"instinct.route_slow elapsed_ms={_route_ms} free_ms={_free_ms}")
            except asyncio.TimeoutError:
                logger.warning("instinct.extract_router_timeout, skip this round")
                return
            except Exception as e:
                logger.warning("instinct.extract_llm_failed", error=str(e))
                return

        if not result or not isinstance(result, str):
            return

        now = time.time()
        # 查询已有 active instincts 的 content，用于插入前去重
        # 根因修复：原代码不查重，LLM 每轮措辞略有不同但语义重复的本能被无限 INSERT
        # （4714 条，99.7% use_count=0）。这里用 difflib 检查相似度，O(new*active) 很快。
        _db_t0 = time.time()
        existing_cursor = await self.db._conn.execute(
            "SELECT content FROM instincts WHERE status='active'"
        )
        existing_rows = await existing_cursor.fetchall()
        existing_contents = [r["content"] if isinstance(r, dict) else r[0] for r in existing_rows]

        rows_to_insert = []
        skipped_duplicates = 0
        corrections = []  # 用户否定的本能（复用本次 LLM 调用，零额外开销）
        for line in result.strip().splitlines():
            ln = line.strip()
            if ln.startswith(("<tool_call>", "```")):
                continue
            if "|" not in ln:
                continue
            parts = [p.strip() for p in ln.split("|")]

            # 新格式：NEW | content | confidence  或  CORRECT | content | action
            if parts[0] in ("NEW", "CORRECT") and len(parts) >= 3:
                kind = parts[0]
                content = parts[1].lstrip("-").strip()
                third = parts[2]
                if kind == "CORRECT":
                    # 用户否定已有本能，收集后批量修正
                    action = third if third in ("demote", "archive") else "demote"
                    corrections.append((content, action))
                    continue
                # kind == "NEW"，走正常提取流程
                try:
                    confidence = float(third)
                except ValueError:
                    logger.debug("instinct_manager: skipping NEW line: {!r}", line, exc_info=True)
                    continue
            else:
                # 兼容旧格式（无前缀）：content | confidence
                content = parts[0].lstrip("-").strip() if parts else ""
                if len(parts) < 2 or not content:
                    continue
                try:
                    confidence = float(parts[-1])
                except ValueError:
                    logger.debug("instinct_manager: skipping line: {!r}", line, exc_info=True)
                    continue
            confidence = max(0.0, min(1.0, confidence))

            # 过滤无效内容
            if not content or len(content) < 5 or confidence < 0.5:
                continue
            # 过滤 LLM 思考过程
            if any(kw in content for kw in _LLM_THINKING_KEYWORDS):
                continue
            # 过滤 prompt 示例被复制
            if any(frag in content for frag in _PROMPT_EXAMPLE_FRAGMENTS):
                continue
            # 过滤模板化/非偏好类内容（正则匹配）
            if any(p.search(content) for p in _INVALID_INSTINCT_PATTERNS):
                continue
            # 插入前去重：rapidfuzz text_ratio >= 75.0（0-100 刻度，等价旧 difflib 0.75）
            is_duplicate = any(
                text_ratio(content, ex) >= 75.0
                for ex in existing_contents
            )
            if is_duplicate:
                skipped_duplicates += 1
                continue

            rows_to_insert.append((content, confidence, session_id, now, now))

        # 处理用户否定的本能（复用本次 LLM 调用，零额外开销、零新工具）
        for hint, action in corrections:
            try:
                correction = await self.correct_instinct(hint, action)
                if correction:
                    logger.info(
                        "instinct.corrected_via_extract",
                        content=correction["content"][:60], action=correction["action"],
                    )
            except Exception:
                logger.debug("instinct.correct_via_extract_failed", exc_info=True)

        if skipped_duplicates > 0:
            logger.info("instinct.dedup_skipped", count=skipped_duplicates, session=session_id)
        if rows_to_insert:
            # 根治（2026-08-05 rework）：收敛到主连接 write_transaction，删除独立写连接。
            # 根因复盘：历史"独立连接"方案用 separate aiosqlite 连接写 instincts，与主连接/
            # curator 等并发写时争抢 SQLite 单写锁 → "database is locked"（14:37:45 实证）。
            # 提高 busy_timeout（5s→20s）只是让写等待而非失败，锁竞争仍在（治标）。
            # 根治：所有写统一走主连接 + write_transaction() 的 asyncio.Lock 串行化。
            #   - 只剩一个写者（主连接），跨连接写锁无从谈起 → database is locked 消失。
            #   - aiosqlite 主连接在独立后台线程执行，串行化写不冻结事件循环（只排队）。
            #   - 关键读路径（restore_from_db）已走独立 readonly 连接，不被本写阻塞。
            #   - write_transaction 正常退出 commit，异常/取消自动 rollback，无脏事务。
            try:
                async with self.db.write_transaction() as _conn:
                    await _conn.executemany(
                        """INSERT INTO instincts
                           (content, confidence, source_session, status, created_at, last_used_at, use_count)
                           VALUES (?, ?, ?, 'active', ?, ?, 0)""",
                        rows_to_insert,
                    )
                _db_ms = int((time.time() - _db_t0) * 1000)
                if _db_ms > 2000:
                    logger.warning(f"instinct.db_slow elapsed_ms={_db_ms} active_count={len(existing_contents)}")
                logger.info("instinct.extracted", count=len(rows_to_insert), session=session_id)
            except Exception as e:
                logger.debug("instinct.insert_failed", error=str(e))

    async def get_active_instincts(self, limit: int = 6, min_confidence: float = 0.7) -> list[dict]:
        """获取活跃的 Instinct，按置信度降序"""
        if not self._available:
            return []
        cursor = await self.db._conn.execute(
            """SELECT * FROM instincts
               WHERE status='active' AND confidence >= ?
               ORDER BY confidence DESC LIMIT ?""",
            (min_confidence, limit),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def mark_used(self, instinct_id: int) -> None:
        """标记 Instinct 被使用"""
        if not self._available:
            return
        now = time.time()
        await self.db._conn.execute(
            """UPDATE instincts SET last_used_at=?, use_count=use_count+1
               WHERE id=?""",
            (now, instinct_id),
        )
        await self.db._conn.commit()

    async def archive_stale(self, max_age_days: int = 30) -> int:
        """归档明确垃圾本能（保守策略，不一刀切）。

        只归档以下"明确垃圾"：
        1. content 为空或纯空白
        2. content 长度 < 5（无意义碎片）
        3. last_used_at < cutoff（真正长期未使用，默认 30 天）

        不再按 use_count=0 一刀切归档（use_count=0 是 get_active_instincts
        只取 top 6 的排序副产品，不是垃圾证据）。疑似重复/低价值的不自动处理，
        由 correct_instinct 在对话内由用户一句话修正。
        """
        if not self._available:
            return 0
        cutoff = time.time() - max_age_days * 86400
        cursor = await self.db._conn.execute(
            """SELECT COUNT(*) FROM instincts WHERE status='active' AND (
                content IS NULL OR TRIM(content) = '' OR LENGTH(content) < 5
                OR last_used_at < ?
            )""",
            (cutoff,)
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        if count > 0:
            await self.db._conn.execute(
                """UPDATE instincts SET status='archived' WHERE status='active' AND (
                    content IS NULL OR TRIM(content) = '' OR LENGTH(content) < 5
                    OR last_used_at < ?
                )""",
                (cutoff,)
            )
            await self.db._conn.commit()
            logger.info("instinct.archived_garbage", count=count)
        return count

    @staticmethod
    def _compute_duplicates_sync(
        rows: list, similarity_threshold: float
    ) -> tuple[set, list, dict, int]:
        """同步计算重复本能（CPU 密集，必须在线程中运行）。

        P0 修复（用户反馈"回复特别慢、阻塞"根因）：
        原实现在 merge_duplicates 中直接做 O(n²) difflib.SequenceMatcher 比较，
        这是纯 CPU 密集操作，会阻塞 asyncio 事件循环 243 秒（日志证据：
        bg.task_slow name=curator_run elapsed=243.7s）。
        修复：将 CPU 密集的比较逻辑抽到独立同步方法，通过 asyncio.to_thread
        在线程池中执行，不阻塞事件循环。

        Args:
            rows: DB 查询结果 [(id, content, confidence, use_count), ...]
            similarity_threshold: 相似度阈值

        Returns:
            (merged_ids, archive_ids, use_count_increments, merge_count)
        """
        merged_ids: set[int] = set()
        merge_count = 0
        use_count_increments: dict[int, int] = {}
        archive_ids: list[int] = []

        for i in range(len(rows)):
            if rows[i]["id"] in merged_ids:
                continue
            for j in range(i + 1, len(rows)):
                if rows[j]["id"] in merged_ids:
                    continue
                ratio = text_ratio(rows[i]["content"], rows[j]["content"])
                if ratio >= similarity_threshold * 100:
                    # 保留置信度更高的，归档另一个
                    merged_ids.add(rows[j]["id"])
                    archive_ids.append(rows[j]["id"])
                    # 将被合并的使用次数加到保留项上（聚合后批量更新）
                    use_count_increments[rows[i]["id"]] = (
                        use_count_increments.get(rows[i]["id"], 0) + rows[j]["use_count"]
                    )
                    merge_count += 1
        return merged_ids, archive_ids, use_count_increments, merge_count

    async def merge_duplicates(self, similarity_threshold: float = 0.92) -> int:
        """合并语义重复的 Instinct（基于文本相似度）"""
        if not self._available:
            return 0
        cursor = await self.db._conn.execute(
            """SELECT id, content, confidence, use_count FROM instincts
               WHERE status='active' ORDER BY confidence DESC"""
        )
        rows = await cursor.fetchall()

        if len(rows) < 2:
            return 0

        # P0 修复：CPU 密集的 O(n²) 比较放到线程池，不阻塞事件循环
        # 根因：difflib.SequenceMatcher 是同步 CPU 密集操作，原实现在事件循环中
        # 直接运行导致 243 秒阻塞（bg.task_slow name=curator_run elapsed=243.7s）。
        # 通过 asyncio.to_thread 让事件循环保持响应，_spawn 的超时才能真正生效。
        merged_ids, archive_ids, use_count_increments, merge_count = (
            await asyncio.to_thread(
                self._compute_duplicates_sync, rows, similarity_threshold
            )
        )

        if merge_count > 0:
            if use_count_increments:
                await self.db._conn.executemany(
                    "UPDATE instincts SET use_count=use_count+? WHERE id=?",
                    [(inc, id_) for id_, inc in use_count_increments.items()],
                )
            if archive_ids:
                placeholders = ",".join("?" * len(archive_ids))
                await self.db._conn.execute(
                    f"UPDATE instincts SET status='archived' WHERE id IN ({placeholders})",
                    archive_ids,
                )
            await self.db._conn.commit()
            logger.info("instinct.merged_duplicates", count=merge_count)
        return merge_count

    async def curator_run(self) -> None:
        """Curator 一次完整运行：归档明确垃圾 + 合并高度重复"""
        if not self._available:
            return
        # 30 天：只归档真正长期未使用的 + 空/碎片垃圾。
        # 不再按 use_count=0 一刀切（那是排序副产品，不是垃圾证据）。
        # 疑似重复/低价值由 correct_instinct 对话内修正。
        archived = await self.archive_stale(max_age_days=30)
        merged = await self.merge_duplicates()
        logger.info("instinct.curator_done", archived=archived, merged=merged)

    async def correct_instinct(self, instinct_hint: str, action: str = "demote") -> dict | None:
        """LLM 驱动的本能修正：根据 hint 定位并修正相关本能。

        由 instinct_correct 工具调用（LLM 判断用户否定后主动触发），
        不靠硬编码词表轮询，零硬代码——语义理解交给 LLM。

        Args:
            instinct_hint: LLM 提供的被否定本能描述（用户认为错误的那条）
            action: demote=降权（confidence减半），archive=归档

        Returns:
            {"corrected": True, "content": str, "action": str,
             "old_conf": float, "new_conf": float} 或 None（未匹配）
        """
        if not self._available:
            return None
        hint = (instinct_hint or "").strip()
        if not hint:
            return None
        # 取当前 active top 6（即注入 prompt 的那几条，最可能被否定）
        instincts = await self.get_active_instincts(limit=6, min_confidence=0.0)
        if not instincts:
            return None
        # 用 rapidfuzz 定位最匹配 hint 的本能（text_ratio 返回 0-100）
        best_match = None
        best_score = 0.0
        for inst in instincts:
            score = text_ratio(hint, inst["content"])
            if score > best_score:
                best_score = score
                best_match = inst
        # 相似度阈值：50 表示中等相似（rapidfuzz 0-100 刻度）
        # 低于此值说明 hint 与所有本能都不够匹配，避免误修正
        if best_score < 50.0 or best_match is None:
            return None
        # 执行修正
        old_conf = float(best_match["confidence"])
        new_conf = old_conf * 0.5
        if action == "archive":
            await self.db._conn.execute(
                "UPDATE instincts SET status='archived', confidence=? WHERE id=?",
                (new_conf, best_match["id"])
            )
            result_action = "archived"
        else:
            await self.db._conn.execute(
                "UPDATE instincts SET confidence=? WHERE id=?",
                (new_conf, best_match["id"])
            )
            result_action = "demoted"
        await self.db._conn.commit()
        logger.info(
            "instinct.corrected",
            content=best_match["content"][:60], action=result_action,
            old_conf=old_conf, new_conf=new_conf, score=best_score,
        )
        return {
            "corrected": True,
            "content": best_match["content"],
            "action": result_action,
            "old_conf": old_conf,
            "new_conf": new_conf,
        }

    async def build_instinct_prompt(self) -> str:
        """构建 Instinct 提示文本，用于注入系统提示，同时标记被使用的 Instinct"""
        instincts = await self.get_active_instincts()
        if not instincts:
            return ""

        # 标记被使用的 Instinct（批量更新，避免 N+1 查询）
        try:
            ids = [inst["id"] for inst in instincts]
            now = time.time()
            placeholders = ",".join("?" * len(ids))
            await self.db._conn.execute(
                f"""UPDATE instincts SET last_used_at=?, use_count=use_count+1
                   WHERE id IN ({placeholders})""",
                [now, *ids],
            )
            await self.db._conn.commit()
        except Exception:
            logger.debug("instinct_manager.mark_used_failed", exc_info=True)

        lines = [f"· {inst['content']}" for inst in instincts]
        return "[已学习的经验模式（仅供参考，根据当前对话独立判断）]\n" + "\n".join(lines)
