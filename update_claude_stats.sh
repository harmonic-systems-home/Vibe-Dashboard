#!/bin/bash
# Update Claude stats and push to GitHub
# Run manually or via launchd schedule (02:00, from its own clone; see jobs/).
#
# The commit deliberately has no [skip ci]: its push starts the dashboard
# workflow straight away. GitHub defers the workflow's own scheduled run by
# many hours (10+ in late Nov 2026), so this push is what refreshes the
# dashboard overnight; the cron run stays as a fallback for nights the Mac is
# off.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "$(date): Updating Claude stats..."

# Start every run from origin/main. This clone exists only for this job, so
# nothing local is worth keeping: claude_stats.json is regenerated from the
# session files, with the published copy as its history floor. Starting clean
# also clears whatever an earlier failed run left behind (a stuck rebase kept
# the job from pushing anything from Nov 24 to Nov 28, 2026).
sync_to_origin() {
    git rebase --abort 2>/dev/null || true
    git merge --abort 2>/dev/null || true
    git fetch --quiet origin main
    git checkout --quiet -B main origin/main
    git reset --quiet --hard origin/main
}

for attempt in 1 2 3; do
    sync_to_origin
    python3 parse_claude_stats.py

    if git diff --quiet claude_stats.json 2>/dev/null; then
        echo "$(date): No changes to Claude stats"
        exit 0
    fi

    git add claude_stats.json
    git commit --quiet -m "Update Claude stats"

    # Rejected only if someone pushed in between; regenerate on top of theirs.
    if git push --quiet origin main; then
        echo "$(date): Claude stats updated and pushed"
        exit 0
    fi
    echo "$(date): push rejected (attempt $attempt), retrying"
    sleep 10
done

echo "$(date): giving up after 3 attempts"
exit 1
