#!/usr/bin/env python3
"""
Issue fetcher for the Vibe Dashboard "Issues" tab.
==================================================
Pulls every issue (open and closed) from the public repos of one or more
GitHub owners and writes issues_data.json: a flat list of issues plus
per-repo/overall counts. The dashboard buckets them over time client-side.

Pull requests are excluded — GitHub's issues endpoint returns them too.

--owners scans an owner's public repos; --repos names individual repos and
always includes them, which is how a repo the --dashboard-data scoping would
drop still gets its issues counted.

Usage:
    export GITHUB_TOKEN="..."           # or --token, or `gh auth token`
    python fetch_issues.py --owners Rick-Wilson bridge-craftwork
    python fetch_issues.py --repos owner/repo1 owner/repo2
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GITHUB_API = "https://api.github.com"


class IssueFetcher:
    def __init__(self, token=None):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.session = requests.Session()
        if self.token:
            self.session.headers["Authorization"] = f"token {self.token}"
        self.session.headers["Accept"] = "application/vnd.github+json"
        self.session.headers["User-Agent"] = "Vibe-Dashboard"

    def _get(self, endpoint, params=None):
        r = self.session.get(f"{GITHUB_API}{endpoint}", params=params)
        if r.status_code == 403 and "rate limit" in r.text.lower():
            reset = r.headers.get("X-RateLimit-Reset")
            if reset:
                print(f"⚠️  Rate limited. Resets at {datetime.fromtimestamp(int(reset))}")
            raise RuntimeError("GitHub API rate limit exceeded")
        r.raise_for_status()
        return r.json()

    def _paginated(self, endpoint, params=None, max_pages=30):
        params = dict(params or {})
        params["per_page"] = 100
        out = []
        for page in range(1, max_pages + 1):
            params["page"] = page
            batch = self._get(endpoint, params)
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    def owner_repos(self, owner):
        """All repos for a user or org (the token's visibility applies)."""
        kind = self._get(f"/users/{owner}").get("type", "User")
        endpoint = f"/orgs/{owner}/repos" if kind == "Organization" else f"/users/{owner}/repos"
        return self._paginated(endpoint, {"type": "owner", "sort": "updated"})

    def repo_info(self, owner, repo):
        return self._get(f"/repos/{owner}/{repo}")

    def repo_issues(self, owner, repo):
        """Every issue in a repo, newest first. PRs are filtered out."""
        raw = self._paginated(f"/repos/{owner}/{repo}/issues",
                              {"state": "all", "sort": "created", "direction": "desc"})
        return [i for i in raw if "pull_request" not in i]


def normalize_issue(issue, repo_full_name):
    """Trim a GitHub issue payload down to what the dashboard renders."""
    return {
        "repo": repo_full_name.split("/", 1)[-1],
        "full_name": repo_full_name,
        "number": issue["number"],
        "title": issue.get("title") or "",
        "state": issue.get("state") or "open",
        "created_at": issue.get("created_at"),
        "closed_at": issue.get("closed_at"),
        "url": issue.get("html_url"),
        "labels": [l["name"] for l in issue.get("labels", []) if isinstance(l, dict)],
        "comments": issue.get("comments", 0),
        "author": (issue.get("user") or {}).get("login"),
    }


def load_allowed_repos(dashboard_data_file):
    """Repo full_names already in dashboard_data.json — the owner's own repos.

    Used to keep the Issues tab scoped to the same set of projects the rest of
    the dashboard reports on. Absent/empty file means "no restriction".
    """
    p = Path(dashboard_data_file) if dashboard_data_file else None
    if not p or not p.exists():
        return set()
    try:
        data = json.loads(p.read_text())
    except Exception:
        return set()
    return {p_["full_name"] for p_ in data.get("projects", []) if p_.get("full_name")}


def days_between(start, end):
    if not start or not end:
        return None
    s = datetime.fromisoformat(start.replace("Z", "+00:00"))
    e = datetime.fromisoformat(end.replace("Z", "+00:00"))
    return (e - s).total_seconds() / 86400.0


def median(values):
    if not values:
        return None
    vals = sorted(values)
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def build_stats(issues):
    now = datetime.now(timezone.utc)
    cutoff_30 = now - timedelta(days=30)
    cutoff_90 = now - timedelta(days=90)

    def parse(ts):
        return datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None

    open_issues = [i for i in issues if i["state"] == "open"]
    closed_issues = [i for i in issues if i["state"] != "open"]

    opened_30 = sum(1 for i in issues if (parse(i["created_at"]) or now) >= cutoff_30)
    closed_30 = sum(1 for i in closed_issues if (parse(i["closed_at"]) or now) >= cutoff_30)
    opened_90 = sum(1 for i in issues if (parse(i["created_at"]) or now) >= cutoff_90)
    closed_90 = sum(1 for i in closed_issues if (parse(i["closed_at"]) or now) >= cutoff_90)

    resolution_days = [d for d in (days_between(i["created_at"], i["closed_at"]) for i in closed_issues) if d is not None]

    # Age of the still-open backlog, oldest first
    open_ages = [d for d in (days_between(i["created_at"], now.isoformat()) for i in open_issues) if d is not None]

    by_repo = {}
    for i in issues:
        entry = by_repo.setdefault(i["repo"], {"open": 0, "closed": 0, "total": 0})
        entry["open" if i["state"] == "open" else "closed"] += 1
        entry["total"] += 1

    return {
        "total": len(issues),
        "open": len(open_issues),
        "closed": len(closed_issues),
        "close_rate": round(100 * len(closed_issues) / len(issues)) if issues else 0,
        "opened_last_30": opened_30,
        "closed_last_30": closed_30,
        "opened_last_90": opened_90,
        "closed_last_90": closed_90,
        "median_days_to_close": round(median(resolution_days), 1) if resolution_days else None,
        "median_open_age_days": round(median(open_ages), 1) if open_ages else None,
        "oldest_open_days": round(max(open_ages), 1) if open_ages else None,
        "repo_count": len(by_repo),
        "by_repo": by_repo,
    }


def main():
    parser = argparse.ArgumentParser(description="Fetch GitHub issues for the Vibe Dashboard")
    parser.add_argument("--owners", nargs="+", help="GitHub users/orgs whose repos to scan")
    parser.add_argument("--repos", nargs="+",
                        help="Specific repos (format: owner/repo). Always scanned, even when "
                             "--dashboard-data would otherwise exclude them")
    parser.add_argument("--include-private", action="store_true",
                        help="Include private repos (default: public only)")
    parser.add_argument("--include-archived", action="store_true",
                        help="Include archived repos (default: skipped)")
    parser.add_argument("--dashboard-data", default="dashboard_data.json",
                        help="Restrict to repos present in this dashboard_data.json (pass '' to disable)")
    parser.add_argument("--output", default="issues_data.json", help="Output JSON file")
    parser.add_argument("--token", help="GitHub token (or set GITHUB_TOKEN)")
    args = parser.parse_args()

    if not args.owners and not args.repos:
        print("❌ Specify --owners and/or --repos")
        sys.exit(1)

    fetcher = IssueFetcher(args.token)
    if not fetcher.token:
        print("⚠️  No GitHub token — only public data, and a 60 req/hour limit")

    allowed = load_allowed_repos(args.dashboard_data)
    if allowed:
        print(f"🔎 Restricting to {len(allowed)} repos from {args.dashboard_data}")

    # Resolve the repo list -------------------------------------------------
    targets = []  # (owner, name, full_name, has_issues_enabled)
    seen = set()

    for owner in args.owners or []:
        print(f"📦 Listing repos for {owner}...")
        try:
            repos = fetcher.owner_repos(owner)
        except Exception as e:
            print(f"   ❌ Could not list repos for {owner}: {e}")
            continue
        kept = 0
        for r in repos:
            if r.get("private") and not args.include_private:
                continue
            if r.get("archived") and not args.include_archived:
                continue
            full = r["full_name"]
            if allowed and full not in allowed:
                continue
            if full in seen:
                continue
            seen.add(full)
            targets.append((r["owner"]["login"], r["name"], full, r.get("has_issues", True)))
            kept += 1
        print(f"   {len(repos)} repos, {kept} to scan")

    # Explicitly named repos bypass the --dashboard-data scoping above
    for spec in args.repos or []:
        if "/" not in spec:
            print(f"⚠️  Invalid repo format: {spec} (use owner/repo)")
            continue
        owner, name = spec.split("/", 1)
        full = f"{owner}/{name}"
        if full in seen:
            continue
        seen.add(full)
        try:
            info = fetcher.repo_info(owner, name)
        except Exception as e:
            print(f"   ❌ {full}: {e}")
            continue
        if info.get("private") and not args.include_private:
            print(f"   ⏭️  {full} is private, skipping")
            continue
        targets.append((owner, name, info["full_name"], info.get("has_issues", True)))

    if not targets:
        print("❌ No repos to scan")
        sys.exit(1)

    # Fetch issues ----------------------------------------------------------
    print(f"\n🚀 Fetching issues from {len(targets)} repos...")
    issues = []
    repos_meta = []
    for owner, name, full, has_issues in sorted(targets, key=lambda t: t[2].lower()):
        if not has_issues:
            continue
        try:
            raw = fetcher.repo_issues(owner, name)
        except Exception as e:
            print(f"   ❌ {full}: {e}")
            continue
        if raw:
            print(f"   ✅ {full}: {len(raw)} issues")
        repo_issues = [normalize_issue(i, full) for i in raw]
        issues.extend(repo_issues)
        if repo_issues:
            repos_meta.append({
                "repo": name,
                "full_name": full,
                "url": f"https://github.com/{full}",
                "open": sum(1 for i in repo_issues if i["state"] == "open"),
                "closed": sum(1 for i in repo_issues if i["state"] != "open"),
            })

    issues.sort(key=lambda i: i["created_at"] or "", reverse=True)
    stats = build_stats(issues)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": stats,
        "repos": sorted(repos_meta, key=lambda r: r["open"] + r["closed"], reverse=True),
        "issues": issues,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n✅ Issue data saved to {args.output}")
    print(f"\n📈 Summary:")
    print(f"   Issues: {stats['total']} ({stats['open']} open, {stats['closed']} closed)")
    print(f"   Repos with issues: {stats['repo_count']}")
    print(f"   Last 30 days: +{stats['opened_last_30']} opened, -{stats['closed_last_30']} closed")
    if stats["median_days_to_close"] is not None:
        print(f"   Median time to close: {stats['median_days_to_close']} days")


if __name__ == "__main__":
    main()
