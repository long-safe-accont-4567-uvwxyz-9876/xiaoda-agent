#!/bin/bash
# 本地服务阻塞看门狗：检测到检索超时/LLM 错误时，立即 py-spy dump 线程栈 + journalctl 快照
mkdir -p /tmp/dumps
journalctl -u nahida-web.service -f -o cat 2>/dev/null | while read -r line; do
  echo "$line" | grep -qE "memory\.retrieve_global_timeout|agent\.model_error|stage_slow stage=main_path elapsed_ms=[0-9]{5}" || continue
  TS=$(date +%Y%m%d_%H%M%S)
  PID=$(systemctl show -p MainPID --value nahida-web)
  {
    echo "=== BLOCK DETECTED at $TS pid=$PID ==="
    echo "=== trigger line: $line ==="
    echo "=== py-spy dump (暂停) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" 2>&1
    echo "=== py-spy dump (非阻塞) ==="
    sudo -n /home/orangepi/ai-agent/.venv/bin/py-spy dump --pid "$PID" --nonblocking 2>&1
  } > "/tmp/dumps/block_$TS.dump"
  journalctl -u nahida-web.service --since "-3 min" --no-pager > "/tmp/dumps/block_$TS.log" 2>&1
  echo "BLOCK_DETECTED $TS" >> /tmp/dumps/watchdog.log
  sleep 45
done
