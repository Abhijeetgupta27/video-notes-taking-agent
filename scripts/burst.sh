#!/usr/bin/env bash
# fire several jobs at once (queue resilience demo). urls as args or one per line in a file
#   NOTION_TOKEN=... ./scripts/burst.sh urls.txt
DIR=$(dirname "$0")
if [ -f "$1" ]; then
  while IFS= read -r u; do [ -n "$u" ] && "$DIR/submit.sh" "$u"; done < "$1"
else
  for u in "$@"; do "$DIR/submit.sh" "$u"; done
fi
