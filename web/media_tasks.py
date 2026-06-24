"""MediaTaskQueue — TTS/图片/视频生成的异步任务队列（R11）。

单 worker 串行执行（Orange Pi 资源有限），状态机 queued → running → done/failed，
每次变化通过 broadcast 推送 media_task_update 事件并落库 media_tasks 表。
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from pathlib import Path

from loguru import logger

# 媒体目录使用用户数据目录，避免写入 _MEIPASS 只读目录
try:
    from config import MEDIA_DIR
    MEDIA_ROOT = MEDIA_DIR
except ImportError:
    MEDIA_ROOT = Path(__file__).resolve().parent / "media"


class MediaTaskQueue:
    def __init__(self, core, broadcast):
        self.core = core
        self.broadcast = broadcast
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._current: str | None = None
        for sub in ("tts", "image", "video"):
            (MEDIA_ROOT / sub).mkdir(parents=True, exist_ok=True)

    def start(self):
        if not self._worker:
            self._worker = asyncio.create_task(self._run())
            logger.info("media_queue.started")

    async def stop(self):
        if self._worker:
            self._worker.cancel()
            self._worker = None

    # ── 提交与查询 ───────────────────────────────────────

    async def submit(self, kind: str, prompt: str, params: dict | None = None) -> str:
        task_id = f"t_{uuid.uuid4().hex[:10]}"
        await self.core.db.execute(
            "INSERT INTO media_tasks(id, kind, prompt, params, status, created_at) "
            "VALUES (?,?,?,?,'queued',?)",
            (task_id, kind, prompt, json.dumps(params or {}, ensure_ascii=False), time.time()))
        await self._queue.put(task_id)
        await self._notify(task_id, "queued", 0)
        return task_id

    async def get(self, task_id: str) -> dict | None:
        return await self.core.db.fetch_one(
            "SELECT * FROM media_tasks WHERE id=?", (task_id,))

    async def list(self, limit: int = 50) -> list[dict]:
        return await self.core.db.fetch_all(
            "SELECT * FROM media_tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    async def cancel(self, task_id: str) -> bool:
        row = await self.get(task_id)
        if not row or row["status"] != "queued":
            return False
        await self._set_status(task_id, "failed", error="已取消")
        return True

    # ── worker ──────────────────────────────────────────

    async def _run(self):
        # 启动时把上次遗留的 running 标记为失败
        await self.core.db.execute(
            "UPDATE media_tasks SET status='failed', error='服务重启中断' WHERE status='running'")
        while True:
            task_id = await self._queue.get()
            row = await self.get(task_id)
            if not row or row["status"] != "queued":
                continue
            self._current = task_id
            await self._set_status(task_id, "running", progress=0.1)
            try:
                params = json.loads(row["params"] or "{}")
                handler = {"tts": self._do_tts, "image": self._do_image,
                           "video": self._do_video}.get(row["kind"])
                if not handler:
                    raise ValueError(f"未知任务类型 {row['kind']}")
                url = await asyncio.wait_for(
                    handler(row["prompt"], params),
                    timeout=600 if row["kind"] == "video" else 120)
                await self._set_status(task_id, "done", progress=1.0, result_path=url)
            except Exception as e:
                logger.warning("media_task.failed id={} error={}", task_id, str(e))
                await self._set_status(task_id, "failed", error=str(e)[:300])
            finally:
                self._current = None

    async def _set_status(self, task_id: str, status: str, progress: float | None = None,
                          result_path: str = "", error: str = ""):
        sets, vals = ["status=?"], [status]
        if progress is not None:
            sets.append("progress=?"); vals.append(progress)
        if result_path:
            sets.append("result_path=?"); vals.append(result_path)
        if error:
            sets.append("error=?"); vals.append(error)
        if status in ("done", "failed"):
            sets.append("finished_at=?"); vals.append(time.time())
        vals.append(task_id)
        await self.core.db.execute(
            f"UPDATE media_tasks SET {', '.join(sets)} WHERE id=?", tuple(vals))
        await self._notify(task_id, status, progress or 0, result_path, error)

    async def _notify(self, task_id: str, status: str, progress: float,
                      result_url: str = "", error: str = ""):
        try:
            await self.broadcast({
                "type": "media_task_update", "task_id": task_id, "status": status,
                "progress": progress, "result_url": result_url or None,
                "error": error or None,
            })
        except Exception:
            pass

    # ── 各类型执行 ───────────────────────────────────────

    async def _do_tts(self, text: str, params: dict) -> str:
        voice = params.get("voice", "nahida")
        style = params.get("style", "")
        emotion = params.get("emotion", "")
        if not self.core.tts.available:
            raise RuntimeError("TTS 引擎不可用（检查 MIMO_API_KEY 与参考音频）")
        path = await self.core.tts.synthesize(text, voice=voice, style=style, emotion=emotion)
        if not path or not Path(path).exists():
            raise RuntimeError("TTS 合成失败")
        return self._publish(Path(path), "tts")

    async def _do_image(self, prompt: str, params: dict) -> str:
        from tool_engine.tool_registry import get_tool
        tool = get_tool("agnes_image_generate")
        if not tool:
            raise RuntimeError("agnes_image_generate 工具未注册")
        result = await tool["func"](prompt=prompt,
                                    size=params.get("size", "1024x1024"),
                                    n=int(params.get("n", 1)))
        if not result.success:
            raise RuntimeError(result.error or "图片生成失败")
        return await self._extract_media(str(result.data), "image")

    async def _do_video(self, prompt: str, params: dict) -> str:
        from tool_engine.tool_registry import get_tool
        tool = get_tool("agnes_video_generate")
        if not tool:
            raise RuntimeError("agnes_video_generate 工具未注册")
        result = await tool["func"](prompt=prompt,
                                    seconds=float(params.get("seconds", 5)),
                                    fps=int(params.get("fps", 24)))
        if not result.success:
            raise RuntimeError(result.error or "视频生成失败")
        return await self._extract_media(str(result.data), "video")

    async def _extract_media(self, text: str, kind: str) -> str:
        """从工具返回文本中提取本地路径或 URL，搬运到 web/media 下。"""
        m = re.search(r"(/[^\s:]+\.(?:png|jpg|jpeg|webp|mp4|gif))", text)
        if m and Path(m.group(1)).exists():
            return self._publish(Path(m.group(1)), kind)
        m = re.search(r"(https?://\S+)", text)
        if m:
            url = m.group(1).rstrip("，。)")
            return await self._download(url, kind)
        raise RuntimeError(f"无法从结果中定位产物: {text[:120]}")

    def _publish(self, src: Path, kind: str) -> str:
        dest = MEDIA_ROOT / kind / src.name
        if src.resolve() != dest.resolve():
            shutil.copy2(str(src), str(dest))
        return f"/media/{kind}/{dest.name}"

    async def _download(self, url: str, kind: str) -> str:
        import httpx
        ext = ".mp4" if kind == "video" else ".png"
        dest = MEDIA_ROOT / kind / f"{kind}_{int(time.time())}{ext}"
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return f"/media/{kind}/{dest.name}"
