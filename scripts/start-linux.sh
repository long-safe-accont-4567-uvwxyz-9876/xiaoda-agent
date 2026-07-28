#!/bin/bash
# =============================================================================
#  Xiaoda Agent — Linux 统一启动入口
#  职责：1) 检查自动更新  2) 看门狗（崩溃自动重启）  3) 启动 agent.py
#  用法：./start-linux.sh [--desktop | --web | --host 0.0.0.0 --port 8082]
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 如果从 scripts/ 启动，上一级是安装目录；如果从安装目录直接启动，当前就是安装目录
if [ "$(basename "$SCRIPT_DIR")" = "scripts" ]; then
    INSTALL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
    INSTALL_DIR="$SCRIPT_DIR"
    SCRIPT_DIR="${INSTALL_DIR}/scripts"
fi

# Python 解释器：优先用 venv，其次用系统 python3
if [ -x "${INSTALL_DIR}/.venv/bin/python" ]; then
    PYTHON="${INSTALL_DIR}/.venv/bin/python"
else
    PYTHON="$(command -v python3 || echo python3)"
fi

# ── 1. 自动更新检查（非阻塞，失败不阻止启动）──────────────
if [ -f "${SCRIPT_DIR}/auto-update.sh" ]; then
    echo "[start] 检查更新..."
    bash "${SCRIPT_DIR}/auto-update.sh" 2>&1 || true
fi

# ── 2. 看门狗：崩溃自动重启，最多 20 次/10 分钟 ────────────
MAX_RESTARTS=20
RESTART_WINDOW=600  # 10 分钟（秒）
restart_count=0
first_crash_time=0

watchdog() {
    while true; do
        # 启动 agent
        echo "[start] 启动 Xiaoda Agent..."
        "$PYTHON" "${INSTALL_DIR}/agent.py" "$@" || {
            exit_code=$?
            now=$(date +%s)

            # 初始化首次崩溃时间
            if [ $restart_count -eq 0 ]; then
                first_crash_time=$now
            fi

            # 检查是否超出重启窗口
            elapsed=$((now - first_crash_time))
            if [ $elapsed -gt $RESTART_WINDOW ]; then
                # 窗口已过，重置计数
                restart_count=0
                first_crash_time=$now
            fi

            restart_count=$((restart_count + 1))
            if [ $restart_count -ge $MAX_RESTARTS ]; then
                echo "[start] 看门狗：${MAX_RESTARTS} 次崩溃在 ${RESTART_WINDOW} 秒内，停止重启"
                echo "[start] 请运行 ${SCRIPT_DIR}/doctor.sh 诊断问题"
                # 必须退出码 0：systemd Restart=on-failure 会对非零退出码重启，
                # 导致看门狗的"放弃"机制失效（重启并清零计数）
                exit 0
            fi

            echo "[start] 进程退出 (code=${exit_code})，${restart_count}/${MAX_RESTARTS} 次重启，3 秒后重试..."
            sleep 3
            continue
        }

        # 正常退出
        break
    done
}

# ── 3. 启动 ───────────────────────────────────────────────
# 默认参数：--web
if [ $# -eq 0 ]; then
    set -- --web
fi

watchdog "$@"
