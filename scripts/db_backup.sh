#!/usr/bin/env bash
# nahida-db-backup —— 生产 SQLite 库热备份（WAL 安全）+ 完整性校验 + 轮转
#
# 由 deploy/systemd/nahida-db-backup.timer 每日 04:30 触发（Persistent=true
# 补跑错过的窗口）。使用 sqlite3 backup API（在线一致性快照），不锁写、
# 不依赖服务停机。备份保留 KEEP_DAYS 天后清理。
#
# 数据源解析顺序与 config_paths.py 一致：环境变量优先，项目根 .env 兜底，
# 最后回退 ~/.ai-agent/data（键值均不覆盖已存在的进程环境变量，等价于
# load_dotenv(override=False)）。改配 KIOXIA_DATA_DIR 后源目录自动跟随，
# 不再硬编码旧位置导致备份陈旧数据或失败。
#
# 备份目录默认落到系统盘 ~/.ai-agent/backups（与源盘独立，源盘拔除/损坏
# 不影响备份），可用 XIAODA_BACKUP_DIR 覆盖（环境变量/.env 同上）。
#
# 权限：备份含对话/记忆/用户画像等敏感数据，umask 077 + 目录 700 + 文件
# 600，禁止本机其他用户读取。
#
# 退出码：0=全部成功且校验通过；1=任一库校验失败。
set -euo pipefail
umask 077

# 项目根（脚本位于 <root>/scripts/ 下），.env 解析基准与 config_paths.py 相同
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_ROOT}/.env"

# 从 .env 读取键值（尽力而为）：匹配 KEY=VALUE / export KEY=VALUE，
# 取最后一次定义（与 dotenv 覆盖顺序一致），去引号与首尾空白。
# 仅作环境变量缺失时的兜底，绝不覆盖进程环境（override=False 语义）。
_dotenv_get() {
    local key="$1" raw=""
    [ -f "$ENV_FILE" ] || return 0
    raw="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}=" "$ENV_FILE" 2>/dev/null | tail -n 1 || true)"
    [ -n "$raw" ] || return 0
    raw="${raw#*=}"
    # 去首尾空白
    raw="${raw#"${raw%%[![:space:]]*}"}"
    raw="${raw%"${raw##*[![:space:]]}"}"
    # 去成对引号（dotenv 对 "value" / 'value' 均剥壳）
    if [ "${#raw}" -ge 2 ]; then
        case "$raw" in
            \"*\") raw="${raw#\"}"; raw="${raw%\"}" ;;
            \'*\') raw="${raw#\'}"; raw="${raw%\'}" ;;
        esac
    fi
    printf '%s' "$raw"
}

# 键解析：环境变量优先，.env 兜底（镜像 config_paths.py 的解析顺序）
_resolve_key() {
    local key="$1"
    local val="${!key:-}"
    [ -n "$val" ] || val="$(_dotenv_get "$key")"
    printf '%s' "$val"
}

KEEP_DAYS=7
TS="$(date +%Y%m%d_%H%M%S)"

# ── 源目录：KIOXIA_DATA_DIR（env → .env → ~/.ai-agent/data） ──
DATA_BASE="$(_resolve_key KIOXIA_DATA_DIR)"
[ -n "$DATA_BASE" ] || DATA_BASE="${HOME}/.ai-agent/data"
SRC_DIR="${DATA_BASE}/db"

# ── 备份目录：XIAODA_BACKUP_DIR（env → .env），默认系统盘 ~/.ai-agent/backups ──
# 默认刻意与源盘独立：同盘互备无容灾意义，源盘故障时备份将一并丢失。
DST_ROOT="$(_resolve_key XIAODA_BACKUP_DIR)"
[ -n "$DST_ROOT" ] || DST_ROOT="${HOME}/.ai-agent/backups"
DST_DIR="${DST_ROOT}/${TS}"

mkdir -p "$DST_DIR"
# umask 077 已保证新建目录 700；chmod 兜底收紧预存在目录的历史宽松权限
chmod 700 "$DST_ROOT" "$DST_DIR"

# 设备独立性提示：备份与源位于不同设备才具备容灾价值
src_dev="$(stat -c %d "$SRC_DIR" 2>/dev/null || true)"
dst_dev="$(stat -c %d "$DST_ROOT" 2>/dev/null || true)"
if [ -n "$src_dev" ] && [ -n "$dst_dev" ] && [ "$src_dev" != "$dst_dev" ]; then
    echo "[backup] 备份与源不同设备（独立设备容灾）：源=${SRC_DIR} 备份=${DST_ROOT}"
elif [ -n "$src_dev" ] && [ -n "$dst_dev" ]; then
    echo "[warn] 备份与源位于同一设备，源盘故障时备份将一并丢失：${DST_ROOT}"
fi

for db in agent.db agent_vec.db; do
    echo "[backup] ${SRC_DIR}/${db} → ${DST_DIR}/${db}"
    sqlite3 "file:${SRC_DIR}/${db}?mode=ro" \
        ".backup '${DST_DIR}/${db}'"
    # umask 077 已保证 600；chmod 兜底显式声明敏感数据权限契约
    chmod 600 "${DST_DIR}/${db}"
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

# 轮转：删除超期备份目录。只清理本脚本管理的 DST_ROOT 下 20* 时间戳目录，
# 不触碰该目录内的其他文件（如手动导出的 agent_latest.db）。
find "$DST_ROOT" -maxdepth 1 -type d -name '20*' -mtime +"${KEEP_DAYS}" -exec rm -rf {} \; 2>/dev/null || true

rm -f "${DST_DIR}"/*-shm "${DST_DIR}"/*-wal
echo "[done] 本份合计 $((total / 1024 / 1024))MB，保留 ${KEEP_DAYS} 天，备份根=${DST_ROOT}"
exit $fail
