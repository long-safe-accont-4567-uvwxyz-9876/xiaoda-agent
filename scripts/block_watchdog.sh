#!/bin/bash
# 本地服务阻塞看门狗：检测到检索阻塞时，py-spy dump 线程栈 + journalctl 快照。
#
# 两种模式（首个参数）：
#   timeout（默认）: 流式扫描超时/错误关键字，命中即快照（原 block_watchdog.sh 行为）
#   stuck           : 轮询检测 tier/gather 卡住 6 秒无进展即快照（原 block_watchdog2.sh 行为）
#
# 用法: ./block_watchdog.sh [timeout|stuck]
set -u
MODE="${1:-timeout}"
mkdir -p /tmp/dumps

# 统一的快照动作：py-spy dump（暂停 + 非阻塞）+ journalctl 近 3 分钟日志 + 追加记录。
dump_snapshot() {
  local reason="$1" trigger="${2:-}" TS PID
  TS=$(date +%Y%m%d_%H%M%S)
  PID=$(systemctl show -p MainPID --value xiaoda-agent)
  {
    echo "=== $reason at $TS pid=$PID ==="
    if [ -n "$trigger" ]; then
      echo "=== trigger line: $trigger ==="
    fi
    echo "=== py-spy dump (暂停) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" 2>&1
    echo "=== py-spy dump (非阻塞) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" --nonblocking 2>&1
  } > "/tmp/dumps/${reason}_${TS}.dump"
  journalctl -u xiaoda-agent.service --since "-3 min" --no-pager > "/tmp/dumps/${reason}_${TS}.log" 2>&1
  echo "${reason} $TS" >> /tmp/dumps/watchdog.log
}

if [ "$MODE" = "stuck" ]; then
  # 进行中抓取：检测 tier_start/gather_start 后 6 秒内无进展，立即 dump ——
  # 抓"卡住瞬间"而非超时后的终态。
  # 进程替换 <(journalctl) 保证 while 内变量赋值在当前 shell 生效。
  TIER_AT=0; GATHER_AT=0; LAST_DUMP=0
  while true; do
    now=$(date +%s)
    while read -r line; do
      case "$line" in
        *memory.tier_start*) TIER_AT=$now ;;
        *memory.tier_done*|*memory.retrieve_global_timeout*) TIER_AT=0 ;;
        *memory.gather_start*) GATHER_AT=$now ;;
        *channel_*|*memory.retrieve_global_timeout*) GATHER_AT=0 ;;
      esac
    done < <(journalctl -u xiaoda-agent.service --since "-4 sec" --no-pager -o cat 2>/dev/null)
    if [ "$TIER_AT" -ne 0 ] && [ $((now - TIER_AT)) -ge 6 ] && [ $((now - LAST_DUMP)) -ge 20 ]; then
      dump_snapshot "tier_stuck"; LAST_DUMP=$now; TIER_AT=0
    elif [ "$GATHER_AT" -ne 0 ] && [ $((now - GATHER_AT)) -ge 6 ] && [ $((now - LAST_DUMP)) -ge 20 ]; then
      dump_snapshot "gather_stuck"; LAST_DUMP=$now; GATHER_AT=0
    fi
    sleep 2
  done
else
  # 超时后快照：流式扫描超时/错误关键字，命中即 dump，之后冷却 45s 避免刷屏。
  journalctl -u xiaoda-agent.service -f -o cat 2>/dev/null | while read -r line; do
    echo "$line" | grep -qE "memory\.retrieve_global_timeout|agent\.model_error|stage_slow stage=main_path elapsed_ms=[0-9]{5}" || continue
    dump_snapshot "block" "$line"
    sleep 45
  done
fi
