#!/usr/bin/env python3
"""
App status fetcher for the Vibe Dashboard "Apps" tab.
=====================================================
Scans locally-cloned repos for Xcode projects and, when App Store Connect API
credentials are provided, enriches each app with its live release status
(TestFlight / in review / released / etc). Writes apps_data.json.

The repo scan works with no credentials (status falls back to "Development").
Live status needs an App Store Connect API key, supplied via env vars:
    ASC_ISSUER_ID   - the issuer ID (UUID) from App Store Connect > Integrations
    ASC_KEY_ID      - the key ID of the generated API key
    ASC_PRIVATE_KEY - contents of the downloaded .p8 private key (PEM)

Usage:
    python fetch_app_status.py --path ./repos --output apps_data.json
"""

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    requests = None

# ---------------------------------------------------------------------------
# Xcode project discovery (works offline, no Apple credentials needed)
# ---------------------------------------------------------------------------

SDKROOT_PLATFORMS = {
    "iphoneos": "iOS",
    "macosx": "macOS",
    "watchos": "watchOS",
    "appletvos": "tvOS",
    "xros": "visionOS",
}

# bundle-id suffixes that indicate a helper target, not the app itself
NON_APP_HINTS = ("test", "uitest", "widget", "watchkitextension", "notificationservice",
                 "shareextension", "intents", "clip", "tests")


def _first(pattern, text):
    m = re.search(pattern, text)
    return m.group(1).strip().strip('"') if m else None


def _pick_app_bundle_id(bundle_ids):
    """Choose the app's base bundle id from all candidates in a pbxproj.

    Excludes variable references and obvious helper targets, then prefers a
    candidate that is a prefix of the others (the base app id), falling back to
    the shortest.
    """
    candidates = []
    for b in bundle_ids:
        b = b.strip().strip('"')
        if not b or "$" in b:  # skip $(inherited) / interpolated ids
            continue
        low = b.lower()
        if any(h in low for h in NON_APP_HINTS):
            continue
        candidates.append(b)
    if not candidates:
        return None
    # a bundle id that prefixes every other candidate is the base app id
    for b in sorted(candidates, key=len):
        if all(other == b or other.startswith(b + ".") for other in candidates):
            return b
    return min(candidates, key=len)


def _read_xcconfig(path, settings, seen):
    """Merge KEY = VALUE settings from an .xcconfig into settings, following
    mandatory #include lines. Optional #include? files (typically a
    git-ignored Local.xcconfig) are skipped so results match a fresh clone.
    Conditional keys (KEY[sdk=...]) are ignored."""
    try:
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return
    for line in lines:
        line = line.split("//", 1)[0].strip()
        m = re.match(r'#include\s+"([^"]+)"', line)
        if m:
            _read_xcconfig(path.parent / m.group(1), settings, seen)
            continue
        m = re.match(r'([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?);?$', line)
        if m:
            settings[m.group(1)] = m.group(2).strip().strip('"')


def _xcconfig_settings(text, proj_dir):
    """Build settings from the .xcconfig files the project uses as base
    configurations, located by file name inside the project's folder."""
    ref_ids = set(re.findall(r"baseConfigurationReference = (\w+)", text))
    names = set()
    for rid in ref_ids:
        m = re.search(rf"{rid} /\* [^*]* \*/ = {{[^}}]*?path = \"?([^\";]+)\"?;", text)
        if m:
            names.add(Path(m.group(1)).name)
    settings = {}
    seen = set()
    for name in sorted(names):
        for f in sorted(proj_dir.rglob(name)):
            if not _skipped(f.relative_to(proj_dir)):
                _read_xcconfig(f, settings, seen)
                break
    return settings


def _expand(value, settings):
    """Substitute $(VAR) / ${VAR} from settings; unresolvable refs stay."""
    if not value:
        return value
    for _ in range(5):  # settings may reference other settings
        new = re.sub(r"\$[({]([A-Za-z_][A-Za-z0-9_]*)[)}]",
                     lambda m: settings.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


def parse_pbxproj(pbxproj_path):
    """Extract app metadata from a project.pbxproj file.

    Settings may live in the project's .xcconfig files instead of the pbxproj
    (e.g. PRODUCT_BUNDLE_IDENTIFIER = $(PREFIX).App), so those are read and
    used to fill gaps and resolve $(VAR) references.
    """
    try:
        text = pbxproj_path.read_text(errors="ignore")
    except Exception:
        return {}
    xc = _xcconfig_settings(text, pbxproj_path.parent.parent)

    bundle_ids = re.findall(r'PRODUCT_BUNDLE_IDENTIFIER = "?([^";\n]+)"?;', text)
    if xc.get("PRODUCT_BUNDLE_IDENTIFIER"):
        bundle_ids.append(xc["PRODUCT_BUNDLE_IDENTIFIER"])
    bundle_id = _pick_app_bundle_id([_expand(b.strip().strip('"'), xc) for b in bundle_ids])

    version = _expand(_first(r'MARKETING_VERSION = ([^;\n]+);', text) or xc.get("MARKETING_VERSION"), xc)
    build = _expand(_first(r'CURRENT_PROJECT_VERSION = ([^;\n]+);', text) or xc.get("CURRENT_PROJECT_VERSION"), xc)

    platforms = set()
    sdks = re.findall(r'SDKROOT = ([^;\n]+);', text) + ([xc["SDKROOT"]] if xc.get("SDKROOT") else [])
    for sdk in sdks:
        sdk = sdk.strip().strip('"').lower()
        for key, name in SDKROOT_PLATFORMS.items():
            if sdk.startswith(key):
                platforms.add(name)
    # Also honor explicit SUPPORTED_PLATFORMS
    sps = re.findall(r'SUPPORTED_PLATFORMS = "?([^";\n]+)"?;', text) + (
        [xc["SUPPORTED_PLATFORMS"]] if xc.get("SUPPORTED_PLATFORMS") else [])
    for sp in sps:
        for token in sp.split():
            for key, name in SDKROOT_PLATFORMS.items():
                if token.lower().startswith(key):
                    platforms.add(name)

    is_app = "com.apple.product-type.application" in text

    return {
        "bundle_id": bundle_id,
        "version": version if version and "$" not in version else None,
        "build": build if build and "$" not in build else None,
        "platforms": sorted(platforms),
        "is_app": is_app,
    }


# Directories never searched for projects, icons or ship signals (vendored
# dependency checkouts and build output).
SKIP_DIRS = {"Pods", "Carthage", ".build", "node_modules", "DerivedData"}


def _skipped(path):
    return any(part in SKIP_DIRS for part in path.parts)


def _manifest_fields(data):
    """Normalize one manifest object to the recognized planning keys."""
    priority = str(data.get("priority", "")).strip().lower() or None
    if priority not in (None, "high", "medium", "low"):
        priority = None
    return {
        "tagline": data.get("tagline"),
        "priority": priority,
        "target": data.get("target"),
        "next_step": data.get("next_step"),
        "blocked_on": data.get("blocked_on"),
        "bundle_id": data.get("bundle_id"),  # ASC-lookup override / multi-app matcher
        # For an app with no .xcodeproj (e.g. a SwiftPM app bundled by a
        # script): the entry declares the app itself.
        "path": data.get("path"),
        "name": data.get("name"),
        "platforms": data.get("platforms") if isinstance(data.get("platforms"), list) else None,
        "version": data.get("version"),
        "icon": data.get("icon"),
        # Browser-extension listings: {"chrome": id, "edge": id, "firefox": slug}
        "stores": data.get("stores") if isinstance(data.get("stores"), dict) else None,
        # Repo-relative WebExtension manifest whose "version" is the release
        # being shipped; a store serving an older one has an update pending.
        "extension_manifest": data.get("extension_manifest"),
    }

# Manifest keys that describe the app itself rather than planning, so they
# are not copied into the row's "manifest" block.
MANIFEST_APP_KEYS = ("bundle_id", "path", "name", "platforms", "version", "icon",
                     "stores", "extension_manifest")


def read_app_manifests(repo_dir):
    """Read an optional .dashboard-app.json of planning/intent fields that App
    Store Connect can't know: tagline, priority, target date, the next concrete
    step to ship, what it's blocked on, and a bundle_id.

    The file may be one object (one app per repo) or a list of objects (one per
    app in a multi-app repo, each matched to its app by bundle_id). Returns a
    list of normalized dicts, possibly empty.
    """
    path = repo_dir / ".dashboard-app.json"
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [_manifest_fields(d) for d in data if isinstance(d, dict)]


def match_manifests(apps_meta, manifests):
    """Pair each app with its manifest entry.

    apps_meta: list of parsed pbxproj dicts (one per app, in order).
    An entry matches the app whose parsed bundle_id equals its bundle_id
    (case-insensitive). In a single-app repo with a single entry, that entry
    applies even when its bundle_id differs or is absent, so the bundle_id
    can still override the .xcodeproj's id for ASC lookup (unless the entry
    has a "path", which marks it as declaring a separate non-Xcode app).
    Returns (per-app list of entry-or-{}, list of unmatched entries).
    """
    if len(apps_meta) == 1 and len(manifests) == 1 and not manifests[0].get("path"):
        return [manifests[0]], []
    result = [{} for _ in apps_meta]
    unmatched = []
    for entry in manifests:
        want = (entry.get("bundle_id") or "").strip().lower()
        hit = next((i for i, m in enumerate(apps_meta)
                    if want and (m.get("bundle_id") or "").lower() == want and not result[i]), None)
        if hit is None:
            unmatched.append(entry)
        else:
            result[hit] = entry
    return result, unmatched


def _owned_by(path, own_dir, foreign_dirs):
    """True if path belongs to this app: under own_dir and not under another
    app's folder nested inside it."""
    if own_dir not in path.parents and path != own_dir:
        return False
    return not any(f == path or f in path.parents for f in foreign_dirs)


def detect_ship_signals(repo_dir, proj_dir=None, foreign_dirs=()):
    """Detect repo-side 'ready to ship' signals: App Store screenshots and
    store metadata (fastlane deliver). Looks at the repo root and, when given,
    the app's own project folder; anything inside another app's project folder
    is ignored. Returns (has_screenshots, has_metadata).
    """
    roots = [repo_dir] + ([proj_dir] if proj_dir and proj_dir != repo_dir else [])
    meta_candidates, shot_candidates = [], []
    for root in roots:
        meta_candidates += [root / "fastlane" / "metadata"] + list(root.glob("*/fastlane/metadata"))
        shot_candidates += ([root / "fastlane" / "screenshots", root / "Screenshots", root / "screenshots"]
                            + list(root.glob("*/fastlane/screenshots")))

    def mine(p):
        return not _skipped(p.relative_to(repo_dir)) and _owned_by(p, repo_dir, foreign_dirs)

    has_metadata = any(p.is_dir() and mine(p) for p in meta_candidates)
    has_screenshots = any(p.is_dir() and mine(p) and next(p.rglob("*.png"), None) is not None
                          for p in shot_candidates)
    return has_screenshots, has_metadata


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "app"


def extract_app_icon(search_dir, icon_out_dir, icon_name, foreign_dirs=()):
    """Copy the app's largest AppIcon PNG into icon_out_dir/<icon_name>.png.

    Searches AppIcon.appiconset folders under search_dir, skipping any inside
    another app's project folder. The largest PNG in an AppIcon.appiconset is
    the 1024px marketing icon (or the single icon in modern single-size asset
    catalogs). Returns the web-relative path to the copied icon, or None if no
    icon is found.
    """
    appicon_dirs = [p for p in search_dir.rglob("AppIcon.appiconset")
                    if not _skipped(p.relative_to(search_dir)) and _owned_by(p, search_dir, foreign_dirs)]
    pngs = []
    for d in appicon_dirs:
        pngs.extend(d.glob("*.png"))
    if not pngs:
        return None
    return copy_icon(max(pngs, key=lambda p: p.stat().st_size), icon_out_dir, icon_name)


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_from_icns(path):
    """Return the largest PNG embedded in an .icns file, or None.

    Modern .icns elements (ic07-ic14 and friends) hold plain PNG data, so no
    macOS tooling is needed to read them (the workflow runs on Linux)."""
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if data[:4] != b"icns":
        return None
    best, pos = None, 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos + 4:pos + 8], "big")
        if length < 8:
            break
        chunk = data[pos + 8:pos + length]
        if chunk.startswith(PNG_MAGIC) and (best is None or len(chunk) > len(best)):
            best = chunk
        pos += length
    return best


def copy_icon(src, icon_out_dir, icon_name):
    """Copy a PNG (or the largest PNG inside an .icns) to
    icon_out_dir/<icon_name>.png; returns its path or None."""
    best = Path(src)
    suffix = best.suffix.lower()
    if suffix not in (".png", ".icns") or not best.is_file() or best.stat().st_size == 0:
        return None
    icon_out_dir.mkdir(parents=True, exist_ok=True)
    dest = icon_out_dir / f"{_slug(icon_name)}.png"
    try:
        if suffix == ".icns":
            png = _png_from_icns(best)
            if not png:
                return None
            dest.write_bytes(png)
        else:
            shutil.copyfile(best, dest)
    except Exception:
        return None
    return str(dest)  # e.g. "app_icons/MyApp.png" — served from the repo root


def find_app_projects(repo_dir):
    """Return [(xcodeproj_path, pbxproj_meta)] for every app project in a repo.

    Every .xcodeproj outside Pods/Carthage/.build/node_modules counts, except
    that projects whose pbxproj has no application target (frameworks,
    libraries) are dropped. If none has an application target, the shallowest
    project is kept so a repo is never silently lost. Folder names play no
    part, so projects can live anywhere in the repo.
    """
    projects = [p for p in repo_dir.rglob("*.xcodeproj")
                if not _skipped(p.relative_to(repo_dir))
                # an .xcodeproj nested in another (rare) is not its own app
                and not any(a.suffix == ".xcodeproj" for a in p.parents)]
    if not projects:
        return []
    projects.sort(key=lambda p: (len(p.parts), str(p).lower()))  # shallowest first
    parsed = [(p, parse_pbxproj(p / "project.pbxproj")) for p in projects]
    apps = [(p, m) for p, m in parsed if m.get("is_app")]
    return apps or parsed[:1]


def discover_xcode_apps(base_path, repo_meta=None, allowed_repos=None, icon_dir=None):
    """Find Xcode projects in the repos under base_path and extract app metadata.

    One row per app project, so a repo with several apps yields several rows.

    repo_meta: optional {repo_name: {"full_name":..., "url":...}} to enrich rows.
    allowed_repos: optional set of repo names to restrict discovery to (e.g. the
        repos already in dashboard_data.json, which are the owner's own,
        non-fork repos). When None, all cloned repos are scanned.
    """
    repo_meta = repo_meta or {}
    # Repo names compare case-insensitively: a local clone's folder may be
    # cased differently from the GitHub name (Celestron-Workbench vs
    # celestron-workbench).
    repo_meta_ci = {k.lower(): v for k, v in repo_meta.items() if k}
    if allowed_repos is not None:
        allowed_repos = {r.lower() for r in allowed_repos if r}
    base = Path(base_path)
    apps = []

    for repo_dir in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
            continue
        if allowed_repos is not None and repo_dir.name.lower() not in allowed_repos:
            continue  # not one of the owner's repos (co-worker's / fork)
        found = find_app_projects(repo_dir)
        manifests = read_app_manifests(repo_dir)
        matched, unmatched = match_manifests([m for _, m in found], manifests)

        # One unit per app: each .xcodeproj, plus each manifest entry that
        # declares a non-Xcode app by "path".
        units = [{"proj": proj, "dir": proj.parent, "meta": meta, "manifest": man,
                  "name": proj.stem, "xcodeproj": str(proj.relative_to(repo_dir))}
                 for (proj, meta), man in zip(found, matched)]
        for entry in unmatched:
            path = entry.get("path")
            app_dir = (repo_dir / path) if path else None
            if app_dir and app_dir.is_dir() and repo_dir.resolve() in [app_dir.resolve(), *app_dir.resolve().parents]:
                units.append({"proj": None, "dir": app_dir, "manifest": entry,
                              "name": entry.get("name") or app_dir.name, "xcodeproj": None,
                              "meta": {"bundle_id": entry.get("bundle_id"),
                                       "version": entry.get("version"),
                                       "platforms": entry.get("platforms") or []}})
            elif path:
                print(f"⚠️  {repo_dir.name}/.dashboard-app.json: path {path!r} not found; entry ignored")
            else:
                print(f"⚠️  {repo_dir.name}/.dashboard-app.json: no app with bundle_id "
                      f"{entry.get('bundle_id')!r}; entry ignored")
        if not units:
            continue
        multi = len(units) > 1
        rmeta = repo_meta.get(repo_dir.name) or repo_meta_ci.get(repo_dir.name.lower(), {})
        app_dirs = [u["dir"] for u in units]

        for u in units:
            proj_dir, meta, manifest = u["dir"], u["meta"], u["manifest"]
            # Other apps' folders nested inside this one's search area belong
            # to them, not to this app (e.g. an app at the repo root plus
            # another in a subfolder).
            foreign = [d for d in app_dirs if d != proj_dir and proj_dir in d.parents]
            if multi:
                search_dir = proj_dir
                stem = u["proj"].stem if u["proj"] else u["name"]
                icon_name = f"{repo_dir.name}-{stem}"
            else:
                # single-app repos keep the historical repo-wide search and name
                search_dir = repo_dir
                icon_name = repo_dir.name
            icon = None
            if icon_dir:
                icon = extract_app_icon(search_dir, icon_dir, icon_name, foreign)
                if not icon and manifest.get("icon"):
                    icon = copy_icon(repo_dir / manifest["icon"], icon_dir, icon_name)
            # Sibling apps' folders never count toward this app's ship signals.
            siblings = [d for d in app_dirs if d != proj_dir and d != repo_dir
                        and d not in proj_dir.parents]
            has_screenshots, has_metadata = detect_ship_signals(repo_dir, proj_dir, siblings)
            # A manifest bundle_id overrides the one parsed from project.pbxproj
            # (useful when the .xcodeproj id doesn't match the app's ASC registration).
            bundle_id = manifest.get("bundle_id") or meta.get("bundle_id")
            row = {
                "name": u["name"],  # e.g. MyApp.xcodeproj -> MyApp
                "repo": repo_dir.name,
                "full_name": rmeta.get("full_name", repo_dir.name),
                "url": rmeta.get("url", ""),
                "last_commit": rmeta.get("last_commit", ""),
                "manifest": {k: v for k, v in manifest.items() if k not in MANIFEST_APP_KEYS and v},
                "bundle_id": bundle_id,
                "version": meta.get("version"),
                "build": meta.get("build"),
                "platforms": meta.get("platforms") or [],
                "icon": icon,
                "xcodeproj": u["xcodeproj"],
                # Ship-readiness signals; asc_registered/testflight filled in later.
                "readiness": {
                    "icon": bool(icon),
                    "asc_registered": False,
                    "testflight": False,
                    "screenshots": has_screenshots,
                    "metadata": has_metadata,
                },
            }
            if u["proj"] is None:
                row["path"] = str(proj_dir.relative_to(repo_dir))
            if manifest.get("stores"):
                row["stores_decl"] = manifest["stores"]
                row["source_version"] = read_extension_version(repo_dir, manifest.get("extension_manifest"))
            apps.append(row)
    return apps


# ---------------------------------------------------------------------------
# App Store Connect API (live release status)
# ---------------------------------------------------------------------------

# Map raw App Store version states to friendly labels + a coarse category.
STORE_STATE_LABELS = {
    "READY_FOR_SALE": ("Released", "released"),
    "READY_FOR_DISTRIBUTION": ("Released", "released"),
    "ACCEPTED": ("Pending Release", "review"),
    "PROCESSING_FOR_DISTRIBUTION": ("Processing", "review"),
    "IN_REVIEW": ("In Review", "review"),
    "WAITING_FOR_REVIEW": ("Waiting for Review", "review"),
    "PENDING_DEVELOPER_RELEASE": ("Pending Release", "review"),
    "PENDING_APPLE_RELEASE": ("Pending Release", "review"),
    "PROCESSING_FOR_APP_STORE": ("Processing", "review"),
    "PREPARE_FOR_SUBMISSION": ("Preparing", "development"),
    "REJECTED": ("Rejected", "rejected"),
    "DEVELOPER_REJECTED": ("Rejected", "rejected"),
    "METADATA_REJECTED": ("Metadata Rejected", "rejected"),
    "INVALID_BINARY": ("Invalid Binary", "rejected"),
    "REMOVED_FROM_SALE": ("Removed from Sale", "removed"),
    "DEVELOPER_REMOVED_FROM_SALE": ("Removed from Sale", "removed"),
    "REPLACED_WITH_NEW_VERSION": ("Superseded", "released"),
    "WAITING_FOR_EXPORT_COMPLIANCE": ("Waiting (Export Compliance)", "review"),
}

TESTFLIGHT_ACTIVE = {"READY_FOR_BETA_TESTING", "IN_BETA_TESTING", "BETA_APPROVED"}
TESTFLIGHT_REVIEW = {"WAITING_FOR_BETA_REVIEW", "IN_BETA_REVIEW"}

# Store states at/past submission. Reaching any of these means Apple accepted
# the required screenshots + metadata, so treat those checklist items as done
# even when they aren't kept as fastlane files in the repo.
STORE_SUBMITTED_STATES = {
    "WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE",
    "PENDING_APPLE_RELEASE", "PROCESSING_FOR_APP_STORE", "READY_FOR_SALE",
    "REPLACED_WITH_NEW_VERSION", "WAITING_FOR_EXPORT_COMPLIANCE",
    "PENDING_CONTRACT", "READY_FOR_DISTRIBUTION", "ACCEPTED",
    "PROCESSING_FOR_DISTRIBUTION",
}

# A version in one of these states is the one customers can download.
STORE_LIVE_STATES = {"READY_FOR_SALE", "READY_FOR_DISTRIBUTION"}
# Old versions that are neither live nor on their way.
STORE_DEAD_STATES = {"REPLACED_WITH_NEW_VERSION", "REMOVED_FROM_SALE",
                     "DEVELOPER_REMOVED_FROM_SALE"}
ASC_PLATFORMS = {"IOS": "iOS", "MAC_OS": "macOS", "TV_OS": "tvOS", "VISION_OS": "visionOS"}


def store_label(state):
    return STORE_STATE_LABELS.get(state, (state.replace("_", " ").title(), "other"))


class ASCClient:
    BASE = "https://api.appstoreconnect.apple.com/v1"

    def __init__(self, issuer_id, key_id, private_key):
        import jwt  # PyJWT; imported lazily so the offline scan needs no crypto
        self._jwt = jwt
        self.issuer_id = issuer_id
        self.key_id = key_id
        self.private_key = private_key
        self.session = requests.Session()
        self._token = None
        self._token_exp = 0

    def _tok(self):
        now = int(time.time())
        if self._token and now < self._token_exp - 60:
            return self._token
        exp = now + 1000  # ASC allows up to 20 min; stay well under
        self._token = self._jwt.encode(
            {"iss": self.issuer_id, "iat": now, "exp": exp, "aud": "appstoreconnect-v1"},
            self.private_key, algorithm="ES256",
            headers={"kid": self.key_id, "typ": "JWT"},
        )
        self._token_exp = exp
        return self._token

    def _get(self, path, params=None):
        r = self.session.get(f"{self.BASE}{path}",
                             headers={"Authorization": f"Bearer {self._tok()}"},
                             params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def find_app(self, bundle_id):
        data = self._get("/apps", {"filter[bundleId]": bundle_id, "limit": 1}).get("data", [])
        return data[0] if data else None

    def store_state(self, app_id):
        """Live and pending App Store versions.

        An app can be live at one version while the next is in review (and
        each platform has its own versions), so this returns both:
          state/version: the headline (the live version if any, else the
              newest one in progress)
          live:    {"version", "platforms"} or None
          pending: {"version", "state", "label", "category", "platforms"} for
              the newest version newer than the live one, or None
        """
        params = {"limit": 20,
                  "fields[appStoreVersions]": "appStoreState,appVersionState,versionString,platform,createdDate"}
        data = self._get(f"/apps/{app_id}/appStoreVersions", params).get("data", [])
        if not data:
            return None
        versions = []
        for d in data:
            a = d.get("attributes") or {}
            state = a.get("appVersionState") or a.get("appStoreState")
            if state:
                versions.append({"version": a.get("versionString"), "state": state,
                                 "platform": ASC_PLATFORMS.get(a.get("platform"), a.get("platform")),
                                 "created": a.get("createdDate") or ""})
        versions.sort(key=lambda v: v["created"], reverse=True)
        live_rows = [v for v in versions if v["state"] in STORE_LIVE_STATES]
        live = None
        if live_rows:
            lv = live_rows[0]["version"]
            live = {"version": lv,
                    "platforms": sorted({v["platform"] for v in live_rows if v["version"] == lv and v["platform"]})}
        newest_live = live_rows[0]["created"] if live_rows else ""
        pend_rows = [v for v in versions if v["state"] not in STORE_LIVE_STATES | STORE_DEAD_STATES
                     and v["created"] > newest_live and (not live or v["version"] != live["version"])]
        pending = None
        if pend_rows:
            pv = pend_rows[0]
            label, category = store_label(pv["state"])
            pending = {"version": pv["version"], "state": pv["state"], "label": label, "category": category,
                       "platforms": sorted({v["platform"] for v in pend_rows if v["version"] == pv["version"] and v["platform"]})}
        head = live_rows[0] if live_rows else (pend_rows[0] if pend_rows else versions[0] if versions else None)
        if not head:
            return None
        # The same split per platform: iOS and macOS ship separately and are
        # often at different versions or stages.
        by_platform = {}
        for plat in sorted({v["platform"] for v in versions if v["platform"]}):
            rows = [v for v in versions if v["platform"] == plat]
            lr = next((v for v in rows if v["state"] in STORE_LIVE_STATES), None)
            pr = next((v for v in rows if v["state"] not in STORE_LIVE_STATES | STORE_DEAD_STATES
                       and (not lr or (v["created"] > lr["created"] and v["version"] != lr["version"]))), None)
            pend = None
            if pr:
                label, category = store_label(pr["state"])
                pend = {"version": pr["version"], "state": pr["state"], "label": label, "category": category}
            by_platform[plat] = {"live": lr["version"] if lr else None, "pending": pend}
        return {"state": head["state"], "version": head["version"], "live": live, "pending": pending,
                "by_platform": by_platform}

    def testflight_by_platform(self, app_id, platforms):
        """Latest TestFlight build per platform: {platform: {"version",
        "external", "internal"}}. platforms are display names (iOS, macOS)."""
        codes = {v: k for k, v in ASC_PLATFORMS.items()}
        out = {}
        for plat in platforms:
            code = codes.get(plat)
            if not code:
                continue
            res = self._get("/builds", {"filter[app]": app_id, "filter[preReleaseVersion.platform]": code,
                                        "sort": "-uploadedDate", "limit": 1, "include": "preReleaseVersion",
                                        "fields[preReleaseVersions]": "version,platform"})
            builds = res.get("data", [])
            if not builds:
                continue
            pre = next((i for i in res.get("included", []) if i.get("type") == "preReleaseVersions"), {})
            det = self._get(f"/builds/{builds[0]['id']}/buildBetaDetail",
                            {"fields[buildBetaDetails]": "externalBuildState,internalBuildState"})
            a = (det.get("data") or {}).get("attributes") or {}
            out[plat] = {"version": (pre.get("attributes") or {}).get("version"),
                         "external": a.get("externalBuildState"), "internal": a.get("internalBuildState")}
        return out

    def testflight_state(self, app_id):
        # Two-step to keep each request simple/well-formed: fetch the latest
        # build, then its beta detail. Use the primary /v1/builds endpoint with
        # filter[app] (the /v1/apps/{id}/builds relationship endpoint rejects
        # sort with a 400).
        builds = self._get("/builds",
                           {"filter[app]": app_id, "limit": 1, "sort": "-version"}).get("data", [])
        if not builds:
            return None
        build_id = builds[0]["id"]
        det = self._get(f"/builds/{build_id}/buildBetaDetail",
                        {"fields[buildBetaDetails]": "externalBuildState,internalBuildState"})
        a = (det.get("data") or {}).get("attributes") or {}
        return {"external": a.get("externalBuildState"), "internal": a.get("internalBuildState")}


def derive_status(store, testflight, found):
    """Collapse store + testflight signals into a single headline status."""
    if store and store.get("state"):
        label, category = store_label(store["state"])
        # If not yet submitted for sale but a build is in beta, TestFlight wins.
        if category == "development" and testflight:
            ext = testflight.get("external"); intr = testflight.get("internal")
            if ext in TESTFLIGHT_ACTIVE or intr in TESTFLIGHT_ACTIVE:
                return {"label": "TestFlight", "category": "testflight"}
            if ext in TESTFLIGHT_REVIEW:
                return {"label": "TestFlight (Beta Review)", "category": "review"}
        return {"label": label, "category": category}
    if testflight:
        ext = testflight.get("external"); intr = testflight.get("internal")
        if ext in TESTFLIGHT_ACTIVE or intr in TESTFLIGHT_ACTIVE:
            return {"label": "TestFlight", "category": "testflight"}
        if ext in TESTFLIGHT_REVIEW:
            return {"label": "TestFlight (Beta Review)", "category": "review"}
        return {"label": "TestFlight (Processing)", "category": "testflight"}
    if found:
        return {"label": "Registered", "category": "development"}
    return {"label": "Development", "category": "development"}


APPLE_PLATFORM_ORDER = ["iOS", "macOS", "visionOS", "tvOS"]


def apple_platform_cells(platforms, store, tf_by_platform, store_url):
    """One cell per Apple platform: its live App Store version and the next
    one on its way (a store version in progress, else a newer TestFlight
    build). Same cell shape as the browser grid."""
    by_plat = (store or {}).get("by_platform") or {}
    cells = {}
    for plat in sorted(platforms, key=lambda p: (APPLE_PLATFORM_ORDER.index(p)
                                                 if p in APPLE_PLATFORM_ORDER else 9, p)):
        sp = by_plat.get(plat) or {}
        live = sp.get("live")
        pending = None
        if sp.get("pending"):
            pending = {"version": sp["pending"]["version"], "label": sp["pending"]["label"]}
        tf = tf_by_platform.get(plat)
        if tf and tf.get("version") and (not live or _vkey(tf["version"]) > _vkey(live)):
            beta = "Beta Review" if tf.get("external") in TESTFLIGHT_REVIEW else "TestFlight"
            if not pending:
                pending = {"version": tf["version"], "label": beta}
            elif pending.get("label") == "Preparing":
                # A store version being prepared while the build is in beta:
                # TestFlight is the more informative stage.
                pending = {"version": tf["version"], "label": beta}
        cells[plat] = _cell(live, pending, store_url if live else None)
    return cells


def enrich_with_asc(apps, client):
    """Attach live App Store Connect status to each app (by bundle id)."""
    for app in apps:
        bundle_id = app.get("bundle_id")
        app["store"] = None
        app["testflight"] = None
        if not bundle_id:
            app["status"] = derive_status(None, None, False)
            app["status"]["note"] = "no bundle id parsed"
            continue

        try:
            asc_app = client.find_app(bundle_id)
        except Exception as e:
            app["status"] = derive_status(None, None, False)
            app["status"]["note"] = f"ASC app lookup failed: {e}"
            continue
        if not asc_app:
            app["status"] = derive_status(None, None, False)
            continue

        app_id = asc_app["id"]
        app.setdefault("readiness", {})["asc_registered"] = True
        # Prefer ASC's marketing name over the .xcodeproj filename
        asc_name = (asc_app.get("attributes") or {}).get("name")
        if asc_name:
            app["name"] = asc_name

        # Fetch store and TestFlight state independently so a failure in one
        # never discards the other (a released app must still show as Released
        # even if the TestFlight lookup hiccups).
        store = testflight = None
        notes = []
        try:
            store = client.store_state(app_id)
        except Exception as e:
            notes.append(f"store lookup failed: {e}")
        try:
            testflight = client.testflight_state(app_id)
        except Exception as e:
            notes.append(f"testflight lookup failed: {e}")

        tf_by_platform = {}
        plats = sorted(set(app.get("platforms") or []) | set((store or {}).get("by_platform", {})))
        try:
            tf_by_platform = client.testflight_by_platform(app_id, plats)
        except Exception as e:
            notes.append(f"per-platform testflight lookup failed: {e}")

        app["store"] = store
        app["testflight"] = testflight
        app["apple_platforms"] = apple_platform_cells(plats, store, tf_by_platform,
                                                      f"https://apps.apple.com/app/id{app_id}")
        r = app.setdefault("readiness", {})
        r["testflight"] = bool(testflight)
        # Submission to the store implies screenshots + metadata exist in ASC
        # (Apple requires them), regardless of whether they're mirrored as
        # fastlane files in the repo.
        if store and store.get("state") in STORE_SUBMITTED_STATES:
            r["screenshots"] = True
            r["metadata"] = True
        app["status"] = derive_status(store, testflight, True)
        if store and (store.get("live") or store.get("state") in STORE_LIVE_STATES):
            app["store"]["url"] = f"https://apps.apple.com/app/id{app_id}"
        if notes:
            app["status"]["note"] = "; ".join(notes)
    return apps


# ---------------------------------------------------------------------------
# Browser extension stores (Chrome Web Store, Edge Add-ons, addons.mozilla.org)
# ---------------------------------------------------------------------------
# Public endpoints only, so no store credentials are needed. They give the
# live version; an update counts as pending when the repo's WebExtension
# manifest is at a newer version than the store serves (submitted or about
# to be: the public listings can't tell those apart).

import urllib.error
import urllib.request

BROWSERS = ("chrome", "safari", "firefox", "edge")
CRX_ID = re.compile(r"^[a-p]{32}$")


def read_extension_version(repo_dir, rel):
    if not rel:
        return None
    try:
        with open(repo_dir / rel) as f:
            v = json.load(f).get("version")
        return str(v) if v else None
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def _vkey(v):
    """Comparable version key; trailing zeros don't count, so 1.0 == 1.0.0."""
    parts = [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", str(v or "0"))]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def _http(url, accept_json=True):
    """GET url; returns (status, body text). Network errors give (None, msg)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (vibe-dashboard store status)",
        "Accept": "application/json" if accept_json else "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception as e:
        return None, str(e)


def chrome_listing(item_id):
    url = f"https://chromewebstore.google.com/detail/{item_id}"
    status, body = _http(url, accept_json=False)
    if status == 404:
        return {"url": url, "found": False}
    m = re.search(r'Version</div><div[^>]*>\s*([0-9][^<\s]*)', body or "")
    title = re.search(r"<title>([^<]*)", body or "")
    if status == 200 and not m and title and title.group(1).strip() == "Chrome Web Store":
        # An item that isn't published yet gets the store's generic page.
        return {"url": url, "found": False}
    if status != 200 or not m:
        return {"url": url, "error": f"listing unreadable (HTTP {status})"}
    return {"url": url, "found": True, "version": m.group(1)}


def edge_listing(edge_id):
    # A 32-letter id is the extension (crx) id; anything else is the store
    # product id Partner Center shows (e.g. 0RDCKDRFVZHS).
    if CRX_ID.match(edge_id):
        api = f"https://microsoftedge.microsoft.com/addons/getproductdetailsbycrxid/{edge_id}"
    else:
        api = f"https://microsoftedge.microsoft.com/addons/getproductdetails/{edge_id}"
    status, body = _http(api)
    if status == 404:
        url = f"https://microsoftedge.microsoft.com/addons/detail/{edge_id}" if CRX_ID.match(edge_id) else None
        return {"url": url, "found": False}
    try:
        d = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"url": None, "error": f"lookup failed (HTTP {status})"}
    crx = d.get("crxId") or (edge_id if CRX_ID.match(edge_id) else None)
    return {"url": f"https://microsoftedge.microsoft.com/addons/detail/{crx}" if crx else None,
            "found": bool(d.get("version")), "version": d.get("version")}


def firefox_listing(slug):
    url = f"https://addons.mozilla.org/firefox/addon/{slug}/"
    status, body = _http(f"https://addons.mozilla.org/api/v5/addons/addon/{slug}/")
    if status in (401, 403):
        # The add-on exists but isn't public: a first listing awaiting review.
        return {"url": url, "found": False, "exists": True}
    if status == 404:
        return {"url": url, "found": False}
    try:
        d = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return {"url": url, "error": f"lookup failed (HTTP {status})"}
    cv = (d.get("current_version") or {}).get("version")
    return {"url": d.get("url") or url, "found": d.get("status") == "public" and bool(cv), "version": cv}


LOOKUPS = {"chrome": chrome_listing, "edge": edge_listing, "firefox": firefox_listing}


def _cell(live, pending, url=None, **extra):
    """One grid cell. state: live (current), update (live + newer pending),
    pending (not live yet), absent (not listed), unknown (lookup failed)."""
    if live and pending:
        state = "update"
    elif live:
        state = "live"
    elif pending:
        state = "pending"
    else:
        state = "absent"
    return {"state": state, "live": live, "pending": pending, "url": url, **extra}


def browser_cell(browser, store_id, source_version):
    try:
        info = LOOKUPS[browser](store_id)
    except Exception as e:  # never let one store break the run
        info = {"error": str(e)}
    if info.get("error"):
        return {"state": "unknown", "live": None, "pending": None, "url": info.get("url"), "note": info["error"]}
    live = info.get("version") if info.get("found") else None
    newer = source_version and (not live or _vkey(source_version) > _vkey(live))
    pending = {"version": source_version, "label": "Pending"} if newer else None
    if not live and not pending and info.get("exists"):
        pending = {"version": None, "label": "Pending"}
    return _cell(live, pending, info.get("url"))


def safari_cell(app):
    """The Safari cell comes from the app's own App Store Connect status."""
    store = app.get("store") or {}
    plats = app.get("apple_platforms") or {}
    if plats:
        # Summarize the per-platform cells: the newest live version, and the
        # most advanced pending stage (a beta beats a version being prepared).
        lives = [c["live"] for c in plats.values() if c.get("live")]
        pends = [c["pending"] for c in plats.values() if c.get("pending")]
        pends.sort(key=lambda p: p.get("label") == "Preparing")
        return _cell(max(lives, key=_vkey) if lives else None, pends[0] if pends else None,
                     store.get("url"))
    live = (store.get("live") or {}).get("version")
    p = store.get("pending")
    pending = {"version": p.get("version"), "label": p.get("label")} if p else None
    if not pending and app.get("testflight") and not live:
        pending = {"version": app.get("version"), "label": "TestFlight"}
    return _cell(live, pending, store.get("url"))


def enrich_with_browser_stores(apps):
    """Give each extension (an app whose manifest lists browser stores) a
    browsers row: chrome, safari, firefox, edge."""
    for app in apps:
        decl = app.pop("stores_decl", None)
        if not decl:
            continue
        src = app.get("source_version")
        row = {}
        for b in BROWSERS:
            if b == "safari":
                row[b] = safari_cell(app)
            elif decl.get(b):
                row[b] = browser_cell(b, str(decl[b]), src)
            else:
                row[b] = _cell(None, None)
        app["browsers"] = row
        # The headline for an extension is its best store, not Safari alone
        # (App Store Connect only knows the Safari build).
        cells = row.values()
        if any(c["live"] for c in cells):
            pend = any(c["pending"] for c in cells)
            app["status"] = {"label": "Released" + (" · update pending" if pend else ""),
                             "category": "released", "safari": app.get("status")}
        elif any(c["pending"] for c in cells):
            app["status"] = {"label": "Pending", "category": "review", "safari": app.get("status")}
        print(f"   • {app['name']} stores: " + ", ".join(
            f"{b} {c['state']}" + (f" {c['live']}" if c.get("live") else "")
            + (f"→{c['pending'].get('version')}" if c.get("pending") else "") for b, c in row.items()))
    return apps


def load_repo_meta(dashboard_data_file):
    """Pull full_name/url per repo from an existing dashboard_data.json if present."""
    meta = {}
    p = Path(dashboard_data_file)
    if p.exists():
        try:
            data = json.load(open(p))
            for proj in data.get("projects", []):
                meta[proj.get("name")] = {"full_name": proj.get("full_name", ""),
                                          "url": proj.get("url", ""),
                                          "last_commit": proj.get("last_commit", "")}
        except (json.JSONDecodeError, IOError):
            pass
    return meta


def main():
    parser = argparse.ArgumentParser(description="Fetch Xcode app inventory + App Store Connect status")
    parser.add_argument("--path", required=True, help="Directory containing cloned repos")
    parser.add_argument("--output", default="apps_data.json", help="Output JSON file")
    parser.add_argument("--dashboard-data", default="dashboard_data.json",
                        help="Existing dashboard_data.json to source repo url/full_name from")
    parser.add_argument("--icon-dir", default="app_icons",
                        help="Directory to copy each app's icon into (served from the repo root)")
    args = parser.parse_args()

    base = Path(args.path)
    if not base.exists():
        print(f"❌ Path not found: {base}")
        return 1

    repo_meta = load_repo_meta(args.dashboard_data)
    # Restrict to the owner's own repos (those in dashboard_data.json) so
    # co-workers' repos and forks with Xcode projects don't appear as "my apps".
    # If dashboard_data.json is absent/empty, fall back to scanning everything.
    allowed_repos = set(repo_meta) or None
    icon_dir = Path(args.icon_dir) if args.icon_dir else None
    apps = discover_xcode_apps(base, repo_meta, allowed_repos=allowed_repos, icon_dir=icon_dir)
    print(f"📱 Found {len(apps)} app(s) in {len({a['repo'] for a in apps})} repo(s)"
          + (f" (restricted to {len(allowed_repos)} owner repos)" if allowed_repos else ""))
    for a in apps:
        print(f"   • {a['name']} ({a['repo']}) bundle={a['bundle_id']} v{a['version']}")

    issuer = os.environ.get("ASC_ISSUER_ID")
    key_id = os.environ.get("ASC_KEY_ID")
    private_key = os.environ.get("ASC_PRIVATE_KEY")
    if issuer and key_id and private_key:
        if requests is None:
            print("⚠️  'requests' not installed; skipping App Store Connect lookup")
            for a in apps:
                a["status"] = derive_status(None, None, False)
        else:
            print("🔑 Querying App Store Connect for live release status...")
            client = ASCClient(issuer, key_id, private_key)
            enrich_with_asc(apps, client)
            for a in apps:
                print(f"   • {a['name']}: {a['status']['label']}")
    else:
        print("ℹ️  No App Store Connect credentials (ASC_ISSUER_ID/ASC_KEY_ID/ASC_PRIVATE_KEY); "
              "status defaults to Development")
        for a in apps:
            a["status"] = derive_status(None, None, False)

    if any(a.get("stores_decl") for a in apps):
        print("🧩 Checking browser extension stores...")
    enrich_with_browser_stores(apps)

    # Sort to surface the work: in-development first (most stale first, so the
    # most-languishing app is at the very top), then review/testflight, with
    # shipped apps last.
    order = {"development": 0, "review": 1, "rejected": 2, "testflight": 3,
             "released": 4, "removed": 5, "other": 6}
    prio_rank = {"high": 0, "medium": 1, "low": 2}

    def sort_key(a):
        cat = a.get("status", {}).get("category", "other")
        prio = prio_rank.get((a.get("manifest") or {}).get("priority"), 1)
        # within a status group: manifest priority, then most-stale first
        return (order.get(cat, 9), prio, a.get("last_commit") or "9999", a["name"].lower())

    apps.sort(key=sort_key)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(apps),
        "apps": apps,
    }
    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n✅ Wrote {args.output} ({len(apps)} apps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
