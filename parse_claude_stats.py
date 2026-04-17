#!/usr/bin/env python3
"""
Parse Claude Code session files to extract user prompt statistics.
Generates claude_stats.json with user prompt counts instead of total messages.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from collections import defaultdict

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
STATS_CACHE = CLAUDE_DIR / "stats-cache.json"

def parse_session_file(filepath):
    """Parse a session .jsonl file and extract prompts, tool calls, and session start.

    Returns (user_prompt_timestamps, tool_call_timestamps, session_start_timestamp).
    """
    user_prompts = []
    tool_calls = []
    session_start = None

    try:
        with open(filepath, 'r') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    timestamp = entry.get('timestamp')
                    if timestamp and session_start is None:
                        session_start = timestamp

                    # User prompts: type=user, userType=external, not a tool_result
                    if (entry.get('type') == 'user' and
                        entry.get('userType') == 'external' and
                        entry.get('message', {}).get('role') == 'user'):

                        content = entry.get('message', {}).get('content', [])
                        if isinstance(content, list):
                            has_text = any(c.get('type') == 'text' for c in content)
                            has_tool_result = any(c.get('type') == 'tool_result' for c in content)
                            if has_text and not has_tool_result and timestamp:
                                user_prompts.append(timestamp)
                        elif isinstance(content, str) and content.strip() and timestamp:
                            user_prompts.append(timestamp)

                    # Tool calls: assistant messages with tool_use content blocks
                    elif entry.get('type') == 'assistant':
                        content = entry.get('message', {}).get('content', [])
                        if isinstance(content, list) and timestamp:
                            for c in content:
                                if c.get('type') == 'tool_use':
                                    tool_calls.append(timestamp)
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Error reading {filepath}: {e}")

    return user_prompts, tool_calls, session_start

def main():
    # Load existing stats-cache for base data
    if STATS_CACHE.exists():
        with open(STATS_CACHE) as f:
            stats = json.load(f)
    else:
        stats = {}

    all_prompts = []
    all_tool_calls = []
    session_starts = []

    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if project_dir.is_dir():
                for session_file in project_dir.glob("*.jsonl"):
                    prompts, tools, start = parse_session_file(session_file)
                    all_prompts.extend(prompts)
                    all_tool_calls.extend(tools)
                    if start:
                        session_starts.append(start)

    def ts_to_local_date(ts):
        try:
            return datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d')
        except Exception:
            return None

    prompts_by_date = defaultdict(int)
    prompts_by_hour = defaultdict(int)
    for ts in all_prompts:
        try:
            dt_local = datetime.fromisoformat(ts.replace('Z', '+00:00')).astimezone()
            prompts_by_date[dt_local.strftime('%Y-%m-%d')] += 1
            prompts_by_hour[dt_local.hour] += 1
        except Exception:
            continue

    tool_calls_by_date = defaultdict(int)
    for ts in all_tool_calls:
        d = ts_to_local_date(ts)
        if d:
            tool_calls_by_date[d] += 1

    sessions_by_date = defaultdict(int)
    for ts in session_starts:
        d = ts_to_local_date(ts)
        if d:
            sessions_by_date[d] += 1

    # Build per-date lookup from the legacy stats-cache (covers dates before
    # the cache stopped updating; newer dates default to 0).
    cache_by_date = {
        d.get('date'): d
        for d in stats.get('dailyActivity', [])
        if d.get('date')
    }

    all_dates = set(prompts_by_date) | set(tool_calls_by_date) | set(sessions_by_date) | set(cache_by_date)
    daily_activity = []
    for date_str in sorted(all_dates):
        cache = cache_by_date.get(date_str, {})
        # Prefer the higher value per-field: cache may retain deleted sessions,
        # jsonls capture dates the cache stopped updating.
        daily_activity.append({
            'date': date_str,
            'userPrompts': max(prompts_by_date.get(date_str, 0), cache.get('userPrompts', 0) or 0),
            'sessionCount': max(sessions_by_date.get(date_str, 0), cache.get('sessionCount', 0) or 0),
            'toolCallCount': max(tool_calls_by_date.get(date_str, 0), cache.get('toolCallCount', 0) or 0),
        })

    first_session = min(session_starts)[:10] if session_starts else stats.get('firstSessionDate')

    output = {
        'version': 1,
        'lastComputedDate': datetime.now().strftime('%Y-%m-%d'),
        'totalUserPrompts': len(all_prompts),
        'totalSessions': len(session_starts),
        'totalToolCalls': len(all_tool_calls),
        'totalMessages': stats.get('totalMessages', 0),
        'firstSessionDate': first_session,
        'modelUsage': stats.get('modelUsage', {}),
        'dailyActivity': daily_activity,
        'hourCounts': dict(prompts_by_hour),
        'longestSession': stats.get('longestSession')
    }

    # Write output
    output_path = Path(__file__).parent / 'claude_stats.json'
    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    print(f"Parsed {len(all_prompts)} user prompts from {len(list(PROJECTS_DIR.iterdir()))} projects")
    print(f"Written to {output_path}")

if __name__ == '__main__':
    main()
