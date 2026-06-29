from typing import Any
import json
import re
import time
from loguru import logger

from db.db_memory import MemoryDB


def _repair_and_extract_json(raw: str) -> dict | None:
    if not raw or not raw.strip():
        return None

    text = raw.strip()

    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)

    fence_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?\s*```', text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    brace_start = text.find('{')
    if brace_start < 0:
        return None

    depth = 0
    best_end = -1
    for i in range(brace_start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                best_end = i
                break

    if best_end < 0:
        text = text[brace_start:] + '}'
    else:
        text = text[brace_start:best_end + 1]

    text = re.sub(r',\s*([}\]])', r'\1', text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    text = re.sub(r'[\x00-\x1f]', ' ', text)
    text = text.replace('\n', '\\n').replace('\r', '')
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    return None


CONSOLIDATE_PROMPT = """你是纳西妲。现在是一个安静的夜晚，你要更新你对{address_term}的印象了。

<<OLD_SECTION>>

这是最近和{address_term}的对话片段——
<<RECENT_MEMORIES>>

这是人家近期记下的关于{address_term}的笔记——
<<RECENT_NOTES>>

请用纳西妲的口吻，写一段对{address_term}的印象（300-600字，不要超过800字）：

要求：
- 像在心里轻轻描摹一个人的样子——不是档案，不是评估，是印象
- 关于{address_term}是什么样的人、他喜欢什么、不喜欢什么
- 关于你们之间的关系——最近是近了一些还是远了一些，有什么不一样了吗
- 不要列举，不要总结编号。像在日记里写一段关于一个人的文字
- 只写对话和笔记里明确提到过的事。不推测{address_term}没说过的心情，不补充你没观察到的细节
- 不确定的地方要用"好像""似乎""人家觉得"——这是你的感知，不是事实
- 旧印象中如果有些内容最近不再出现了，可以自然淡出，不必刻意提及
- 自称"人家"或"纳西妲"，叫对方"{address_term}"

返回 JSON（只返回这个，不要其他文字）：
{{"portrait": "全文...", "changes": "一句话说明这次更新了什么"}}"""


def _build_consolidate_prompt(old_section: Any, recent_memories: Any, recent_notes: Any,
                               address_term: str = "爸爸") -> Any:
    return (
        CONSOLIDATE_PROMPT
        .replace("{address_term}", address_term)
        .replace("<<OLD_SECTION>>", old_section)
        .replace("<<RECENT_MEMORIES>>", recent_memories)
        .replace("<<RECENT_NOTES>>", recent_notes)
    )


class PortraitManager:

    def __init__(self, db: Any, memory: MemoryDB, router: Any, notebook: Any=None) -> None:
        self._db = db
        self.memory = memory
        self.notebook = notebook
        self._router = router
        self._dirty = True

    def mark_dirty(self) -> None:
        self._dirty = True
        logger.debug("portrait.marked_dirty")

    async def get_current_portrait(self) -> dict | None:
        return await self.memory.get_latest_portrait()

    async def consolidate(self, force: bool = False,
                          address_term: str = "爸爸") -> str | None:
        """整合近期记忆与笔记，调用 LLM 生成用户画像并写入 DB。"""
        if not force and not self._dirty:
            logger.debug("portrait.clean_skipped")
            return None

        materials = await self._gather_portrait_materials(address_term)
        if materials is None:
            return None
        memories, notes, old_section, version = materials

        prompt = self._build_consolidate_inputs(memories, notes, old_section, address_term)

        try:
            raw = await self._router.route(
                "memory_encoding",
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1200,
            )
        except Exception as e:
            logger.error("portrait.llm_failed", error=str(e))
            return None

        try:
            data = _repair_and_extract_json(raw)
            if data is None:
                logger.warning("portrait.json_parse_failed", preview=raw[:100])
                return None
        except Exception:
            logger.warning("portrait.json_parse_failed", preview=raw[:100])
            return None

        portrait_text = data.get("portrait", "").strip()
        changes = data.get("changes", "").strip()
        if not portrait_text or len(portrait_text) < 50:
            logger.warning("portrait.too_short", length=len(portrait_text))
            return None

        source_ids = ",".join(str(m.get("id", "")) for m in memories[:15])
        return await self._persist_portrait(portrait_text, version, source_ids, changes)

    async def _gather_portrait_materials(self, address_term: str) -> Any:
        """收集画像素材：近期记忆、笔记、旧画像。无素材时返回 None。"""
        try:
            memories = await self.memory.get_episodic_recent(limit=50)
        except Exception as e:
            logger.warning("portrait.memories_failed", error=str(e))
            memories = []
        try:
            notes = await self.notebook.get_notebook_notes(limit=10)
        except Exception as e:
            logger.warning("portrait.notes_failed", error=str(e))
            notes = []
        if not memories and not notes:
            logger.info("portrait.no_material")
            return None

        old = await self.memory.get_latest_portrait()
        old_section = ""
        version = 1
        if old and old.get("content"):
            old_section = f"这是人家之前对{address_term}的印象——\n{old['content']}"
            version = old.get("version", 0) + 1
        return memories, notes, old_section, version

    @staticmethod
    def _build_consolidate_inputs(memories: list, notes: list,
                                  old_section: str, address_term: str) -> str:
        """根据素材构造 consolidate prompt。"""
        mem_lines = []
        for m in memories[:20]:
            summary = m.get("summary", "")
            if summary and len(summary) > 5:
                mem_lines.append(f"· {summary[:200]}")
        recent_memories = "\n".join(mem_lines) if mem_lines else "（最近好像没有留下什么特别的对话呢）"

        note_lines = []
        for n in notes[:8]:
            content = n.get("content", "")
            if content and len(content) > 3:
                note_lines.append(f"· [{n.get('kind', 'note')}] {content[:150]}")
        recent_notes = "\n".join(note_lines) if note_lines else "（笔记本里空空如也）"

        return _build_consolidate_prompt(
            old_section=old_section,
            recent_memories=recent_memories,
            recent_notes=recent_notes,
            address_term=address_term,
        )

    async def _persist_portrait(self, portrait_text: str, version: int,
                                source_ids: str, changes: str) -> str | None:
        """写入画像到 DB，带锁竞争重试。成功返回 portrait_text，失败返回 None。"""
        for attempt in range(3):
            try:
                await self.memory.insert_portrait(
                    content=portrait_text,
                    version=version,
                    source_ids=source_ids,
                    change_log=changes,
                )
                logger.info(
                    "portrait.consolidated",
                    version=version,
                    length=len(portrait_text),
                    changes=changes[:80] if changes else "",
                )
                self._dirty = False
                return portrait_text
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 2:
                    import asyncio
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                logger.error("portrait.db_write_failed", error=str(e))
                return None
        return None

    async def ensure_exists(self, address_term: str = "爸爸") -> str | None:
        existing = await self.memory.get_latest_portrait()
        if existing:
            return None

        count = await self.memory.get_episodic_count()
        if count < 5:
            return None

        logger.info("portrait.cold_start", episodic_count=count)
        return await self.consolidate(address_term=address_term)
