#!/usr/bin/env bash
# print every status change of a job until it finishes
#   ./scripts/watch.sh <job_id>
API=${API:-http://localhost:8000}
last=""
while true; do
  line=$(curl -s "$API/jobs/$1" | python3 -c '
import sys, json
d = json.load(sys.stdin)
if "error" in d and "status" not in d: print("ERR", d["error"]["message"]); sys.exit()
w = d.get("waiting") or {}
extra = " (retry %s/%s)" % (w["attempt"], w["max_attempts"]) if w else ""
print("%-24s %3s%%  %-18s %s%s" % (d["status"], d["progress"], d.get("worker") or "-", d.get("detail") or "", extra))')
  if [ "$line" != "$last" ]; then echo "$(date +%H:%M:%S)  $line"; last="$line"; fi
  case "$line" in COMPLETED*|FAILED*|DEAD_LETTERED*|ERR*) break;; esac
  sleep 1
done
curl -s "$API/jobs/$1/results"; echo
