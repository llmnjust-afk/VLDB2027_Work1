#!/bin/bash
cd /data/lab/VLDB2027_Work1
LOG=/data/lab/perf2.log
SUP=/data/lab/supervisor.log
while true; do
  sleep 30
  if grep -q "PERF DONE" "$LOG" 2>/dev/null; then
    echo "$(date '+%F %T') suite complete; supervisor exiting" >> "$SUP"
    exit 0
  fi
  bt=$(pgrep -fc "batchtru[s]" 2>/dev/null)
  rp=$(pgrep -fc "run_per[f]" 2>/dev/null)
  if [ "${bt:-0}" -eq 0 ] && [ "${rp:-0}" -eq 0 ]; then
    echo "$(date '+%F %T') no workers alive; relaunching suite" >> "$SUP"
    setsid nohup bash scripts/run_perf.sh >> "$LOG" 2>&1 &
  fi
done
