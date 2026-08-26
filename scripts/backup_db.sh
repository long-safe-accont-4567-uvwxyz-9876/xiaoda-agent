#!/bin/bash
# =============================================================================
# agent.db 每日热备（2026-08-25 运维 review 落地）
# 背景：209MB 生产主库（陪伴记忆/知识图谱/会话日志）位于外接 USB 盘，
#       此前零自动备份，盘损即全部记忆丢失。
# 策略：
#   1) sqlite3 .backup 在线热备——WAL 模式下不锁库、不影响读写；
#   2) 同盘保留最近 7 份轮转（/mnt/usb2/nahida-data/backup/）；
#   3) 最新一份跨设备镜像到内部存储（~/.ai-agent/backups/）——USB 盘损时
#      的最后保险。注意：内部 eMMC 空间有限(3G 级)，只保留单份 latest。
# 安装：crontab 一行（用户级）：
#   0 4 * * * /home/orangepi/ai-agent/scripts/backup_db.sh >> /mnt/usb2/nahida-data/logs/backup.log 2>&1
# 手动执行：直接运行本脚本。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 数据根目录：默认 /mnt/usb2/nahida-data，可被仓库 .env 的 KIOXIA_DATA_DIR 覆盖
# （只提取该键，不 source 整个 .env；tr 剥离包裹引号，\047=' \042="）
DATA_ROOT="/mnt/usb2/nahida-data"
ENV_FILE="$SCRIPT_DIR/../.env"
if [ -f "$ENV_FILE" ]; then
    KIOXIA_DIR=$(grep -E '^KIOXIA_DATA_DIR=' "$ENV_FILE" | tail -n1 | cut -d= -f2- | tr -d '\047\042' || true)
    if [ -n "$KIOXIA_DIR" ]; then
        DATA_ROOT="$KIOXIA_DIR"
    fi
fi

DB="$DATA_ROOT/db/agent.db"
BACKUP_DIR="$DATA_ROOT/backup"
MIRROR_DIR="$HOME/.ai-agent/backups"
KEEP=7
STAMP=$(date +%Y%m%d_%H%M%S)

[ -f "$DB" ] || { echo "[backup] 主库不存在: $DB"; exit 1; }
mkdir -p "$BACKUP_DIR" "$MIRROR_DIR"

# 防重叠：上一次备份未结束时跳过本次
LOCK="/tmp/agent_db_backup.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "[backup] 上一次备份仍在进行，跳过 $(date '+%F %T')"
    exit 0
fi

# 在线热备：.backup 走 sqlite 备份 API，WAL 下安全且对业务无锁。
# 先落 .partial 名：中断/被杀不会在轮转目录留下看似完整的残缺备份；
# 通过完整性校验后才改名入列 —— 轮转槽位里永远是验证过的完整文件。
TARGET="$BACKUP_DIR/agent_${STAMP}.db"
PARTIAL="$TARGET.partial"
cleanup_partial() { rm -f "$PARTIAL"; }
trap cleanup_partial EXIT

if ! sqlite3 "$DB" ".backup '$PARTIAL'"; then
    echo "[backup] FAIL sqlite3 .backup 失败 $(date '+%F %T')"
    exit 1
fi

# 校验备份可用性（integrity 必须返回 ok 才算成功）
CHECK=$(sqlite3 "$PARTIAL" "PRAGMA quick_check" 2>/dev/null || echo "error")
if [ "$CHECK" != "ok" ]; then
    echo "[backup] FAIL 备份完整性校验: $CHECK"
    exit 1
fi
mv -f "$PARTIAL" "$TARGET"
trap - EXIT

SIZE_MB=$(du -m "$TARGET" | cut -f1)

# 同盘轮转：只留最近 KEEP 份（nullglob 防 glob 空匹配时 ls 非零退出误杀脚本）
shopt -s nullglob
old_backups=("$BACKUP_DIR"/agent_*.db)
shopt -u nullglob
if [ "${#old_backups[@]}" -gt "$KEEP" ]; then
    ls -1t "${old_backups[@]}" | tail -n +$((KEEP + 1)) | while read -r old; do
        rm -f "$old"
    done
fi

# 跨设备镜像最新一份到内部存储（覆盖式，只占单份空间）
# 原子写：先落临时名再 rename —— 中断不会留下截断的 agent_latest.db
# （它是 USB 盘损后最后的保险，宁可缺更新不可半截）
MIRROR_TMP="$MIRROR_DIR/.agent_latest.db.tmp"
rm -f "$MIRROR_TMP"
cp -f "$TARGET" "$MIRROR_TMP"
mv -f "$MIRROR_TMP" "$MIRROR_DIR/agent_latest.db"

echo "[backup] OK ${STAMP} size=${SIZE_MB}MB -> $TARGET (+mirror)"
