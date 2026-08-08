#!/usr/bin/env python3
"""
Orchestration layer. Implements SPEC.md sections 4 (architecture), 5 (data
source/scope), and 8 (rate limiting).

Deliberately thin: all field-derivation logic lives in extraction.py and is
unit-tested there. This file's only job is talking to the network, pacing
requests, and calling into that tested logic.

USAGE
  pip install -r requirements.txt
  export COURTLISTENER_TOKEN="..."
  python scripts/collector.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(__file__))
from extraction import build_case_record, validate_record

# ---------------------------------------------------------------------------
# SPEC.md section 5 — data source and scope
# ---------------------------------------------------------------------------

API_ROOT = "https://www.courtlistener.com/api/rest/v4"
SEARCH_ENDPOINT = f"{API_ROOT}/search/"
DOCKET_ENTRIES_ENDPOINT = f"{API_ROOT}/docket-entries/"

SEARCH_QUERY = (
    '("writ of mandamus" OR "mandamus" OR "unreasonable delay") '
    'AND ("221(g)" OR "administrative processing" OR "consular") '
    'AND ("visa")'
)
FILED_AFTER = "2020-01-01"

# ---------------------------------------------------------------------------
# SPEC.md section 8 — rate limiting (single choke point, nothing bypasses it)
# ---------------------------------------------------------------------------

FLOOR_THROTTLE_SECONDS = 13
DAILY_REQUEST_CAP = 120

TOKEN = os.environ.get("COURTLISTENER_TOKEN")
if not TOKEN:
    print("FATAL: COURTLISTENER_TOKEN environment variable not set.", file=sys.stderr)
    sys.exit(1)
HEADERS = {"Authorization": f"Token {TOKEN}"}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CASES_FILE = DATA_DIR / "cases.json"
ISSUES_FILE = DATA_DIR / "issues.json"
CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"
RUN_LOG_FILE = DATA_DIR / "run_log.json"


def load_json(path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


def load_checkpoint():
    return load_json(CHECKPOINT_FILE, {
        "last_search_offset": 0,
        "processed_docket_ids": [],
        "requests_made_today": 0,
        "date_of_counter": None,
    })


def save_checkpoint(cp):
    save_json(CHECKPOINT_FILE, cp)


def throttled_get(url, params, checkpoint):
    """The single point of contact with the API. SPEC.md section 8."""
    today = datetime.now(timezone.utc).date().isoformat()
    if checkpoint["date_of_counter"] != today:
        checkpoint["date_of_counter"] = today
        checkpoint["requests_made_today"] = 0

    if checkpoint["requests_made_today"] >= DAILY_REQUEST_CAP:
        return None  # signals "stop cleanly, resume next run"

    time.sleep(FLOOR_THROTTLE_SECONDS)
    resp = requests.get(url, headers=HEADERS, params=params, timeout=30)
    checkpoint["requests_made_today"] += 1

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "60"))
        print(f"429 received, backing off {retry_after}s")
        time.sleep(retry_after)
        return throttled_get(url, params, checkpoint)

    resp.raise_for_status()
    return resp.json()


def fetch_docket_entries(docket_id, checkpoint):
    entries = []
    url = DOCKET_ENTRIES_ENDPOINT
    params = {"docket": docket_id, "page_size": 20}
    while url:
        data = throttled_get(url, params if url == DOCKET_ENTRIES_ENDPOINT else None, checkpoint)
        if data is None:
            return entries, False  # daily cap hit mid-pagination
        entries.extend(data.get("results", []))
        url = data.get("next")
        params = None
    return entries, True


def search_dockets(offset, checkpoint):
    params = {
        "q": SEARCH_QUERY,
        "type": "d",
        "filed_after": FILED_AFTER,
        "order_by": "dateFiled asc",
    }
    if offset:
        params["offset"] = offset
    return throttled_get(SEARCH_ENDPOINT, params, checkpoint)


def run():
    checkpoint = load_checkpoint()
    cases = load_json(CASES_FILE, [])
    issues_log = load_json(ISSUES_FILE, [])
    seen_ids = set(checkpoint["processed_docket_ids"])

    run_started = datetime.now(timezone.utc).isoformat()
    new_cases_this_run = 0
    stopped_reason = "completed_search_results"

    search_results = search_dockets(checkpoint["last_search_offset"], checkpoint)
    if search_results is None:
        _log_run(run_started, 0, "daily_cap_reached_before_search")
        return

    for docket in search_results.get("results", []):
        docket_id = docket.get("id")
        if docket_id in seen_ids or not docket.get("dateTerminated"):
            continue  # only mine terminated dockets — see SPEC.md section 5

        entries, complete = fetch_docket_entries(docket_id, checkpoint)
        if not complete:
            stopped_reason = "daily_cap_reached_mid_run"
            break

        record = build_case_record(docket, entries, has_full_opinion_text=False)
        record_issues = validate_record(record, seen_ids)
        if record_issues:
            issues_log.append({
                "docket_id": docket_id,
                "issues": record_issues,
                "flagged_at": datetime.now(timezone.utc).isoformat(),
            })

        cases.append(record)
        seen_ids.add(docket_id)
        checkpoint["processed_docket_ids"] = list(seen_ids)
        new_cases_this_run += 1

        # Checkpoint after every single case, per SPEC.md section 4.
        save_json(CASES_FILE, cases)
        save_json(ISSUES_FILE, issues_log)
        save_checkpoint(checkpoint)

    if stopped_reason == "completed_search_results" and search_results.get("next"):
        checkpoint["last_search_offset"] += len(search_results.get("results", []))
        save_checkpoint(checkpoint)

    _log_run(run_started, new_cases_this_run, stopped_reason,
              total_cases=len(cases), total_issues=len(issues_log))


def _log_run(started_at, new_cases, stopped_reason, total_cases=None, total_issues=None):
    log = load_json(RUN_LOG_FILE, [])
    log.append({
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "new_cases_this_run": new_cases,
        "stopped_reason": stopped_reason,
        "total_cases": total_cases,
        "total_issues": total_issues,
    })
    save_json(RUN_LOG_FILE, log[-90:])
    print(f"Run finished: {new_cases} new cases. Reason: {stopped_reason}.")


if __name__ == "__main__":
    run()
