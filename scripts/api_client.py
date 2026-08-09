#!/usr/bin/env python3
"""
The single point of contact with the CourtListener API. SPEC.md section 8
requires that *every* API call in this project go through one throttled
function with no bypass path -- so when a second consumer appeared
(opinion_fetcher.py alongside collector.py), the rate limiter moved here
rather than being duplicated.

Both scripts share one daily request budget via the same checkpoint file,
which is what keeps the combined total under CourtListener's real daily
limit no matter how the work is split between them.

No extraction or business logic lives here -- only transport and pacing.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

API_ROOT = "https://www.courtlistener.com/api/rest/v4"
SEARCH_ENDPOINT = f"{API_ROOT}/search/"
DOCKET_ENTRIES_ENDPOINT = f"{API_ROOT}/docket-entries/"
OPINIONS_ENDPOINT = f"{API_ROOT}/opinions/"
CLUSTERS_ENDPOINT = f"{API_ROOT}/clusters/"
RECAP_DOCUMENTS_ENDPOINT = f"{API_ROOT}/recap-documents/"

# ---------------------------------------------------------------------------
# SPEC.md section 8 -- rate limiting
# ---------------------------------------------------------------------------

FLOOR_THROTTLE_SECONDS = 13
DAILY_REQUEST_CAP = 120
MAX_BACKOFF_SECONDS = 1800  # give up cleanly rather than sleep through a huge
                            # Retry-After; a later run (every 4h) retries

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

# Both consumers persist to this same file, which is how they share one
# daily budget. Each module holds its own module-level path constant so
# tests can redirect it to a temp dir; this is only the default.
DEFAULT_CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"

EMPTY_CHECKPOINT = {
    "last_search_offset": 0,
    "processed_docket_ids": [],
    "requests_made_today": 0,
    "date_of_counter": None,
}


def get_headers():
    """Token is read lazily, not at import time, so this module can be
    imported by tests without a real token in the environment."""
    token = os.environ.get("COURTLISTENER_TOKEN")
    if not token:
        print("FATAL: COURTLISTENER_TOKEN environment variable not set.", file=sys.stderr)
        sys.exit(1)
    return {"Authorization": f"Token {token}"}


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def throttled_get(url, params, checkpoint):
    """The single choke point. Returns parsed JSON, or None to signal
    "stop cleanly and let a later run resume" (daily cap reached, or a
    Retry-After too long to be worth sleeping through)."""
    today = datetime.now(timezone.utc).date().isoformat()
    if checkpoint["date_of_counter"] != today:
        checkpoint["date_of_counter"] = today
        checkpoint["requests_made_today"] = 0

    if checkpoint["requests_made_today"] >= DAILY_REQUEST_CAP:
        return None

    time.sleep(FLOOR_THROTTLE_SECONDS)
    resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
    checkpoint["requests_made_today"] += 1

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        if retry_after > MAX_BACKOFF_SECONDS:
            print(f"429 received, Retry-After={retry_after}s exceeds max tolerated backoff "
                  f"({MAX_BACKOFF_SECONDS}s) -- stopping cleanly, a later run will retry")
            return None
        print(f"429 received, backing off {retry_after}s")
        time.sleep(retry_after)
        return throttled_get(url, params, checkpoint)

    resp.raise_for_status()
    return resp.json()
