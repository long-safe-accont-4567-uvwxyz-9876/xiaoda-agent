"""定时与问候路由（R10）：问候计划 CRUD、DND、立即试发、历史。"""
from __future__ import annotations
from typing import Any

import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from loguru import logger

from web.schemas import Envelope
from web.routers.auth import get_current_user

router = APIRouter(tags=["schedule"], dependencies=[Depends(get_current_user)])

_HM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _cfg() -> Any:
    from web.config_service import get_config_service
    return get_config_service()


def _scheduler(request: Request) -> Any:
    sched = getattr(request.app.state, "greeting_scheduler", None)
    if not sched:
        raise HTTPException(503, "问候调度器未启动")
    return sched


def _check_hm(value: str, field: str) -> None:
    if not _HM.match(value or ""):
        raise HTTPException(400, f"{field} 必须是 HH:MM 格式")


async def _audit(request: Request, action: str, detail: str) -> None:
    core = request.app.state.core
    try:
        await core.db.insert_audit_log(f"webui.schedule.{action}", "webui", detail)
        await core.db.commit()
    except Exception:
        logger.debug("schedule.audit_failed", exc_info=True)


@router.get("/schedule/config", response_model=Envelope[dict])
async def get_config() -> Any:
    cfg = _cfg()
    return Envelope(data={
        "enabled": cfg.get("schedule.enabled", True),
        "greeting_max_per_day": cfg.get("schedule.greeting_max_per_day", 3),
        "dnd_periods": cfg.get("schedule.dnd_periods", []),
    })


@router.put("/schedule/config", response_model=Envelope[dict])
async def put_config(body: dict, request: Request) -> Any:
    cfg = _cfg()
    if "enabled" in body:
        cfg.set("schedule.enabled", bool(body["enabled"]))
    if "greeting_max_per_day" in body and body["greeting_max_per_day"] is not None:
        cfg.set("schedule.greeting_max_per_day",
                max(0, min(int(body["greeting_max_per_day"]), 20)))
    await _audit(request, "config", json.dumps(body, ensure_ascii=False))
    return Envelope(data={
        "enabled": cfg.get("schedule.enabled"),
        "greeting_max_per_day": cfg.get("schedule.greeting_max_per_day"),
    })


@router.get("/schedule/dnd", response_model=Envelope[list[dict]])
async def get_dnd() -> Any:
    return Envelope(data=_cfg().get("schedule.dnd_periods", []))


@router.put("/schedule/dnd", response_model=Envelope[list[dict]])
async def put_dnd(body: dict, request: Request) -> Any:
    periods = body.get("periods")
    if not isinstance(periods, list):
        raise HTTPException(400, "periods 必须是数组")
    for p in periods:
        _check_hm(p.get("start", ""), "start")
        _check_hm(p.get("end", ""), "end")
    cleaned = [{"start": p["start"], "end": p["end"]} for p in periods]
    _cfg().set("schedule.dnd_periods", cleaned)
    await _audit(request, "dnd", json.dumps(cleaned, ensure_ascii=False))
    return Envelope(data=cleaned)


# ── 问候计划 CRUD ────────────────────────────────────────────────


def _validate_schedule(body: dict) -> dict:
    stype = body.get("type")
    if stype not in ("fixed", "random", "reminder"):
        raise HTTPException(400, "type 必须是 fixed、random 或 reminder")
    days = body.get("days") or [1, 2, 3, 4, 5, 6, 7]
    if not isinstance(days, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in days):
        raise HTTPException(400, "days 必须是 1~7 的整数数组")
    channels = body.get("channels") or ["web"]
    if not all(c in ("web", "qq") for c in channels):
        raise HTTPException(400, "channels 仅支持 web/qq")
    rec = {
        "type": stype,
        "days": json.dumps(sorted(set(days))),
        "prompt_hint": (body.get("prompt_hint") or "")[:200],
        "channels": json.dumps(channels),
        "enabled": 1 if body.get("enabled", True) else 0,
        "time": None, "window_start": None, "window_end": None, "count_per_day": None,
    }
    if stype == "fixed":
        _check_hm(body.get("time", ""), "time")
        rec["time"] = body["time"]
    elif stype == "reminder":
        # reminder：需要 time + prompt_hint，无需 window
        _check_hm(body.get("time", ""), "time")
        rec["time"] = body["time"]
        if not body.get("prompt_hint"):
            raise HTTPException(400, "reminder 类型必须提供 prompt_hint（提醒内容）")
    else:
        _check_hm(body.get("window_start", ""), "window_start")
        _check_hm(body.get("window_end", ""), "window_end")
        rec["window_start"] = body["window_start"]
        rec["window_end"] = body["window_end"]
        rec["count_per_day"] = max(1, min(int(body.get("count_per_day", 1)), 10))
    return rec


@router.get("/schedule/greetings", response_model=Envelope[list[dict]])
async def list_greetings(request: Request) -> Any:
    core = request.app.state.core
    rows = await core.db.fetch_all(
        "SELECT * FROM greeting_schedules ORDER BY id")
    return Envelope(data=rows)


@router.post("/schedule/greetings", response_model=Envelope[dict])
async def create_greeting(body: dict, request: Request) -> Any:
    core = request.app.state.core
    rec = _validate_schedule(body)
    # 修复 P1: 设置 user_id 实现按用户隔离 (web UI 用户统一为 'webui').
    # 兼容旧库: 若 greeting_schedules 表无 user_id 列 (尚未迁移 v20),
    # INSERT 会报错, 此时降级为不传 user_id (由 column DEFAULT 处理).
    try:
        await core.db.execute(
            "INSERT INTO greeting_schedules"
            "(type, time, window_start, window_end, count_per_day, days, "
            " prompt_hint, channels, enabled, next_fire_times, created_at, user_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,'[]',?,?)",
            (rec["type"], rec["time"], rec["window_start"], rec["window_end"],
             rec["count_per_day"], rec["days"], rec["prompt_hint"],
             rec["channels"], rec["enabled"], time.time(), "webui"))
    except Exception as _e:
        # 旧库无 user_id 列, 降级为原 INSERT
        logger.debug("schedule.create_greeting_fallback_no_user_id error={}", str(_e))
        await core.db.execute(
            "INSERT INTO greeting_schedules"
            "(type, time, window_start, window_end, count_per_day, days, "
            " prompt_hint, channels, enabled, next_fire_times, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'[]',?)",
            (rec["type"], rec["time"], rec["window_start"], rec["window_end"],
             rec["count_per_day"], rec["days"], rec["prompt_hint"],
             rec["channels"], rec["enabled"], time.time()))
    row = await core.db.fetch_one(
        "SELECT * FROM greeting_schedules ORDER BY id DESC LIMIT 1")
    await _audit(request, "greeting.create", json.dumps(body, ensure_ascii=False))
    return Envelope(data=row or {})


@router.put("/schedule/greetings/{sid}", response_model=Envelope[dict])
async def update_greeting(sid: int, body: dict, request: Request) -> Any:
    core = request.app.state.core
    existing = await core.db.fetch_one(
        "SELECT * FROM greeting_schedules WHERE id=?", (sid,))
    if not existing:
        raise HTTPException(404, f"计划 {sid} 不存在")
    # 仅启停的快捷路径
    if set(body.keys()) == {"enabled"}:
        await core.db.execute(
            "UPDATE greeting_schedules SET enabled=? WHERE id=?",
            (1 if body["enabled"] else 0, sid))
    else:
        merged = dict(existing)
        merged.update({k: v for k, v in body.items() if v is not None})
        merged["days"] = body.get("days") or json.loads(existing["days"])
        merged["channels"] = body.get("channels") or json.loads(existing["channels"])
        rec = _validate_schedule(merged)
        await core.db.execute(
            "UPDATE greeting_schedules SET type=?, time=?, window_start=?, "
            "window_end=?, count_per_day=?, days=?, prompt_hint=?, channels=?, "
            "enabled=?, next_fire_times='[]' WHERE id=?",
            (rec["type"], rec["time"], rec["window_start"], rec["window_end"],
             rec["count_per_day"], rec["days"], rec["prompt_hint"],
             rec["channels"], rec["enabled"], sid))
    row = await core.db.fetch_one(
        "SELECT * FROM greeting_schedules WHERE id=?", (sid,))
    await _audit(request, "greeting.update", str(sid))
    return Envelope(data=row or {})


@router.delete("/schedule/greetings/{sid}", response_model=Envelope[dict])
async def delete_greeting(sid: int, request: Request) -> Any:
    core = request.app.state.core
    n = await core.db.execute(
        "DELETE FROM greeting_schedules WHERE id=?", (sid,))
    if not n:
        raise HTTPException(404, f"计划 {sid} 不存在")
    await _audit(request, "greeting.delete", str(sid))
    return Envelope(data={"deleted": sid})


@router.post("/schedule/test-greeting", response_model=Envelope[dict])
async def test_greeting(body: dict, request: Request) -> Any:
    """立即生成并推送一条问候（验证链路；尊重 DND）。"""
    sched = _scheduler(request)
    if sched.is_dnd():
        return Envelope(data={"sent": False, "reason": "dnd",
                              "message": "当前处于免打扰时段，问候被拦截（这正是 DND 生效的证明）"})
    schedule = {"id": 0, "prompt_hint": body.get("prompt_hint", ""),
                "channels": json.dumps(body.get("channels") or ["web"])}
    text, report = await sched.fire_with_report(schedule, reason="manual_test")
    await _audit(request, "test_greeting", text[:80])
    return Envelope(data={"sent": any(r["ok"] for r in report.values()),
                          "text": text, "channels": report})


@router.get("/schedule/history", response_model=Envelope[list[dict]])
async def greeting_history(request: Request, days: int = Query(default=7, ge=1, le=90)) -> Any:
    core = request.app.state.core
    since = time.time() - days * 86400
    rows = await core.db.fetch_all(
        "SELECT * FROM greeting_log WHERE fired_at > ? ORDER BY fired_at DESC LIMIT 200",
        (since,))
    return Envelope(data=rows)
