# App repo manifest — `.dashboard-app.json`

The dashboard's **Apps** tab pulls objective status (review state, TestFlight,
screenshots/metadata) from **App Store Connect** automatically. It cannot know
your *plans*, though — so each app repo may carry a small `.dashboard-app.json`
at its root with planning/intent fields the dashboard renders on the app card.

All fields are optional; include only what's useful.

| Field | Type | Shown as |
|-------|------|----------|
| `tagline` | string | Subtitle under the app name (replaces the bundle id) |
| `priority` | `"high"` \| `"medium"` \| `"low"` | A pill; also sorts higher-priority apps first |
| `target` | `"YYYY-MM"` or `"YYYY-MM-DD"` | 🎯 target date with a live countdown ("~8 wks left", "overdue") |
| `next_step` | string | The single next concrete action to ship (green "→" line) |
| `blocked_on` | string | What's blocking progress (amber "⚠" line) |
| `bundle_id` | string | Overrides the `.xcodeproj` bundle id for ASC lookup (use if they differ) |

### Example

```json
{
  "tagline": "Ice-time queue manager for rinks",
  "priority": "high",
  "target": "2026-09",
  "next_step": "Record 3 iPhone screenshots, then submit for review",
  "blocked_on": "waiting on final icon from designer"
}
```

### Repos with more than one app

The Apps tab shows one card per `.xcodeproj` (Pods, Carthage, `.build` and
`node_modules` are skipped; projects with no application target, such as
frameworks, are dropped). Folder names don't matter, so projects can be moved
or renamed freely. Each app gets its own icon (from the `AppIcon.appiconset`
inside that project's folder) and its own screenshot/metadata check (the
project's folder plus the repo root).

To give each app its own planning fields, make `.dashboard-app.json` a **list**
of objects. Each entry is matched to its app by `bundle_id`, which must equal
the bundle id the `.xcodeproj` builds (settings in the project's `.xcconfig`
files are understood). An entry whose `bundle_id` matches no app is reported
and ignored; an app with no entry simply shows no planning fields.

```json
[
  {
    "bundle_id": "org.example.reader",
    "tagline": "Safari extension for readers",
    "priority": "high",
    "next_step": "Submit 1.2 for review"
  },
  {
    "bundle_id": "org.example.reader-pro",
    "tagline": "The paid edition",
    "blocked_on": "pricing decision"
  }
]
```

In a single-app repo a one-object file (or a one-entry list) always applies to
that app, and there its `bundle_id` overrides the project's for the App Store
Connect lookup. In a multi-app repo `bundle_id` only matches, so the
`.xcodeproj` id must be the one registered in App Store Connect.

### Apps without an `.xcodeproj`

An app built some other way (say a Swift package bundled into a `.app` by a
script) can't be found by scanning. Declare it with an entry that has a
`path`; it then gets a card of its own:

| Field | Type | Meaning |
|-------|------|---------|
| `path` | string | Repo-relative folder holding the app (required; marks the entry as declaring an app) |
| `name` | string | Card title until App Store Connect supplies one (default: the folder name) |
| `bundle_id` | string | Used for the App Store Connect lookup |
| `platforms` | list of strings | e.g. `["macOS"]` |
| `version` | string | Marketing version |
| `icon` | string | Repo-relative `.png` or `.icns`, used if no `AppIcon.appiconset` is under `path` |

```json
{
  "name": "Moon Mosaic",
  "path": "macos/MoonMosaic",
  "bundle_id": "com.example.MoonMosaic",
  "platforms": ["macOS"],
  "version": "0.1.0",
  "icon": "macos/MoonMosaic/Icon/AppIcon.icns"
}
```

Unlike Xcode apps, this one depends on its folder name: update `path` if the
folder moves.

### Browser extensions

For a Safari web extension that also ships to other browsers, list its store
listings and the card gets a Chrome / Safari / Firefox / Edge grid showing
each store's live version and any update on its way:

| Field | Type | Meaning |
|-------|------|---------|
| `stores` | object | `chrome`: Chrome Web Store item id; `edge`: the 32-letter extension id or the Partner Center store id (e.g. `0RDCKDRFVZHS`); `firefox`: the addons.mozilla.org slug or add-on id. Leave a browser out if it isn't submitted there. |
| `extension_manifest` | string | Repo-relative WebExtension `manifest.json` whose `version` is the release being shipped |

```json
{
  "bundle_id": "org.example.reader",
  "extension_manifest": "manifest.json",
  "stores": {
    "chrome": "abcdefghijklmnopabcdefghijklmnop",
    "edge": "0RDCKDRFVZHS",
    "firefox": "example-reader"
  }
}
```

Safari's cell comes from App Store Connect like any other app (and when the
Safari build ships on several platforms, a Safari iOS / macOS row follows). The other
three use the stores' public listings, so no store credentials are needed. Each
cell shows the live version, and the repo's version marked *pending* when the
store serves an older one (or nothing yet). The public listings can't tell
"in review" from "not yet submitted", so only list a store once you've
submitted to it. The card's status pill is *Released* once any store is live.

Do **not** track objective status here (review state, whether screenshots exist,
etc.) — that comes from App Store Connect. Keep this to planning fields only.

---

## Snippet to paste into each app repo's `CLAUDE.md`

```markdown
## Dashboard manifest (`.dashboard-app.json`)

This repo is tracked by the Vibe Dashboard's Apps tab. Keep a
`.dashboard-app.json` file at the repo root current so the dashboard shows
accurate planning info, and update it whenever the plan changes — a new next
step, the target shifts, priority changes, or it becomes blocked/unblocked.

Optional fields: `tagline` (one-line description), `priority`
("high"/"medium"/"low"), `target` (ship date "YYYY-MM" or "YYYY-MM-DD"),
`next_step` (the single next action to ship), `blocked_on` (what's blocking),
`bundle_id` (only if the .xcodeproj bundle id differs from the App Store
Connect registration).

If this repo holds more than one app, the file is a JSON list with one object
per app, each carrying the app's `bundle_id` so the dashboard can match it.
An app with no `.xcodeproj` needs `path`, `name`, `bundle_id` and `platforms`
in its entry. See the Vibe Dashboard's `APP_MANIFEST.md` for details.

Objective status — review state, TestFlight, screenshots/metadata — comes from
App Store Connect automatically; do not track those here.
```
