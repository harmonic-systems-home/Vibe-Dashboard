# Vibe Dashboard

A development activity dashboard that visualizes commit history, lines of code, language distribution, and releases across GitHub repositories.

## Deployment

The dashboard is deployed via **GitHub Pages** with automated daily updates via **GitHub Actions**.

### GitHub Actions Workflow

The workflow (`.github/workflows/update-dashboard.yml`) runs:
- **Daily at 4 AM Pacific** (12 PM UTC)
- **On push to main branch**
- **Manually via workflow_dispatch**

The workflow:
1. Clones all non-archived repos across the personal account and the two orgs (Rick-Wilson, bridge-craftwork, harmonic-systems-home); forks are cloned but flagged so their LOC is excluded
2. Installs tokei for lines of code counting
3. Runs `fetch_github_data.py` to generate `dashboard_data.json`
4. Runs `fetch_issues.py` to generate `issues_data.json` (open/closed issues from public repos)
5. Commits and pushes the updated JSON files

### Live URL

Once GitHub Pages is enabled, the dashboard will be available at:
`https://rick-wilson.github.io/Vibe-Dashboard/`

## Running Locally

### Prerequisites

- Python 3.x
- tokei (for LOC counting): `brew install tokei` or `cargo install tokei`

### Quick Start

1. **Install dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install python-dateutil requests
   ```

2. **Generate dashboard data:**
   ```bash
   python fetch_github_data.py --local --path /path/to/your/repos --owner YourGitHubUsername --author "Your Name"
   ```

3. **Start local server:**
   ```bash
   python3 -m http.server 8000
   ```

4. **Open in browser:**
   http://localhost:8000

### Script Options

```bash
python fetch_github_data.py --local --path /path/to/repos    # Scan local repos
python fetch_github_data.py --user username                   # Fetch via GitHub API
python fetch_github_data.py --repos owner/repo1 owner/repo2   # Specific repos

# Filters (for local mode)
--author "Name"     # Filter commits by author
--owner username    # Only repos owned by this user
--output file.json  # Output file (default: dashboard_data.json)
```

### Issues Data

The Issues tab needs `issues_data.json`, which comes from the GitHub API (git
clones carry no issue data):

```bash
export GITHUB_TOKEN=$(gh auth token)
python fetch_issues.py --owners Rick-Wilson bridge-craftwork harmonic-systems-home
```

Public, non-archived repos only. By default it is further restricted to the
repos already in `dashboard_data.json` so the tab matches the rest of the
dashboard; pass `--dashboard-data ''` to scan every public repo instead, or
`--include-private` / `--include-archived` to widen the net.

## Data Sources

- **Issues**: GitHub REST API, public repos only, scoped to the repos in `dashboard_data.json`
- **Commits**: Extracted from git log
- **Lines of Code**: Counted by tokei (excludes HTML)
- **Releases**: Git tags with dates
- **Language Distribution**: From tokei analysis

## Key Files

- `index.html` - Dashboard UI (single-page app)
- `fetch_github_data.py` - Data collection script
- `dashboard_data.json` - Generated data file
- `fetch_issues.py` - Issues tab: pulls open/closed issues from the public repos of each owner (needs `GITHUB_TOKEN`)
- `issues_data.json` - Generated Issues tab data
- `fetch_app_status.py` - Apps tab: scans repos for Xcode projects + pulls live App Store Connect status (needs `ASC_*` secrets)
- `apps_data.json` - Generated Apps tab data
- `APP_MANIFEST.md` - `.dashboard-app.json` schema for per-app-repo planning breadcrumbs
- `.github/workflows/update-dashboard.yml` - Automation workflow
