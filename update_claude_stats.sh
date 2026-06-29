#!/bin/bash
# Update Claude stats and push to GitHub
# Run manually or via launchd schedule

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "$(date): Updating Claude stats..."

# Generate fresh stats
python3 parse_claude_stats.py

# Check if there are changes to commit
if git diff --quiet claude_stats.json 2>/dev/null; then
    echo "$(date): No changes to Claude stats"
    exit 0
fi

# Commit and push
git add claude_stats.json
git commit -m "Update Claude stats [skip ci]"

# Integrate the Actions bot's dashboard_data commits before pushing, otherwise
# the push is rejected as non-fast-forward and stats silently stop reaching
# origin. Local commits only touch claude_stats.json and the bot's only touch
# dashboard_data.json / loc_history.json, so this rebase never conflicts.
git pull --rebase --autostash origin main

git push

echo "$(date): Claude stats updated and pushed"
