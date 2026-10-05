#!/bin/bash
#
# deploy.sh - install Vibe-Dashboard's launchd job (nightly Claude stats).
#
# The repo lives on the Express, and macOS won't let launchd jobs read an
# external volume without Full Disk Access. The job commits and pushes
# claude_stats.json, so instead of a file copy it runs from its own clone on
# the internal SSD. That clone pulls origin/main every run, so it picks up
# PUSHED changes to update_claude_stats.sh / parse_claude_stats.py by itself;
# local, unpushed edits never reach it.
#
# Re-run after changing jobs/launchd/, or to (re)create the clone.
#
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
JOB_DIR="$HOME/Library/Application Support/launchd-jobs/Vibe-Dashboard"
DOMAIN="gui/$(id -u)"

if [[ -d "$JOB_DIR/.git" ]]; then
    git -C "$JOB_DIR" pull --ff-only --quiet
else
    mkdir -p "$(dirname "$JOB_DIR")"
    git clone --quiet "$(git -C "$REPO" remote get-url origin)" "$JOB_DIR"
fi

# Render each plist template and (re)load it.
for tpl in "$REPO"/jobs/launchd/*.plist; do
    label="$(basename "$tpl" .plist)"
    dst="$HOME/Library/LaunchAgents/$label.plist"
    tmp="$(mktemp)"
    sed -e "s|@JOB_DIR@|$JOB_DIR|g" -e "s|@HOME@|$HOME|g" "$tpl" > "$tmp"
    plutil -lint -s "$tmp"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    mv "$tmp" "$dst"
    launchctl bootstrap "$DOMAIN" "$dst"
    echo "loaded $label"
done
