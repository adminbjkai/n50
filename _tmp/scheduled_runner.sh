#!/bin/bash
# Bounded one-off schedule (≈45 min, then exits):
#   run_next_set_v3.py at  0,  5, 10, 15, 20 min   (5 runs, first immediately)
#   run_next_set_v2.py at 25, 30, 35, 40, 45 min   (5 runs, first 5 min after v3's last)
# Each run-start is anchored to an absolute offset from START, so there is no drift
# and the two scripts never overlap (each run takes < 1 min). A failed run is logged
# and the schedule continues to the next slot.

cd /Users/m17/2026/notion50/notion50new-v4 || exit 1
LOG="/Users/m17/2026/notion50/notion50new-v4/_tmp/scheduled_runs.log"
START=$(date +%s)

run() {
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') START $1 ===" >> "$LOG"
  python3 "$1" >> "$LOG" 2>&1
  echo "=== $(date '+%Y-%m-%d %H:%M:%S') END $1 (exit $?) ===" >> "$LOG"
  echo >> "$LOG"
}

run_at() {   # $1 = offset seconds from START, $2 = script
  local target=$((START + $1))
  local delay=$(( target - $(date +%s) ))
  [ "$delay" -gt 0 ] && sleep "$delay"
  run "$2"
}

echo "######## scheduled batch started $(date '+%Y-%m-%d %H:%M:%S') (pid $$) ########" >> "$LOG"

run_at 0    run_next_set_v3.py
run_at 300  run_next_set_v3.py
run_at 600  run_next_set_v3.py
run_at 900  run_next_set_v3.py
run_at 1200 run_next_set_v3.py
run_at 1500 run_next_set_v2.py
run_at 1800 run_next_set_v2.py
run_at 2100 run_next_set_v2.py
run_at 2400 run_next_set_v2.py
run_at 2700 run_next_set_v2.py

echo "######## scheduled batch finished $(date '+%Y-%m-%d %H:%M:%S') ########" >> "$LOG"
