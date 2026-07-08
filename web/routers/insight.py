"""内在世界路由（R9）：情绪/画像/今日事件/记忆/知识图谱/笔记/学习/本能。"""
from __future__ import annotations
from typing import Any

import asyncio
import json
import time
import uuid
from datetime import datetime


def _safe_float(val: Any, default: float = 0.5) -> float:
    """安全转换为 float，失败时返回默认值。"""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user
from web.ws_hub import manager

router = APIRouter(tags=["insight"], dependencies=[Depends(get_current_user)])


def _today_start() -> float:
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


async def _broadcast_kg_change(action: str, target: str, name: str) -> None:
    """广播知识图谱变更事件"""
    try:
        await manager.broadcast({
            "type": "knowledge_graph_changed",
            "action": action,
            "target": target,
            "name": name,
        })
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning(f"insight.kg.broadcast_failed action={action} target={target}: {e}")


# ── 情绪 ─────────────────────────────────────────────────────────


@router.get("/insight/emotion/current", response_model=Envelope[dict])
async def emotion_current(request: Request) -> Any:
    # 优先取网关缓存的最近一次回复情绪（ws_hub 写入）
    cached = getattr(request.app.state, "last_emotion", None)
    if cached:
        return Envelope(data=cached)
    core = request.app.state.core
    row = await core.db.fetch_one(
        "SELECT emotion_label, timestamp FROM conversation_logs "
        "WHERE emotion_label != '' ORDER BY timestamp DESC LIMIT 1")
    return Envelope(data={
        "primary": (row or {}).get("emotion_label") or "中性",
        "timestamp": (row or {}).get("timestamp", 0),
    })


@router.get("/insight/emotion/history", response_model=Envelope[list[dict]])
async def emotion_history(request: Request, days: int = Query(default=7, ge=1, le=30)) -> Any:
    core = request.app.state.core
    since = time.time() - days * 86400
    rows = await core.db.fetch_all(
        "SELECT strftime('%Y-%m-%d %H:00', timestamp, 'unixepoch', 'localtime') AS hour, "
        "emotion_label, COUNT(*) AS cnt FROM conversation_logs "
        "WHERE timestamp > ? AND emotion_label != '' "
        "GROUP BY hour, emotion_label ORDER BY hour", (since,))
    return Envelope(data=rows)


# ── 用户画像 ─────────────────────────────────────────────────────


@router.get("/insight/portrait", response_model=Envelope[dict])
async def get_portrait(request: Request) -> Any:
    core = request.app.state.core
    row = await core.db.fetch_one(
        "SELECT * FROM user_portrait ORDER BY version DESC LIMIT 1")
    history = await core.db.fetch_all(
        "SELECT version, change_log, created_at FROM user_portrait "
        "ORDER BY version DESC LIMIT 10")
    return Envelope(data={"portrait": row or {}, "history": history})


@router.post("/insight/portrait/consolidate", response_model=Envelope[dict])
async def consolidate_portrait(request: Request) -> Any:
    core = request.app.state.core
    if not core.portrait_manager:
        raise HTTPException(503, "画像管理器未初始化")

    async def _run() -> None:
        try:
            result = await core.portrait_manager.consolidate(
                force=True, address_term=core.context.current_address_term)
            from web.ws_hub import manager
            await manager.broadcast({"type": "portrait_consolidated",
                                     "ok": bool(result)})
        except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
            logger.warning("webui.portrait.consolidate_failed error={}", str(e))
            try:
                from web.ws_hub import manager
                await manager.broadcast({"type": "portrait_consolidated",
                                         "ok": False, "error": str(e)[:200]})
            except (ValueError, TypeError, KeyError) as exc:
                logger.debug("insight.portrait_broadcast_failed: {}", exc, exc_info=True)

    request.app.state.portrait_consolidate_task = asyncio.create_task(_run())
    return Envelope(data={"started": True})


# ── 今日事件 ─────────────────────────────────────────────────────


@router.get("/insight/today", response_model=Envelope[dict])
async def today(request: Request) -> Any:
    core = request.app.state.core
    t0 = _today_start()
    items: list[dict] = []
    mems = await core.db.fetch_all(
        "SELECT timestamp AS ts, summary AS text, importance, emotion_label "
        "FROM episodic_memories WHERE timestamp >= ? ORDER BY timestamp", (t0,))
    for m in mems:
        items.append(dict(m, kind="memory"))
    events = await core.db.fetch_all(
        "SELECT created_at AS ts, event_type, detail AS text "
        "FROM agent_events WHERE created_at >= ? ORDER BY created_at", (t0,))
    for e in events:
        items.append(dict(e, kind="event"))
    notes = await core.db.fetch_all(
        "SELECT created_at AS ts, kind AS note_kind, content AS text "
        "FROM notebook_entries WHERE created_at >= ? ORDER BY created_at", (t0,))
    for n in notes:
        items.append(dict(n, kind="note"))
    try:
        greetings = await core.db.fetch_all(
            "SELECT fired_at AS ts, content AS text, reason "
            "FROM greeting_log WHERE fired_at >= ? ORDER BY fired_at", (t0,))
        for g in greetings:
            items.append(dict(g, kind="greeting"))
    except (ValueError, TypeError, KeyError) as exc:
        logger.debug("insight.today_greetings_failed: {}", exc, exc_info=True)
    items.sort(key=lambda x: x.get("ts") or 0)
    conv = await core.db.fetch_one(
        "SELECT COUNT(*) AS c FROM conversation_logs WHERE timestamp >= ?", (t0,))
    tool_calls = await core.db.fetch_one(
        "SELECT COUNT(*) AS c FROM audit_logs WHERE event_type LIKE 'tool%' AND timestamp >= ?", (t0,))
    return Envelope(data={
        "items": items,
        "stats": {
            "conversations": (conv or {}).get("c", 0),
            "tool_calls": (tool_calls or {}).get("c", 0),
            "memories": len(mems),
        },
    })


# ── 记忆 ─────────────────────────────────────────────────────────


@router.get("/insight/memories", response_model=Envelope[list[dict]])
async def list_memories(request: Request,
                        q: str = Query(default=""),
                        importance_min: float = Query(default=0.0, ge=0, le=1),
                        page: int = Query(default=0, ge=0),
                        limit: int = Query(default=30, le=100)) -> Any:
    core = request.app.state.core
    if q.strip() and core.memory:
        try:
            results = await core.memory.retrieve_memories(q.strip(), k=limit)
            return Envelope(data=[
                {"id": r.get("id"), "summary": r.get("summary", ""),
                 "importance": r.get("importance", 0.5),
                 "emotion_label": r.get("emotion_label", ""),
                 "timestamp": r.get("timestamp", 0), "via": "vector"}
                for r in results
                if (r.get("importance") or 0) >= importance_min])
        except (OSError, RuntimeError, ConnectionError, TimeoutError) as e:
            logger.warning("webui.memories.search_failed error={}", str(e))
    rows = await core.db.fetch_all(
        "SELECT id, timestamp, summary, importance, emotion_label "
        "FROM episodic_memories WHERE importance >= ? "
        "ORDER BY timestamp DESC LIMIT ? OFFSET ?",
        (importance_min, limit, page * limit))
    return Envelope(data=[dict(r, via="db") for r in rows])


@router.delete("/insight/memories/{memory_id}", response_model=Envelope[dict])
async def delete_memory(memory_id: int, request: Request) -> Any:
    if request.headers.get("X-Confirm") != "yes":
        raise HTTPException(400, "缺少 X-Confirm: yes 确认头")
    core = request.app.state.core
    vec = getattr(core, "_vec_store", None)
    try:
        await core.db.memory.delete_memory_with_vector(memory_id, vector_store=vec)
        await core.db.insert_audit_log("webui.memory.delete", "webui", str(memory_id))
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.memory.delete_failed memory_id={} error={}", memory_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"deleted": memory_id})


# ── 知识图谱 ─────────────────────────────────────────────────────


@router.get("/insight/knowledge/graph", response_model=Envelope[dict])
async def knowledge_graph(request: Request,
                          entity: str = Query(default=""),
                          depth: int = Query(default=1, ge=1, le=2)) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    async def _node(name: str) -> None:
        if name in nodes:
            return
        ent = await kdb.get_knowledge_entity(name)
        nodes[name] = {"name": name, "kind": (ent or {}).get("kind", "")}

    if entity.strip():
        frontier = [entity.strip()]
        seen_rel: set[str] = set()
        for _ in range(depth):
            next_frontier = []
            for name in frontier:
                await _node(name)
                rels = await kdb.get_knowledge_relations(name)
                for r in rels:
                    rid = r.get("id", f"{r['from_entity']}-{r['to_entity']}")
                    if rid in seen_rel:
                        continue
                    seen_rel.add(rid)
                    await _node(r["from_entity"])
                    await _node(r["to_entity"])
                    edges.append({"from": r["from_entity"], "to": r["to_entity"],
                                  "relation": r.get("relation_type", "")})
                    for other in (r["from_entity"], r["to_entity"]):
                        if other != name:
                            next_frontier.append(other)
            frontier = next_frontier
    else:
        rows = await core.db.fetch_all(
            "SELECT * FROM knowledge_relations ORDER BY rowid DESC LIMIT 80")
        for r in rows:
            await _node(r["from_entity"])
            await _node(r["to_entity"])
            edges.append({"from": r["from_entity"], "to": r["to_entity"],
                          "relation": r.get("relation_type", "")})
    return Envelope(data={"nodes": list(nodes.values()), "edges": edges})


@router.get("/insight/knowledge/entities", response_model=Envelope[list[dict]])
async def list_entities(request: Request, limit: int = Query(default=200, le=500)) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    rows = await kdb.get_all_entities(limit=limit)
    return Envelope(data=rows)


@router.get("/insight/knowledge/relations", response_model=Envelope[list[dict]])
async def list_relations(request: Request, limit: int = Query(default=200, le=500)) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    rows = await kdb.get_all_relations(limit=limit)
    return Envelope(data=rows)


@router.put("/insight/knowledge/relations/{relation_id}", response_model=Envelope[dict])
async def update_relation(relation_id: str, body: dict, request: Request) -> Any:
    core = request.app.state.core
    _kdb = core.db.knowledge
    rel_type = (body.get("relation") or body.get("relation_type") or "").strip()
    if not rel_type:
        raise HTTPException(400, "relation 不能为空")
    try:
        n = await core.db.execute(
            "UPDATE knowledge_relations SET relation_type=?, updated_at=? WHERE id=?",
            (rel_type, time.time(), relation_id))
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("webui.knowledge.update_relation_failed relation_id={} error={}", relation_id, e)
        raise HTTPException(500, "操作失败")
    if not n:
        raise HTTPException(404, f"关系 {relation_id} 不存在")
    try:
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.knowledge.update_relation_commit_failed relation_id={} error={}", relation_id, e)
        raise HTTPException(500, "操作失败")
    await _broadcast_kg_change("update", "relation", relation_id)
    return Envelope(data={"id": relation_id, "updated": True})


# ── 笔记 ─────────────────────────────────────────────────────────


@router.get("/insight/notebook", response_model=Envelope[list[dict]])
async def list_notes(request: Request,
                     kind: str = Query(default=""),
                     limit: int = Query(default=50, le=200)) -> Any:
    core = request.app.state.core
    cond, params = "status != 'archived'", []
    if kind:
        cond += " AND kind=?"
        params.append(kind)
    rows = await core.db.fetch_all(
        f"SELECT * FROM notebook_entries WHERE {cond} "
        f"ORDER BY importance DESC, updated_at DESC LIMIT ?",
        tuple(params) + (limit,))
    return Envelope(data=rows)


@router.post("/insight/notebook", response_model=Envelope[dict])
async def create_note(body: dict, request: Request) -> Any:
    core = request.app.state.core
    content = (body.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    kind = body.get("kind", "note")
    note_id = await core.db.notebook.insert_notebook(
        kind=kind, content=content, tags=body.get("tags", ""),
        importance=float(body.get("importance", 0.5)))
    await core.db.commit()
    return Envelope(data={"id": note_id, "kind": kind})


@router.put("/insight/notebook/{note_id}", response_model=Envelope[dict])
async def update_note(note_id: int, body: dict, request: Request) -> Any:
    core = request.app.state.core
    sets, params = [], []
    for field in ("content", "tags", "kind", "status"):
        if field in body and body[field] is not None:
            sets.append(f"{field}=?")
            params.append(body[field])
    if "importance" in body and body["importance"] is not None:
        sets.append("importance=?")
        params.append(_safe_float(body["importance"]))
    if not sets:
        raise HTTPException(400, "无可更新字段")
    sets.append("updated_at=?")
    params.append(time.time())
    n = await core.db.execute(
        f"UPDATE notebook_entries SET {', '.join(sets)} WHERE id=?",
        tuple(params) + (note_id,))
    if not n:
        raise HTTPException(404, f"笔记 {note_id} 不存在")
    return Envelope(data={"id": note_id, "updated": True})


@router.delete("/insight/notebook/{note_id}", response_model=Envelope[dict])
async def delete_note(note_id: int, request: Request) -> Any:
    core = request.app.state.core
    n = await core.db.execute("DELETE FROM notebook_entries WHERE id=?", (note_id,))
    if not n:
        raise HTTPException(404, f"笔记 {note_id} 不存在")
    await core.db.commit()
    return Envelope(data={"deleted": note_id})


# ── 学习与本能 ───────────────────────────────────────────────────


@router.get("/insight/learnings", response_model=Envelope[list[dict]])
async def list_learnings(request: Request, limit: int = Query(default=50, le=200)) -> Any:
    core = request.app.state.core
    rows = await core.db.fetch_all(
        "SELECT * FROM learnings ORDER BY last_seen DESC LIMIT ?", (limit,))
    return Envelope(data=rows)


@router.get("/insight/instincts", response_model=Envelope[list[dict]])
async def list_instincts(request: Request, limit: int = Query(default=50, le=200)) -> Any:
    core = request.app.state.core
    try:
        rows = await core.db.fetch_all(
            "SELECT * FROM instincts ORDER BY confidence DESC LIMIT ?", (limit,))
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
        logger.debug("insight.instincts_fetch_failed: {}", exc, exc_info=True)
        rows = []
    return Envelope(data=rows)


# ── 记忆增改 ─────────────────────────────────────────────────────


@router.post("/insight/memories", response_model=Envelope[dict])
async def create_memory(body: dict, request: Request) -> Any:
    core = request.app.state.core
    summary = (body.get("summary") or "").strip()
    if not summary:
        raise HTTPException(400, "summary 不能为空")
    importance = _safe_float(body.get("importance", 0.5))
    emotion_label = body.get("emotion_label", "")
    timestamp = _safe_float(body.get("timestamp", time.time()), default=time.time())
    mid = await core.db.insert_episodic_memory(
        summary=summary, importance=importance,
        emotion_label=emotion_label, timestamp=timestamp)
    # 写入向量索引（失败时记录警告，不静默吞掉）
    try:
        if core.memory:
            await core.memory.vec.upsert(mid, summary)
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning(f"insight.create_memory.vec_failed mid={mid}: {e}")
    await core.db.commit()
    return Envelope(data={"id": mid})


@router.put("/insight/memories/{memory_id}", response_model=Envelope[dict])
async def update_memory(memory_id: int, body: dict, request: Request) -> Any:
    core = request.app.state.core
    sets, params = [], []
    if "summary" in body and body["summary"]:  # noqa: RUF019
        sets.append("summary=?")
        params.append(body["summary"])
    if "importance" in body:
        sets.append("importance=?")
        params.append(_safe_float(body["importance"]))
    if "emotion_label" in body:
        sets.append("emotion_label=?")
        params.append(body["emotion_label"])
    if not sets:
        raise HTTPException(400, "无可更新字段")
    n = await core.db.execute(
        f"UPDATE episodic_memories SET {', '.join(sets)} WHERE id=?",
        tuple(params) + (memory_id,))
    if not n:
        raise HTTPException(404, f"记忆 {memory_id} 不存在")
    # 同步更新向量索引
    if "summary" in body and body["summary"]:  # noqa: RUF019
        try:
            if core.memory:
                await core.memory.vec.upsert(memory_id, body["summary"])
        except (OSError, KeyError, ValueError, RuntimeError, TypeError) as exc:
            logger.debug("insight.memory_vec_upsert_failed: {}", exc, exc_info=True)
    await core.db.commit()
    return Envelope(data={"id": memory_id, "updated": True})


# ── 学习记录增删改 ────────────────────────────────────────────────


@router.post("/insight/learnings", response_model=Envelope[dict])
async def create_learning(body: dict, request: Request) -> Any:
    core = request.app.state.core
    summary = (body.get("summary") or "").strip()
    if not summary:
        raise HTTPException(400, "summary 不能为空")
    pattern = body.get("pattern", "")
    priority = body.get("priority", "medium")
    category = body.get("category", "insight")
    now = time.time()
    learning_id = f"LRN-{uuid.uuid4().hex[:12]}"
    try:
        lid = await core.db.execute(
            "INSERT INTO learnings (learning_id, category, priority, summary, pattern_key, "
            "recurrence_count, first_seen, last_seen, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (learning_id, category, priority, summary, pattern, now, now, now))
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.learning.create_failed learning_id={} error={}", learning_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"id": lid})


@router.put("/insight/learnings/{learning_id}", response_model=Envelope[dict])
async def update_learning(learning_id: int, body: dict, request: Request) -> Any:
    core = request.app.state.core
    sets, params = [], []
    field_map = {"summary": "summary", "pattern": "pattern_key", "priority": "priority", "status": "status"}
    for api_field, db_field in field_map.items():
        if api_field in body and body[api_field] is not None:
            sets.append(f"{db_field}=?")
            params.append(body[api_field])
    if not sets:
        raise HTTPException(400, "无可更新字段")
    try:
        n = await core.db.execute(
            f"UPDATE learnings SET {', '.join(sets)} WHERE id=?",
            tuple(params) + (learning_id,))
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("webui.learning.update_failed learning_id={} error={}", learning_id, e)
        raise HTTPException(500, "操作失败")
    if not n:
        raise HTTPException(404, f"学习记录 {learning_id} 不存在")
    try:
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.learning.update_commit_failed learning_id={} error={}", learning_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"id": learning_id, "updated": True})


@router.delete("/insight/learnings/{learning_id}", response_model=Envelope[dict])
async def delete_learning(learning_id: int, request: Request) -> Any:
    core = request.app.state.core
    try:
        n = await core.db.execute("DELETE FROM learnings WHERE id=?", (learning_id,))
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.learning.delete_failed learning_id={} error={}", learning_id, e)
        raise HTTPException(500, "操作失败")
    if not n:
        raise HTTPException(404, f"学习记录 {learning_id} 不存在")
    try:
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.learning.delete_commit_failed learning_id={} error={}", learning_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"deleted": learning_id})


# ── 本能增删改 ────────────────────────────────────────────────────


@router.post("/insight/instincts", response_model=Envelope[dict])
async def create_instinct(body: dict, request: Request) -> Any:
    core = request.app.state.core
    content = (body.get("content") or body.get("summary") or "").strip()
    if not content:
        raise HTTPException(400, "content 不能为空")
    confidence = _safe_float(body.get("confidence", 0.5))
    now = time.time()
    try:
        iid = await core.db.execute(
            "INSERT INTO instincts (content, confidence, created_at, last_used_at, use_count) "
            "VALUES (?, ?, ?, ?, 0)",
            (content, confidence, now, now))
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.instinct.create_failed content={} error={}", content, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"id": iid})


@router.put("/insight/instincts/{instinct_id}", response_model=Envelope[dict])
async def update_instinct(instinct_id: int, body: dict, request: Request) -> Any:
    core = request.app.state.core
    sets, params = [], []
    for field in ("content", "status"):
        if field in body and body[field] is not None:
            sets.append(f"{field}=?")
            params.append(body[field])
    if "confidence" in body and body["confidence"] is not None:
        sets.append("confidence=?")
        params.append(_safe_float(body["confidence"]))
    if not sets:
        raise HTTPException(400, "无可更新字段")
    sets.append("last_used_at=?")
    params.append(time.time())
    try:
        n = await core.db.execute(
            f"UPDATE instincts SET {', '.join(sets)} WHERE id=?",
            tuple(params) + (instinct_id,))
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning("webui.instinct.update_failed instinct_id={} error={}", instinct_id, e)
        raise HTTPException(500, "操作失败")
    if not n:
        raise HTTPException(404, f"本能 {instinct_id} 不存在")
    try:
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.instinct.update_commit_failed instinct_id={} error={}", instinct_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"id": instinct_id, "updated": True})


@router.delete("/insight/instincts/{instinct_id}", response_model=Envelope[dict])
async def delete_instinct(instinct_id: int, request: Request) -> Any:
    core = request.app.state.core
    try:
        n = await core.db.execute("DELETE FROM instincts WHERE id=?", (instinct_id,))
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.instinct.delete_failed instinct_id={} error={}", instinct_id, e)
        raise HTTPException(500, "操作失败")
    if not n:
        raise HTTPException(404, f"本能 {instinct_id} 不存在")
    try:
        await core.db.commit()
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.instinct.delete_commit_failed instinct_id={} error={}", instinct_id, e)
        raise HTTPException(500, "操作失败")
    return Envelope(data={"deleted": instinct_id})


# ── 知识图谱实体和关系增删 ─────────────────────────────────────────


@router.post("/insight/knowledge/entities", response_model=Envelope[dict])
async def create_entity(body: dict, request: Request) -> Any:
    core = request.app.state.core
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name 不能为空")
    kdb = core.db.knowledge
    # observations 统一转为 list，避免字符串被拆成字符数组
    raw_obs = body.get("observations", "")
    if isinstance(raw_obs, str):
        observations = [raw_obs] if raw_obs.strip() else []
    elif isinstance(raw_obs, list):
        observations = raw_obs
    else:
        observations = []
    await kdb.upsert_knowledge_entity(
        name=name,
        kind=body.get("kind", ""),
        observations=observations)
    await core.db.commit()
    await _broadcast_kg_change("create", "entity", name)
    return Envelope(data={"name": name})


@router.put("/insight/knowledge/entities/{name}", response_model=Envelope[dict])
async def update_entity(name: str, body: dict, request: Request) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    existing = await kdb.get_knowledge_entity(name)
    if not existing:
        raise HTTPException(404, f"实体 {name} 不存在")
    new_kind = body.get("kind", existing.get("kind", ""))
    new_obs = body.get("observations", existing.get("observations", []))
    # observations 统一转为 list
    if isinstance(new_obs, str):
        new_obs = [new_obs] if new_obs.strip() else []
    elif isinstance(new_obs, list):
        pass
    else:
        # existing 中可能是 JSON 字符串
        if isinstance(new_obs, str):
            try:
                new_obs = json.loads(new_obs)
            except (json.JSONDecodeError, TypeError):
                new_obs = []
        else:
            new_obs = []
    await kdb.update_knowledge_entity(name, new_kind, new_obs)
    await _broadcast_kg_change("update", "entity", name)
    return Envelope(data={"name": name, "updated": True})


@router.delete("/insight/knowledge/entities/{name}", response_model=Envelope[dict])
async def delete_entity(name: str, request: Request) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    deleted = await kdb.delete_knowledge_entity(name)
    if not deleted:
        raise HTTPException(404, f"实体 {name} 不存在")
    await _broadcast_kg_change("delete", "entity", name)
    return Envelope(data={"deleted": name})


@router.post("/insight/knowledge/relations", response_model=Envelope[dict])
async def create_relation(body: dict, request: Request) -> Any:
    core = request.app.state.core
    from_e = (body.get("from") or "").strip()
    to_e = (body.get("to") or "").strip()
    rel = (body.get("relation") or "").strip()
    if not from_e or not to_e or not rel:
        raise HTTPException(400, "from/to/relation 不能为空")
    relation_id = f"REL-{uuid.uuid4().hex[:12]}"
    kdb = core.db.knowledge
    try:
        await kdb.insert_knowledge_relation(relation_id, from_e, rel, to_e)
    except (OSError, KeyError, ValueError, RuntimeError) as e:
        logger.warning("webui.knowledge.create_relation_failed from={} to={} rel={} error={}", from_e, to_e, rel, e)
        raise HTTPException(500, "操作失败")
    await _broadcast_kg_change("create", "relation", relation_id)
    return Envelope(data={"id": relation_id, "from": from_e, "to": to_e, "relation": rel})


@router.delete("/insight/knowledge/relations/{relation_id}", response_model=Envelope[dict])
async def delete_relation(relation_id: str, request: Request) -> Any:
    core = request.app.state.core
    kdb = core.db.knowledge
    deleted = await kdb.delete_knowledge_relation(relation_id)
    if not deleted:
        raise HTTPException(404, f"关系 {relation_id} 不存在")
    await _broadcast_kg_change("delete", "relation", relation_id)
    return Envelope(data={"deleted": relation_id})


# ── XP 亲密度 ───────────────────────────────────────────────────

_GUIDANCE_TEMPLATES = {
    "polite": "标准化礼貌回复",
    "warm": "温暖友好交流、可提及近期话题",
    "intimate": "可主动提及过往话题、使用昵称、深度情感陪伴",
    "deep_intimate": "完全个性化、深度亲密交流、可分享秘密",
    "soulmate": "最高人格自由度、深度情感共鸣、灵魂伴侣级交流",
}


def _build_guidance(config: dict) -> str:
    tone = config.get("tone", "")
    if tone in _GUIDANCE_TEMPLATES:
        return _GUIDANCE_TEMPLATES[tone]
    parts: list[str] = []
    if config.get("can_mention_past"):
        parts.append("可主动提及过往话题")
    if config.get("can_use_nickname"):
        parts.append("使用昵称")
    if config.get("can_share_secrets"):
        parts.append("可分享秘密")
    return "、".join(parts) if parts else "标准交流"


@router.get("/insight/xp", response_model=Envelope[dict])
async def get_xp(request: Request, user_id: str = Depends(get_current_user)) -> Any:
    from core.xp_system import get_xp_system, XPLevel, XP_THRESHOLDS, _LEVEL_LABELS

    try:
        xp_sys = get_xp_system()
        state = xp_sys.get_state(user_id)

        level = state.level
        current_threshold = XP_THRESHOLDS.get(level, 0)
        next_level = XPLevel(int(level) + 1) if int(level) < 6 else None
        next_level_xp = XP_THRESHOLDS.get(next_level, current_threshold) if next_level else state.xp

        level_range = next_level_xp - current_threshold
        progress = min((state.xp - current_threshold) / level_range, 1.0) if level_range > 0 else 1.0

        config_raw = xp_sys.get_intimacy_config(level)
        level_config = {
            "label": config_raw.get("label", _LEVEL_LABELS.get(level, "未知")),
            "tone": config_raw.get("tone", ""),
            "proactivity": config_raw.get("initiative", 0),
            "emotional_richness": config_raw.get("emotion_richness", 0),
            "guidance": _build_guidance(config_raw),
        }

        history = [h.to_dict() for h in state.history[-50:]]
        history.reverse()

        return Envelope(data={
            "user_id": state.user_id,
            "xp": state.xp,
            "level": int(state.level),
            "level_label": _LEVEL_LABELS.get(level, "未知"),
            "next_level_xp": next_level_xp,
            "progress": round(progress, 3),
            "history": history,
            "milestones": state.milestones,
            "first_seen_at": state.first_seen_at,
            "last_chat_at": state.last_chat_at,
            "level_config": level_config,
        })
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning(f"insight.xp.get_failed user={user_id}: {e}")
        raise HTTPException(500, "获取 XP 状态失败")


@router.get("/insight/xp/levels", response_model=Envelope[dict])
async def get_xp_levels(request: Request) -> Any:
    from core.xp_system import XPLevel, XP_THRESHOLDS, _LEVEL_LABELS

    try:
        from core.xp_system import get_xp_system
        xp_sys = get_xp_system()
        levels = []
        for lv in sorted(XPLevel, key=lambda x: int(x)):
            config_raw = xp_sys.get_intimacy_config(lv)
            levels.append({
                "level": int(lv),
                "threshold": XP_THRESHOLDS.get(lv, 0),
                "label": config_raw.get("label", _LEVEL_LABELS.get(lv, "未知")),
                "tone": config_raw.get("tone", ""),
                "proactivity": config_raw.get("initiative", 0),
                "emotional_richness": config_raw.get("emotion_richness", 0),
                "guidance": _build_guidance(config_raw),
            })
        return Envelope(data={"levels": levels})
    except (OSError, KeyError, ValueError, RuntimeError, TypeError) as e:
        logger.warning(f"insight.xp.levels_failed: {e}")
        raise HTTPException(500, "获取 XP 等级配置失败")