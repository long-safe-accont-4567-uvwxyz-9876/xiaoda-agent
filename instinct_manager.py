import os
import time
import asyncio
import difflib
import httpx
from loguru import logger

from db.database import DatabaseManager
from model_router import ModelRouter

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

EXTRACT_PROMPT = """从以下对话中提取可复用的用户偏好或行为模式。只输出结果，不要解释。

严格格式（每行一条，用 | 分隔）：
模式描述 | 置信度

示例（不要输出这些，仅作格式参考）：
用户偏好浓香入味的菜品 | 0.8
用户倾向于直接处理问题 | 0.75

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

    def set_free_model_client(self, api_key: str, base_url: str, model: str) -> None:
        """配置硅基流动免费模型客户端"""
        self._free_api_key = api_key
        self._free_base_url = base_url
        self._free_model = model

    async def _call_free_model(self, messages: list, temperature: float = 0.6,
                                max_tokens: int = 800) -> str | None:
        """调用硅基流动免费模型"""
        if not self._free_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
                )
                response.raise_for_status()
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("instinct.free_model_failed", error=str(e))
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
        if not self._available:
            return
        prompt = EXTRACT_PROMPT.format(user_input=user_input, reply=reply)
        messages = [{"role": "user", "content": prompt}]

        # 优先调用硅基流动免费模型，失败则降级到 router
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=800)
        if result is None:
            try:
                # 修复 P0-2 同类 bug：降级路由加 10s 超时保护
                # 根因：原代码 router.route 无超时，主模型卡住会让 extract_instincts
                # 阻塞 30-53s（日志 bg.task_slow name=extract_instincts elapsed=30-53s）。
                # instinct 提取是后台任务，不应阻塞这么久；超时则放弃本次提取。
                result = await asyncio.wait_for(
                    self.router.route(
                        task_type="chat_mini",
                        messages=messages,
                        temperature=0.3,
                        max_tokens=800,
                    ),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning("instinct.extract_router_timeout, skip this round")
                return
            except Exception as e:
                logger.warning("instinct.extract_llm_failed", error=str(e))
                return

        if not result or not isinstance(result, str):
            return

        now = time.time()
        rows_to_insert = []
        for line in result.strip().splitlines():
            ln = line.strip()
            if ln.startswith(("<tool_call>", "```")):
                continue
            if "|" not in ln:
                continue
            parts = ln.rsplit("|", 1)
            if len(parts) != 2:
                continue
            content = parts[0].strip().lstrip("-").strip()  # 去掉前导 "- "
            try:
                confidence = float(parts[1].strip())
            except ValueError:
                logger.debug("instinct_manager: skipping line with non-numeric confidence: {!r}", line, exc_info=True)
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

            rows_to_insert.append((content, confidence, session_id, now, now))

        if rows_to_insert:
            try:
                await self.db._conn.executemany(
                    """INSERT INTO instincts
                       (content, confidence, source_session, status, created_at, last_used_at, use_count)
                       VALUES (?, ?, ?, 'active', ?, ?, 0)""",
                    rows_to_insert,
                )
                await self.db._conn.commit()
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
        use_count_increments: dict[int, int] = {}
        archive_ids: list[int] = []

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
                    archive_ids.append(rows[j]["id"])
                    # 将被合并的使用次数加到保留项上（聚合后批量更新）
                    use_count_increments[rows[i]["id"]] = (
                        use_count_increments.get(rows[i]["id"], 0) + rows[j]["use_count"]
                    )
                    merge_count += 1

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
