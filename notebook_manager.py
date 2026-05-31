import json
import time
import uuid
from datetime import datetime
from typing import Optional

from loguru import logger
from db_memory import MemoryDB


class NotebookManager:

    def __init__(self, memory: MemoryDB):
        self._memory = memory

    async def add_task(self, content: str, due_date: str = "", priority: int = 5,
                       tags: str = "", user_id: str = "") -> dict:
        task_id = str(uuid.uuid4())[:8]
        due_ts = 0
        if due_date:
            try:
                for fmt in ["%Y-%m-%d %H:%M", "%Y-%m-%d", "%m-%d %H:%M", "%H:%M"]:
                    try:
                        dt = datetime.strptime(due_date, fmt)
                        if dt.year == 1900:
                            dt = dt.replace(year=datetime.now().year)
                        due_ts = int(dt.timestamp())
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        await self._memory.add(
            content=content,
            memory_type="task",
            importance=priority / 10.0,
            metadata=json.dumps({
                "task_id": task_id,
                "due_date": due_ts,
                "priority": priority,
                "tags": tags,
                "completed": False,
            }),
        )

        logger.info("notebook.task_added", id=task_id, content=content[:50])
        return {"id": task_id, "content": content, "due_date": due_ts, "priority": priority}

    async def add_note(self, content: str, tags: str = "", user_id: str = "") -> dict:
        note_id = str(uuid.uuid4())[:8]

        await self._memory.add(
            content=content,
            memory_type="note",
            importance=0.5,
            metadata=json.dumps({
                "note_id": note_id,
                "tags": tags,
                "created_at": int(time.time()),
            }),
        )

        logger.info("notebook.note_added", id=note_id, content=content[:50])
        return {"id": note_id, "content": content}

    async def get_tasks(self, include_completed: bool = False, limit: int = 20) -> list[dict]:
        raw = await self._memory.search_by_type("task", limit=limit * 2)
        tasks = []
        for item in raw:
            try:
                meta = json.loads(item.get("metadata", "{}"))
            except (json.JSONDecodeError, TypeError):
                meta = {}

            if not include_completed and meta.get("completed"):
                continue

            tasks.append({
                "id": meta.get("task_id", item.get("id", "")),
                "content": item.get("content", ""),
                "due_date": meta.get("due_date", 0),
                "priority": meta.get("priority", 5),
                "tags": meta.get("tags", ""),
                "completed": meta.get("completed", False),
                "created_at": item.get("created_at", 0),
            })

        tasks.sort(key=lambda t: (t["priority"] * -1, t["due_date"] or float("inf")))
        return tasks[:limit]

    async def get_notes(self, limit: int = 20) -> list[dict]:
        raw = await self._memory.search_by_type("note", limit=limit)
        notes = []
        for item in raw:
            try:
                meta = json.loads(item.get("metadata", "{}"))
            except (json.JSONDecodeError, TypeError):
                meta = {}
            notes.append({
                "id": meta.get("note_id", item.get("id", "")),
                "content": item.get("content", ""),
                "tags": meta.get("tags", ""),
                "created_at": meta.get("created_at", item.get("created_at", 0)),
            })
        return notes[:limit]

    async def complete_task(self, task_id: str) -> bool:
        raw = await self._memory.search_by_type("task", limit=100)
        for item in raw:
            try:
                meta = json.loads(item.get("metadata", "{}"))
            except (json.JSONDecodeError, TypeError):
                continue
            if meta.get("task_id") == task_id:
                meta["completed"] = True
                meta["completed_at"] = int(time.time())
                await self._memory.update_metadata(item["id"], json.dumps(meta))
                logger.info("notebook.task_completed", id=task_id)
                return True
        return False

    async def delete_item(self, item_id: str) -> bool:
        return await self._memory.delete(item_id)

    async def get_due_tasks(self, window_seconds: int = 3600) -> list[dict]:
        tasks = await self.get_tasks(include_completed=False)
        now = time.time()
        due = []
        for t in tasks:
            due_date = t.get("due_date", 0)
            if due_date > 0 and due_date <= now + window_seconds:
                due.append(t)
        return due

    async def get_summary(self) -> dict:
        tasks = await self.get_tasks(limit=100)
        notes = await self.get_notes(limit=100)
        due_soon = await self.get_due_tasks(window_seconds=86400)

        return {
            "total_tasks": len(tasks),
            "pending_tasks": len([t for t in tasks if not t.get("completed")]),
            "due_today": len(due_soon),
            "total_notes": len(notes),
        }
