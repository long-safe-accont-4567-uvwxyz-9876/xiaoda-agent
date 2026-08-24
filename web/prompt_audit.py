"""Prompt 治理审计日志：ab-run 报告摘要与 stage/promote/rollback 事件留痕。

JSONL 追加写，单条记录自包含（时间/prompt/版本/门禁结论/回归清单），
满足文档 §6.4「按 prompt version 分组统计」与 §8.2「每次调用记录
prompt_id/version/template_hash/backend/degraded」的可追溯要求。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

_MAX_RECORDS = 500


def _default_log_path() -> Path:
    from config_paths import LOG_DIR

    return Path(LOG_DIR) / "prompt_audit.jsonl"


class PromptAuditLog:
    def __init__(self, path: Path | None = None) -> None:
        self._path = path or _default_log_path()

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        record = {"ts": time.time(), **event}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        self._truncate()
        return record

    def recent(self, limit: int = 50, prompt_id: str | None = None) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines[-_MAX_RECORDS:]:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if prompt_id and record.get("prompt_id") != prompt_id:
                continue
            records.append(record)
        return records[-limit:]

    def _truncate(self) -> None:
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        if len(lines) > _MAX_RECORDS:
            kept = lines[-_MAX_RECORDS:]
            tmp = self._path.with_suffix(".tmp")
            try:
                tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
                tmp.replace(self._path)
            except OSError:
                pass


_module_log: PromptAuditLog | None = None


def get_prompt_audit() -> PromptAuditLog:
    global _module_log
    if _module_log is None:
        _module_log = PromptAuditLog()
    return _module_log
