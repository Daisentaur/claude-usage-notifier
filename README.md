# Claude Usage Notifier

A small personal tool for Ubuntu/Linux that fetches your claude.ai usage stats and shows them in a desktop notification with the press of a keyboard shortcut.

```
   5-hour   ▓▓▓▓░░░░░░░░  33.0%  · resets 4:10 AM
   7-day    ▓░░░░░░░░░░░   8.0%  · resets Sun 8:30 PM
```

## Why this exists

Claude.ai's settings page shows your rolling 5-hour and 7-day usage, but checking it means opening a browser tab, navigating to settings,changing over to the usage tab, and waiting for the page to load. This script reduces that to a single keyboard shortcut, with the data popping up as a system notification.

It's intentionally read-only and personal-use. It reads your existing browser session — no API keys, no second login, no separate auth flow.

## What it does, end to end

1. Reads your claude.ai session cookies directly from Firefox's local cookie database (`cookies.sqlite`).
2. Extracts your organization UUID from the `lastActiveOrg` cookie.
3. Calls `https://claude.ai/api/organizations/<uuid>/usage` — the same endpoint the settings page calls under the hood.
4. Bypasses Cloudflare's bot detection by using `curl_cffi` to mimic a real Firefox's TLS handshake.
5. Caches the response so spamming the shortcut doesn't hammer the API.
6. Falls back to stale cached data if the network request fails, marking it as stale in the notification.
7. Formats the data with progress bars and reset times.
8. Shows it via `notify-send` (the standard Linux desktop notification tool).
9. Logs everything to `~/.cache/claude_usage.log` for debugging.

## Project structure

```
claude_usage_notifier/
├── claude_usage.py        # the main script
├── config.json            # user-editable settings
├── requirements.txt       # pinned Python dependencies
├── .gitignore
├── README.md              # this file
└── venv/                  # local virtualenv (gitignored)
```

Runtime files (auto-created, not in the repo):

```
~/.cache/claude_usage.log           # debug log
~/.cache/claude_usage_cache.json    # cached API response
```

## How it works in detail

### The endpoint we hit

`GET https://claude.ai/api/organizations/<org-uuid>/usage` returns a JSON blob like:

```json
{
  "five_hour":  {"utilization": 33.0, "resets_at": "2026-05-19T22:40:00Z"},
  "seven_day":  {"utilization":  8.0, "resets_at": "2026-05-24T15:00:00Z"},
  "seven_day_opus":   null,
  "seven_day_sonnet": null,
  "extra_usage": { ... },
  ...
}
```

`utilization` is a percentage (0–100). `resets_at` is when the rolling window expires.

The endpoint also returns various internal fields with Anthropic codenames (`tangelo`, `iguana_necktie`, `seven_day_omelette`, etc.) which i ignored for this projet.

### Why cookies, not an API key

claude.ai's web app doesn't expose a public API for chat usage stats. The Admin API (at `console.anthropic.com`) tracks API consumption, but that's a separate billing surface — it doesn't cover Pro plan chat usage.

So the only path to this data from a script is to reuse the same session that your logged-in browser already has. So I just tried to "do what the browser does, but from Python."

### Which cookies actually matter

A claude.ai session has ~20+ cookies. Most are for analytics, Stripe billing, and Intercom chat — irrelevant to API auth. The ones that matter:

| Cookie | What it's for | What happens without it |
|---|---|---|
| `sessionKey` | Anthropic's auth token. Functionally equivalent to your password. | `HTTP 401 Unauthorized` |
| `cf_clearance` | Cloudflare's "this client is human" cookie. Tied to your IP and User-Agent. | `HTTP 403` with a Cloudflare challenge page |
| `lastActiveOrg` | Your org UUID — needed to construct the URL. | Can't form the request |
| `__cf_bm` | Cloudflare bot management cookie. | Possible Cloudflare challenge |

We pass the *whole* cookie jar to `requests` and let it figure out which to send. We don't need to pluck individual cookies. We *do* manually read `lastActiveOrg` from the jar to grab the org UUID.

### Why `curl_cffi` instead of `requests`

When I first tried this with plain `requests`, Cloudflare returned a 403 with a "Just a moment..." challenge page — even though the cookies and User-Agent header were correct after a quick check.

Apparently, Cloudflare doesn't just inspect headers. It also fingerprints the **TLS handshake** itself (a technique often called **JA3 fingerprinting** I got to know). Python's `requests` library uses OpenSSL with a cipher suite order and TLS extension list that's clearly *not* a real browser. Cloudflare sees the handshake shape and rejects us regardless of what's in our headers.

`curl_cffi` is a drop-in replacement for `requests` that uses libcurl under the hood, with the ability to impersonate real browsers' TLS fingerprints:

```python
from curl_cffi import requests
requests.get(url, ..., impersonate="firefox133")
```

That one argument makes the handshake look like Firefox 133 (close enough to current Firefox that Cloudflare accepts it). Same `.get()` API, same response object — only the lower-layer TLS behavior changes.

This is increasingly the standard approach I learned for "scrape your own data from sites behind Cloudflare." It's not a vulnerability btw it's just doing the same TLS handshake your browser already does.

### Stale-while-error caching

The fetch flow has three layers of fallback:

1. **Fresh cache exists?** Use it. (Unless `--force` is passed.)
2. **No fresh cache?** Try a live fetch. On success, write to cache.
3. **Live fetch failed?** Fall back to *any* cached data, even if expired. Mark it as stale in the notification ("⚠️ Stale data (12 min old)").
4. **No cache at all and fetch failed?** Show an error notification with actionable guidance.

This means temporary network blips or Cloudflare hiccups don't leave you without info — you see slightly stale data with a clear stale marker. 

### Why a separate config file

Two reasons:

1. **Editability without code changes.** Tweaking a threshold or notification timeout shouldn't require editing the script.
2. **Forward compatibility.** When new config keys get added (a future feature), the loader merges them with defaults — your existing `config.json` keeps working without modification.

Current config options:

| Key | Default | Purpose |
|---|---|---|
| `notification_timeout_ms` | `8000` | How long the notification stays visible (ms) |
| `cache_ttl_seconds` | `300` | Fresh-cache lifetime; below this, no API call |
| `warning_threshold` | `80` | Show ⚠️ on lines with utilization ≥ this percentage |
| `show_progress_bar` | `true` | Toggle the Unicode bar in the notification |
| `bar_width` | `12` | Bar width in characters |
| `tls_impersonate` | `"firefox133"` | Browser fingerprint to mimic (see curl_cffi docs for options) |

## Setup

### Prerequisites

- Ubuntu (tested on 24.04). Should work on any modern Linux with libnotify.
- Python 3.10+
- Firefox, logged into claude.ai

### Install

```bash
git clone https://github.com/Daisentaur/claude-usage-notifier
cd claude_usage_notifier
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Test

```bash
python3 claude_usage.py
```

You should see usage stats both in your terminal and as a desktop notification.

### Keyboard shortcut (GNOME)

GNOME shortcuts run in a minimal shell environment, so the command must use the venv's Python explicitly (not just `python3`).

Find the absolute paths:

```bash
realpath venv/bin/python
realpath claude_usage.py
```

Then in **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts → +**:

- **Name:** `Claude Usage`
- **Command:** `/full/path/to/venv/bin/python /full/path/to/claude_usage.py`
- **Shortcut:** something like `Ctrl+Shift+U`

Test by pressing the shortcut from any app.

## Usage

```bash
python3 claude_usage.py           # normal run (uses cache if fresh)
python3 claude_usage.py --force   # bypass cache, force a fresh API call
```

The keyboard shortcut runs the normal version (no flag) — fast, polite to the API, and shows fresh data within `cache_ttl_seconds` of the last fetch.

Use `--force` from the terminal when you want guaranteed-current numbers.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Not logged in. Open claude.ai in Firefox.` | Cookie store has no claude.ai session. | Log into claude.ai in Firefox. |
| `Session expired. Log into claude.ai in Firefox.` | `sessionKey` rejected (HTTP 401). | Same — load claude.ai in Firefox and log in. |
| `Cloudflare block. Reload claude.ai in Firefox.` | `cf_clearance` expired (HTTP 403). | Open claude.ai in Firefox to refresh the cookie, then retry. |
| Stale-data notification appears | Last fetch failed; you're seeing cached data. | Usually transient — try `--force` or check `~/.cache/claude_usage.log`. |
| Shortcut does nothing | Wrong path in shortcut command, or notifications disabled. | Check `~/.cache/claude_usage.log` to see if the script even ran. |

### Reading the log

```bash
tail -f ~/.cache/claude_usage.log
```

Every run is bracketed by `--- Run start ---` markers and notes cache hits, fetches, errors, and tracebacks.

## Limitations and known caveats

- **Linux + Firefox only.** Mac/Windows and other browsers are not supported (yet — see Roadmap below :p).
- **Pro plan only (probably).** Tested on a Pro subscription. Free, Team, and Enterprise plans may surface different fields.
- **Relies on a private API.** This endpoint isn't documented. Anthropic could rename or restructure it any time, breaking this script. The fix when that happens is to redo the DevTools reconnaissance done initially to find `https://claude.ai/api/organizations/<org-uuid>/usage` endpoint.
- **Session-bound.** When the browser session expires (weeks-to-months of not opening claude.ai), the script breaks until you log in again.

## Roadmap

### Phase 1 (done)

- Working script with cache, config, logging, stale-while-error, progress bars, warning thresholds
- GNOME keyboard shortcut
- README

### Phase 2 — Cross-browser on Linux

- Add `browser` to config (`firefox` / `chrome` / `chromium` / `brave` / `edge`)
- Branch the `browser_cookie3` call accordingly
- Auto-select an appropriate `tls_impersonate` value per browser
- Test against Chromium and Chrome on Linux

### Phase 3 — macOS and Windows

- Replace direct `notify-send` calls with a platform-dispatching `notify()`:
  - Linux: `notify-send`
  - macOS: `osascript -e 'display notification ...'` or `terminal-notifier`
  - Windows: PowerShell + `BurntToast`, or `win10toast` Python library
- Document keyboard-shortcut setup per OS (Automator on macOS, AutoHotkey on Windows)
- Test browser cookie reading on each platform (Safari's binary cookie format is scaring me to go through with all this lol)

### Phase 4 — Plugin model for other AI services

A clean separation between *service-specific* logic and *general* infrastructure (cache, notification, config, error handling).

Each service plugin would be a small Python module exporting:

```python
def fetch(cookies) -> CommonUsageShape: ...
```

Where `CommonUsageShape` is something like:

```python
{
  "buckets": [
    {"label": "Weekly", "pct": 47.0, "resets_at": "2026-05-24T15:00:00Z"},
    ...
  ]
}
```

Then the main script does cache/notify/log around any plugin, and users pick which service(s) to track via config. The repo would ship with a `claude.py` plugin (essentially this script's `fetch_usage` extracted), and contributors could add `chatgpt.py`, `gemini.py`, etc.

The original instinct was "let users paste any AI service's usage URL and have it work." That's not actually feasible without an AI parsing the page in real time — every service has a different endpoint structure, JSON shape, and auth flow. Reconnaissance has to happen once per service, by a human (or a plugin author). The plugin model is the realistic version of that goal.

## Security notes

- The session cookie this script reads is functionally a password. The script never transmits it anywhere except to `claude.ai`. It never writes it to logs or the cache file.
- The cache file contains your usage numbers and reset times — nothing sensitive, but `~/.cache` is your home directory either way.
- If you fork this and add features, **never log cookie values** and **never include them in error messages** that might get pasted into chats or GitHub issues.

## License

MIT
