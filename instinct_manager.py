import time
import difflib
from loguru import logger

from db.database import DatabaseManager
from model_router import ModelRouter

EXTRACT_PROMPT = """请分析以下对话，提取可复用的模式、经验或用户偏好。每个模式用一句话描述，附带0-1的置信度。

格式要求（每行一个）：
模式描述 | 置信度

示例：
用户喜欢用中文交流，偶尔夹杂英文技术术语 | 0.9
用户是开发者，经常需要代码调试帮助 | 0.85

对话内容：
用户：{user_input}
助手：{reply}"""


class InstinctManager:

    def __init__(self, db: DatabaseManager, router: ModelRouter):
        self.db = db
        self.router = router
        self._available = db is not None

    async def init(self):
        """创建 instincts 表"""
        if not self._available:
            return
        await self.db._conn.executescript("""
            CREATE TABLE IF NOT EXISTS instincts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source_session TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                last_used_at REAL NOT NULL,
                use_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_instincts_status ON instincts(status);
            CREATE INDEX IF NOT EXISTS idx_instincts_confidence ON instincts(confidence);
            CREATE INDEX IF NOT EXISTS idx_instincts_last_used ON instincts(last_used_at);
        """)
        await self.db._conn.commit()
        logger.info("instinct.table_ready")

    async def extract_instincts(self, user_input: str, reply: str, session_id: str):
        """对话结束后异步提取 Instinct，使用 LLM 分析对话提取可复用模式"""
        if not self._available:
            return
        prompt = EXTRACT_PROMPT.format(user_input=user_input, reply=reply)
        try:
            result = await self.router.route(
                task_type="chat_mini",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=400,
            )
        except Exception as e:
            logger.warning("instinct.extract_llm_failed", error=str(e))
            return

        if not result or not isinstance(result, str):
            return

        now = time.time()
        inserted = 0
        for line in result.strip().splitlines():
            line = line.strip()
            if "|" not in line:
                continue
            parts = line.rsplit("|", 1)
            if len(parts) != 2:
                continue
            content = parts[0].strip()
            try:
                confidence = float(parts[1].strip())
            except ValueError:
                confidence = 0.5
            confidence = max(0.0, min(1.0, confidence))

            if not content or confidence < 0.5:
                continue

            try:
                await self.db._conn.execute(
                    """INSERT INTO instincts
                       (content, confidence, source_session, status, created_at, last_used_at, use_count)
                       VALUES (?, ?, ?, 'active', ?, ?, 0)""",
                    (content, confidence, session_id, now, now),
                )
                inserted += 1
            except Exception as e:
                logger.debug("instinct.insert_failed", content=content[:40], error=str(e))

        if inserted > 0:
            await self.db._conn.commit()
            logger.info("instinct.extracted", count=inserted, session=session_id)

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

    async def mark_used(self, instinct_id: int):
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
        """归档超过 max_age_days 天未使用的 Instinct"""
        if not self._available:
            return 0
        cutoff = time.time() - max_age_days * 86400
        # 先查询数量
        cursor = await self.db._conn.execute(
            "SELECT COUNT(*) FROM instincts WHERE status='active' AND last_used_at < ?",
            (cutoff,)
        )
        row = await cursor.fetchone()
        count = row[0] if row else 0
        # 再执行更新
        if count > 0:
            await self.db._conn.execute(
                "UPDATE instincts SET status='archived' WHERE status='active' AND last_used_at < ?",
                (cutoff,)
            )
            await self.db._conn.commit()
            logger.info("instinct.archived_stale", count=count)
        return count

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

        merged_ids: set[int] = set()
        merge_count = 0

        for i in range(len(rows)):
            if rows[i]["id"] in merged_ids:
                continue
            for j in range(i + 1, len(rows)):
                if rows[j]["id"] in merged_ids:
                    continue
                ratio = difflib.SequenceMatcher(
                    None, rows[i]["content"], rows[j]["content"]
                ).ratio()
                if ratio >= similarity_threshold:
                    # 保留置信度更高的，归档另一个
                    merged_ids.add(rows[j]["id"])
                    # 将被合并的使用次数加到保留项上
                    await self.db._conn.execute(
                        """UPDATE instincts SET use_count=use_count+?
                           WHERE id=?""",
                        (rows[j]["use_count"], rows[i]["id"]),
                    )
                    await self.db._conn.execute(
                        "UPDATE instincts SET status='archived' WHERE id=?",
                        (rows[j]["id"],),
                    )
                    merge_count += 1

        if merge_count > 0:
            await self.db._conn.commit()
            logger.info("instinct.merged_duplicates", count=merge_count)
        return merge_count

    async def curator_run(self):
        """Curator 一次完整运行：归档过期 + 合并重复"""
        if not self._available:
            return
        archived = await self.archive_stale()
        merged = await self.merge_duplicates()
        logger.info("instinct.curator_done", archived=archived, merged=merged)

    async def build_instinct_prompt(self) -> str:
        """构建 Instinct 提示文本，用于注入系统提示，同时标记被使用的 Instinct"""
        instincts = await self.get_active_instincts()
        if not instincts:
            return ""

        # 标记被使用的 Instinct
        for inst in instincts:
            try:
                await self.mark_used(inst["id"])
            except Exception:
                pass

        lines = [f"· {inst['content']}" for inst in instincts]
        return "[已学习的经验模式（仅供参考，根据当前对话独立判断）]\n" + "\n".join(lines)
