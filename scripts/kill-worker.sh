#!/usr/bin/env bash
# SIGKILL the worker container that is currently running a job (crash recovery demo)
#   ./scripts/kill-worker.sh <job_id>
# tip: start the stack with FRAME_DELAY=4 in .env so the frames stage is slow enough to hit
API=${API:-http://localhost:8000}
w=$(curl -s "$API/jobs/$1" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("worker") or "")')
[ -z "$w" ] && { echo "job has no worker yet"; exit 1; }
cid=${w%%:*}   # worker id is <container hostname>:<pid>, hostname = container id
echo "killing $cid (job $1)"
docker kill --signal KILL "$cid"
