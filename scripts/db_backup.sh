#!/usr/bin/env bash
# nahida-db-backup —— 生产 SQLite 库热备份（WAL 安全）+ 完整性校验 + 轮转
#
# 由 deploy/systemd/nahida-db-backup.timer 每日 04:30 触发（Persistent=true
# 补跑错过的窗口）。使用 sqlite3 backup API（在线一致性快照），不锁写、
# 不依赖服务停机。备份保留 KEEP_DAYS 天后清理。
#
# 退出码：0=全部成功且校验通过；1=任一库校验失败。
set -euo pipefail

SRC_DIR="/mnt/usb2/nahida-data/db"
DST_ROOT="/mnt/usb2/nahida-data/backups"
KEEP_DAYS=7
TS="$(date +%Y%m%d_%H%M%S)"
DST_DIR="${DST_ROOT}/${TS}"

mkdir -p "$DST_DIR"

for db in agent.db agent_vec.db; do
    echo "[backup] ${SRC_DIR}/${db} → ${DST_DIR}/${db}"
    sqlite3 "file:${SRC_DIR}/${db}?mode=ro" \
        ".backup '${DST_DIR}/${db}'"
done

fail=0
total=0
for f in "${DST_DIR}/agent.db" "${DST_DIR}/agent_vec.db"; do
    r="$(sqlite3 "file:${f}?mode=ro&immutable=1" 'PRAGMA integrity_check' 2>/dev/null || echo FAIL)"
    sz="$(du -h "$f" | cut -f1)"
    total=$(( total + $(stat -c%s "$f") ))
    if [ "$r" = "ok" ]; then
        echo "[verify] ok  ${f} (${sz})"
    else
        echo "[verify] FAIL ${f} -> ${r}"
        fail=1
    fi
done

# 轮转：删除超期备份目录
find "$DST_ROOT" -maxdepth 1 -type d -name '20*' -mtime +"${KEEP_DAYS}" -exec rm -rf {} \; 2>/dev/null || true

rm -f "${DST_DIR}"/*-shm "${DST_DIR}"/*-wal
echo "[done] 本份合计 $((total / 1024 / 1024))MB"
exit $fail
