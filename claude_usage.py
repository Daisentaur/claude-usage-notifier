#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import browser_cookie3
from curl_cffi import requests

# --- Paths ---------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = SCRIPT_DIR / "config.json"

CACHE_DIR = Path.home() / ".cache"
CACHE_PATH = CACHE_DIR / "claude_usage_cache.json"
LOG_PATH = CACHE_DIR / "claude_usage.log"


# --- API constants -------------------------------------------------------

USAGE_URL = "https://claude.ai/api/organizations/{org}/usage"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
    "Accept": "*/*",
    "Referer": "https://claude.ai/settings/usage",
    "anthropic-client-platform": "web_claude_ai",
}


# --- Default config ------------------------------------------------------
# Used to seed config.json on first run if it doesn't exist yet

DEFAULT_CONFIG = {
    "notification_timeout_ms": 8000,
    "cache_ttl_seconds": 300,
    "warning_threshold": 80,
    "show_progress_bar": True,
    "bar_width": 12,
    "tls_impersonate": "firefox133",
}


# --- Config & logging setup ---------------------------------------------


def load_config():
    """Load config.json, creating it with defaults if it doesn't exist."""
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2))
        logging.info(f"Created default config at {CONFIG_PATH}")
        return DEFAULT_CONFIG

    try:
        config = json.loads(CONFIG_PATH.read_text())
        # Fill in any missing keys from defaults (so adding new keys later doesn't break old configs).
        merged = {**DEFAULT_CONFIG, **config}
        return merged
    except json.JSONDecodeError as e:
        logging.warning(f"Config file is invalid JSON: {e}. Using defaults.")
        return DEFAULT_CONFIG


def setup_logging():
    """Configure logging to ~/.cache/claude_usage.log. Truncate if it gets too big."""
    CACHE_DIR.mkdir(exist_ok=True)

    # Simple log rotation: if the file is over 1 MB, start fresh.
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 1_000_000:
        LOG_PATH.unlink()

    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# --- Cache helpers -------------------------------------------------------


def read_cache():
    """Return (data, age_seconds) from cache, or (None, None) if no cache exists."""
    if not CACHE_PATH.exists():
        return None, None
    try:
        cached = json.loads(CACHE_PATH.read_text())
        age = (
            datetime.now(timezone.utc) - datetime.fromisoformat(cached["fetched_at"])
        ).total_seconds()
        return cached["data"], age
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logging.warning(f"Cache file unreadable: {e}")
        return None, None


def write_cache(data):
    """Save data to cache with current timestamp."""
    CACHE_DIR.mkdir(exist_ok=True)
    payload = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }
    CACHE_PATH.write_text(json.dumps(payload, indent=2))


# --- Cookie & request handling ------------------------------------------


def get_cookies_and_org():
    """Pull claude.ai cookies from Firefox. Return (jar, org_uuid)."""
    jar = browser_cookie3.firefox(domain_name="claude.ai")

    org_uuid = None
    for cookie in jar:
        if cookie.name == "lastActiveOrg":
            org_uuid = cookie.value
            break

    if not org_uuid:
        raise RuntimeError("Not logged in. Open claude.ai in Firefox.")

    return jar, org_uuid


def fetch_usage(config):
    """Hit the usage endpoint. Returns parsed JSON. Raises on failure."""
    jar, org_uuid = get_cookies_and_org()
    url = USAGE_URL.format(org=org_uuid)

    logging.info(f"Fetching {url}")
    response = requests.get(
        url,
        headers=HEADERS,
        cookies=jar,
        timeout=10,
        impersonate=config["tls_impersonate"],
    )

    if response.status_code == 401:
        raise RuntimeError("Session expired. Log into claude.ai in Firefox.")
    if response.status_code == 403:
        raise RuntimeError("Cloudflare block. Reload claude.ai in Firefox.")
    if response.status_code != 200:
        raise RuntimeError(f"HTTP {response.status_code}")

    return response.json()


# --- Formatting ----------------------------------------------------------


def format_reset_time(iso_string):
    """Turn a UTC ISO timestamp into a friendly local-time string."""
    if not iso_string:
        return "—"
    dt_local = datetime.fromisoformat(iso_string).astimezone()
    now = datetime.now(timezone.utc).astimezone()
    hours = (dt_local - now).total_seconds() / 3600

    if hours < 24:
        return dt_local.strftime("%-I:%M %p")
    return dt_local.strftime("%a %-I:%M %p")


def progress_bar(pct, width):
    """Render a Unicode progress bar. e.g. ▓▓▓░░░░░░░ for 30% at width 10."""
    filled = round((pct / 100) * width)
    return "▓" * filled + "░" * (width - filled)


def format_line(label, bucket, config):
    """Format one usage line, with optional bar and warning marker."""
    pct = bucket["utilization"]
    reset = format_reset_time(bucket.get("resets_at"))

    warn = "⚠️ " if pct >= config["warning_threshold"] else "   "

    if config["show_progress_bar"]:
        bar = progress_bar(pct, config["bar_width"])
        return f"{warn}{label:8} {bar} {pct:>5.1f}%  · resets {reset}"
    return f"{warn}{label:8} {pct:>5.1f}%  · resets {reset}"


def build_message(data, config, stale_age_seconds=None):
    """Format the JSON into a notification body."""
    lines = []

    if stale_age_seconds is not None:
        minutes = int(stale_age_seconds // 60)
        lines.append(f"⚠️  Stale data ({minutes} min old)")
        lines.append("")

    if data.get("five_hour"):
        lines.append(format_line("5-hour", data["five_hour"], config))
    if data.get("seven_day"):
        lines.append(format_line("7-day", data["seven_day"], config))

    # Model-specific buckets — only if populated.
    for key, label in [("seven_day_opus", "  Opus"), ("seven_day_sonnet", "  Sonnet")]:
        bucket = data.get(key)
        if bucket and bucket.get("utilization") is not None:
            lines.append(format_line(label, bucket, config))

    return "\n".join(lines) if lines else "No usage data."


# --- Notifications -------------------------------------------------------


def notify(title, body, config, urgency="normal"):
    """Show a desktop notification."""
    subprocess.run(
        [
            "notify-send",
            "--app-name=Claude Usage",
            "--icon=dialog-information",
            f"--expire-time={config['notification_timeout_ms']}",
            f"--urgency={urgency}",
            title,
            body,
        ],
        check=False,
    )


def notify_error(message, config):
    notify("⚠️ Claude Usage", message, config, urgency="critical") # Putting urgency=critical makes the notification visible for longer


# --- Main flow -----------------------------------------------------------


def get_usage_data(config, force_refresh):
    cached_data, cached_age = read_cache()

    # Use fresh cache if not forcing and within TTL.
    if (
        not force_refresh
        and cached_data is not None
        and cached_age < config["cache_ttl_seconds"]
    ):
        logging.info(f"Cache hit (age {cached_age:.0f}s)")
        return cached_data, None

    # Try a fresh fetch.
    try:
        data = fetch_usage(config)
        write_cache(data)
        logging.info("Fetch successful, cache updated")
        return data, None
    except Exception as fetch_error:
        logging.warning(f"Fetch failed: {fetch_error}")
        # Fall back to any cached data, even if expired.
        if cached_data is not None:
            logging.info(f"Falling back to stale cache (age {cached_age:.0f}s)")
            return cached_data, cached_age
        # No cache to fall back on — re-raise.
        raise


def main():
    parser = argparse.ArgumentParser(
        description="Show claude.ai usage in a notification."
    )
    parser.add_argument(
        "--force", action="store_true", help="Bypass cache, fetch fresh."
    )
    args = parser.parse_args()

    setup_logging()
    config = load_config()

    logging.info(f"--- Run start (force={args.force}) ---")

    try:
        data, stale_age = get_usage_data(config, force_refresh=args.force)
        message = build_message(data, config, stale_age_seconds=stale_age)
        print(message)
        notify("Claude Usage", message, config)
    except Exception as e:
        logging.error(f"Fatal: {e}\n{traceback.format_exc()}")
        # Make sure the user sees something even when triggered via shortcut.
        notify_error(str(e), config)
        sys.exit(1)


if __name__ == "__main__":
    main()
