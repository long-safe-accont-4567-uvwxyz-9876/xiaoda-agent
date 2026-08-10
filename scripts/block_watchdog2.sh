#!/bin/bash
# 检索阻塞看门狗（进行中抓取）：检测 memory.tier_start / memory.gather_start 后
# 6 秒内无进展（tier_done / channel_ / 超时），立即 py-spy dump —— 抓"卡住瞬间"
# 而非超时后的终态。与 block_watchdog.sh（超时后快照）互补。
# 进程替换 <(journalctl) 保证 while 内变量赋值在当前 shell 生效。
mkdir -p /tmp/dumps
TIER_AT=0; GATHER_AT=0; LAST_DUMP=0
dump_and_reset() {
  local reason="$1" TS PID
  TS=$(date +%Y%m%d_%H%M%S)
  PID=$(systemctl show -p MainPID --value xiaoda-agent)
  {
    echo "=== $reason at $TS pid=$PID ==="
    echo "=== py-spy dump (暂停) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" 2>&1
    echo "=== py-spy dump (非阻塞) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" --nonblocking 2>&1
  } > "/tmp/dumps/${reason}_${TS}.dump"
  journalctl -u xiaoda-agent.service --since "-3 min" --no-pager > "/tmp/dumps/${reason}_${TS}.log" 2>&1
  echo "${reason} $TS" >> /tmp/dumps/watchdog.log
}
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
    dump_and_reset "tier_stuck"; LAST_DUMP=$now; TIER_AT=0
  elif [ "$GATHER_AT" -ne 0 ] && [ $((now - GATHER_AT)) -ge 6 ] && [ $((now - LAST_DUMP)) -ge 20 ]; then
    dump_and_reset "gather_stuck"; LAST_DUMP=$now; GATHER_AT=0
  fi
  sleep 2
done
