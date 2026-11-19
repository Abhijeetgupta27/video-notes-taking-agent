#!/usr/bin/env bash
# submit a job without the extension (handy for testing)
#   NOTION_TOKEN=ntn_xxx ./scripts/submit.sh "https://www.youtube.com/watch?v=..." [parent_page_id]
API=${API:-http://localhost:8000}
[ -z "$1" ] && { echo "usage: NOTION_TOKEN=... $0 <youtube_url> [parent_id]"; exit 1; }
[ -z "$NOTION_TOKEN" ] && { echo "set NOTION_TOKEN first"; exit 1; }
body=$(python3 -c 'import json,sys; d={"youtube_url":sys.argv[1],"notion_token":sys.argv[2]}
if len(sys.argv)>3 and sys.argv[3]: d["notion_parent_id"]=sys.argv[3]
print(json.dumps(d))' "$1" "$NOTION_TOKEN" "$2")
curl -s -X POST "$API/jobs" -H 'Content-Type: application/json' -d "$body"
echo
