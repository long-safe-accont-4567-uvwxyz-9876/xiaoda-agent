#!/usr/bin/env python3
"""workflow_v2 M3 迁移/回滚/灰度 CLI（立项书 §6）。

把 WORKSPACE_DIR/workflows/*.json（v1 定义）幂等迁移为 v2 revision：
同 content_hash 的 revision 已存在则跳过（不打扰 WebUI 人工回滚）；
定义行缺失自动创建；新迁移的首版置为 current。

子命令/开关：
    （无）                —— 全量迁移（只处理白名单里缺失定义的也可加 --wf 限定）
    --dry-run             —— 只预演：输出差异报告，不写一行库
    --check               —— 双引擎一致性抽查：v1 → v2 映射 + 图校验，只读
    --status              —— 打印灰度开关状态 + 各工作流可用性
    --rollback WF REV     —— 把 WF 的 current 切回 REV（必须属于该工作流）
    --global-on / --global-off   —— 全局灰度开关
    --pilot WF [WF ...]   —— 把工作流加入试点白名单
    --unpilot WF [...]    —— 移出白名单
    --wf <id>             —— 只处理指定工作流（默认全部）
    --json                —— 机器可读输出（JSON）
    --db PATH             —— 覆盖 DB 路径（默认 DATA_DIR/agent.db 动态解析）

退出码：0 成功；1 存在失败项（invalid/rollback 失败）；2 用法错误。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import aiosqlite  # noqa: E402 —— sys.path bootstrap 在项目导入之前

from db.db_workflow import create_schema  # noqa: E402
from workflow_v2.repository import WorkflowRepository  # noqa: E402
from workflow_v2.service import WorkflowV2Service  # noqa: E402

KEY_ENABLED = "workflow_v2.enabled"
KEY_PILOT = "workflow_v2.pilot_wf_ids"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="把 v1 工作流幂等迁移进 v2 revision，并管理灰度开关（试点白名单）"
    )
    p.add_argument("--dry-run", action="store_true", help="只输出差异报告，不写库")
    p.add_argument("--check-sync", dest="check_sync", action="store_true",
                   help="只读一致性抽查：每个 v1 文件的映射/图校验")
    p.add_argument("--status", action="store_true", help="打印灰度开关与各工作流可用性")
    p.add_argument("--rollback", nargs=2, metavar=("WF_ID", "REVISION"),
                   help="把指定工作流 current 回退到 REVISION")
    p.add_argument("--global-on", action="store_true", help="打开全局灰度开关")
    p.add_argument("--global-off", action="store_true", help="关闭全局灰度开关")
    p.add_argument("--pilot", nargs="+", metavar="WF", help="把工作流加入试点白名单")
    p.add_argument("--unpilot", nargs="+", metavar="WF", help="把工作流移出白名单")
    p.add_argument("--wf", nargs="+", metavar="WF", help="只处理这些工作流（默认全部）")
    p.add_argument("--json", action="store_true", help="JSON 机器可读输出（含 --status）")
    p.add_argument("--db", default=None, help="覆盖 DB 路径（默认动态解析 DATA_DIR/agent.db）")
    return p.parse_args(argv)


def _db_path(override: str | None) -> Path:
    if override:
        return Path(override).expanduser()
    from config import DATA_DIR
    return Path(DATA_DIR) / "agent.db"


async def _open_svc(db_path: str | Path) -> tuple[aiosqlite.Connection, WorkflowV2Service]:
    """打开 DB 并建幂等 wf 表（CLI 不依赖主程序迁移进度）。"""
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await create_schema(conn)
    return conn, WorkflowV2Service(WorkflowRepository(conn))


def _v1_files(workspace: str | Path, only: list[str] | None) -> list[Path]:
    base = Path(workspace) / "workflows"
    if only:
        return [base / f"{wf}.json" for wf in only]
    return sorted(base.glob("*.json")) if base.is_dir() else []


def _emit(json_out: bool, payload: dict) -> None:
    """统一报告输出：--json 走 stdout JSON，否则人类可读逐行。"""
    if json_out:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    label = "预演" if payload.get("action") == "dry-run" else "迁移"
    for r in payload["reports"]:
        act = r.get("action")
        if act == "invalid":
            print(f"❌ {r['wf_id']}: 图校验失败 {r.get('error', '')}")
        elif act == "unchanged":
            print(f"⏭  {r['wf_id']}: 内容未变，跳过（current 保持人工选择）")
        else:
            note = "（预演，未写库）" if r.get("dry_run") else ""
            print(f"✅ {r['wf_id']}: 已固化新版本 {note}")
        for w in r.get("warnings", []):
            print(f"   ⚠ {w.get('warning', w)}")
    if not payload["reports"]:
        print(f"（{label}范围内没有可处理的 v1 工作流）")


async def cmd_migrate(svc: WorkflowV2Service, files: list[Path],
                      dry_run: bool, json_out: bool) -> int:
    reports = []
    for fp in files:
        wf_id = fp.stem
        report = await svc.migrate_workflow(wf_id, set_current=True, dry_run=dry_run)
        if report is None:
            continue
        reports.append(report)
    _emit(json_out, {"action": "dry-run" if dry_run else "migrate", "reports": reports})
    bad = [r for r in reports if r.get("action") == "invalid"]
    return 1 if bad else 0


async def cmd_status(svc: WorkflowV2Service, files: list[Path], json_out: bool) -> int:
    global_on = await svc.v2_global_enabled()
    pilot = await svc.v2_pilot_ids()
    rows = []
    for fp in files:
        wf_id = fp.stem
        rows.append({"wf_id": wf_id, "enabled": global_on or wf_id in pilot})
    if json_out:
        print(json.dumps({"global_enabled": global_on, "pilot_wf_ids": pilot,
                          "workflows": rows}, ensure_ascii=False, indent=2))
    else:
        print(f"全局灰度: {'✅ 开' if global_on else '❌ 关（默认）'}")
        print(f"试点白名单: {pilot if pilot else '（空）'}")
        for r in rows:
            print(f"  {'✅' if r['enabled'] else '⛔'} {r['wf_id']}")
    return 0


async def cmd_check_sync(svc: WorkflowV2Service, files: list[Path], json_out: bool) -> int:
    """只读一致性抽查：v1 → v2 映射 + 图校验，不落库。"""
    reports = []
    for fp in files:
        try:
            v1 = json.loads(fp.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            reports.append({"wf_id": fp.stem, "action": "invalid", "error": "v1 JSON 不可读"})
            continue
        from workflow_v2.graph import GraphError, validate_graph
        from workflow_v2.migrate import migrate_v1
        rev, warnings = migrate_v1(v1)
        mapping = [{"v1": n.id, "v2_type": n.type.value} for n in rev.nodes
                   if n.id not in ("__start__", "__end__")]
        try:
            validate_graph(rev.nodes, rev.edges)
        except GraphError as e:
            reports.append({"wf_id": fp.stem, "action": "invalid", "error": str(e)})
            continue
        reports.append({"wf_id": fp.stem, "action": "ok", "nodes": len(rev.nodes),
                        "edges": len(rev.edges), "warnings": warnings,
                        "mapping": mapping})
    if json_out:
        print(json.dumps({"check": reports}, ensure_ascii=False, indent=2))
    else:
        for r in reports:
            mark = "✅" if r["action"] == "ok" else "❌"
            extra = f"{r.get('mapping', [])} warnings={len(r.get('warnings', []))}" if r["action"] == "ok" else r.get("error", "")
            print(f"{mark} {r['wf_id']}: {extra}")
    bad = [r for r in reports if r["action"] == "invalid"]
    return 1 if bad else 0


async def cmd_rollback(svc: WorkflowV2Service, wf: str, rev: str, json_out: bool) -> int:
    definition = await svc.get_definition(wf)
    if definition is None:
        _err_out(json_out, "WORKFLOW_NOT_FOUND", f"定义不存在: {wf}")
        return 1
    updated = await svc.set_revision_current(wf, rev, etag=definition["etag"])
    if updated is None:
        if not await svc.revision_exists(wf, rev):
            _err_out(json_out, "REVISION_NOT_FOUND", f"revision 不存在或不属于该工作流: {rev}")
        else:
            _err_out(json_out, "ETAG_CONFLICT", "定义被其他客户端修改（CAS 失败），请重试")
        return 1
    if json_out:
        print(json.dumps({"ok": True, "workflow": wf, "current_revision": rev}, ensure_ascii=False))
    else:
        print(f"✅ {wf} 已回滚到 {rev}（etag 已翻转）")
    return 0


async def cmd_switch(svc: WorkflowV2Service, args: argparse.Namespace, json_out: bool) -> int:
    if args.global_on:
        await svc.set_config(KEY_ENABLED, True)
    elif args.global_off:
        await svc.set_config(KEY_ENABLED, False)
    if args.pilot or args.unpilot:
        pilot = await svc.v2_pilot_ids()
        for wf in args.pilot or []:
            if wf not in pilot:
                pilot.append(wf)
        for wf in args.unpilot or []:
            if wf in pilot:
                pilot.remove(wf)
        await svc.set_config(KEY_PILOT, pilot)
    global_on = await svc.v2_global_enabled()
    pilot = await svc.v2_pilot_ids()
    if json_out:
        print(json.dumps({"global_enabled": global_on, "pilot_wf_ids": pilot}, ensure_ascii=False))
    else:
        print(f"全局: {'✅ 开' if global_on else '❌ 关'}  白名单: {pilot}")
    return 0


def _err_out(json_out: bool, code: str, message: str) -> None:
    if json_out:
        print(json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False))
    else:
        print(f"❌ [{code}] {message}", file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    from config import WORKSPACE_DIR
    db_path = _db_path(args.db)
    conn, svc = await _open_svc(db_path)
    try:
        files = _v1_files(WORKSPACE_DIR, args.wf)
        if args.rollback:
            return await cmd_rollback(svc, args.rollback[0], args.rollback[1], args.json)
        # 轻量开关管理不需要工作区文件
        if args.global_on or args.global_off or args.pilot or args.unpilot:
            return await cmd_switch(svc, args, args.json)
        if args.status:
            return await cmd_status(svc, files, args.json)
        if args.check_sync:
            return await cmd_check_sync(svc, files, args.json)
        return await cmd_migrate(svc, files, args.dry_run, args.json)
    finally:
        await conn.close()


def main() -> int:
    args = _parse_args(sys.argv[1:])
    if args.rollback and len(args.rollback) != 2:
        print("用法错误: --rollback <wf_id> <revision_id>", file=sys.stderr)
        return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
