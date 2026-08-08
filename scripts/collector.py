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
from extraction import (
    build_case_record,
    compute_priority_score,
    validate_record,
)

# ---------------------------------------------------------------------------
# SPEC.md section 5 — data source and scope
# ---------------------------------------------------------------------------

API_ROOT = "https://www.courtlistener.com/api/rest/v4"
SEARCH_ENDPOINT = f"{API_ROOT}/search/"
DOCKET_ENTRIES_ENDPOINT = f"{API_ROOT}/docket-entries/"

TERMINATED_AFTER = "2020-01-01"
# dateTerminated is a query-string range filter, not an order_by option (confirmed
# against the live API -- see SPEC.md section 13 decision log). This guarantees
# every result is already concluded, regardless of when it was originally filed.
SEARCH_QUERY = (
    '("writ of mandamus" OR "mandamus" OR "unreasonable delay") '
    'AND ("221(g)" OR "administrative processing" OR "consular") '
    'AND ("visa") '
    f'AND dateTerminated:[{TERMINATED_AFTER} TO *]'
)

# ---------------------------------------------------------------------------
# SPEC.md section 8 — rate limiting (single choke point, nothing bypasses it)
# ---------------------------------------------------------------------------

FLOOR_THROTTLE_SECONDS = 13
DAILY_REQUEST_CAP = 120
MAX_SEARCH_PAGES_PER_RUN = 5  # SPEC.md section 8 — use most of the daily budget

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
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default
    return default


def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


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
        "order_by": "dateFiled desc",  # stable pagination order only, see SPEC.md section 5
    }
    if offset:
        params["offset"] = offset
    return throttled_get(SEARCH_ENDPOINT, params, checkpoint)


def gather_candidates(offset, checkpoint):
    """Fetch up to MAX_SEARCH_PAGES_PER_RUN consecutive search pages.
    Returns (candidates, next_offset, reached_end, hit_cap)."""
    candidates = []
    pages_fetched = 0
    reached_end = False
    hit_cap = False

    while pages_fetched < MAX_SEARCH_PAGES_PER_RUN:
        search_results = search_dockets(offset, checkpoint)
        if search_results is None:
            hit_cap = True
            break
        results = search_results.get("results", [])
        print(f"Search page at offset={offset} returned {len(results)} dockets.")
        candidates.extend(results)
        pages_fetched += 1
        offset += len(results)
        if not search_results.get("next"):
            reached_end = True
            break

    return candidates, offset, reached_end, hit_cap


def run():
    checkpoint = load_checkpoint()
    cases = load_json(CASES_FILE, [])
    issues_log = load_json(ISSUES_FILE, [])
    seen_ids = set(checkpoint["processed_docket_ids"])

    run_started = datetime.now(timezone.utc).isoformat()
    new_cases_this_run = 0

    candidates, next_offset, reached_end, hit_cap_during_search = gather_candidates(
        checkpoint["last_search_offset"], checkpoint
    )

    if not candidates and hit_cap_during_search:
        _log_run(run_started, 0, "daily_cap_reached_before_search")
        return

    # SPEC.md 5.1 — mine highest-priority candidates first, not raw search order
    unseen = [d for d in candidates if d.get("docket_id") not in seen_ids]
    unseen.sort(key=compute_priority_score, reverse=True)

    stopped_reason = "daily_cap_reached_during_search" if hit_cap_during_search else "completed_search_results"

    for docket in unseen:
        docket_id = docket.get("docket_id")
        if docket_id in seen_ids:
            continue  # can appear twice across overlapping pages

        if not docket.get("dateTerminated"):
            # Defensive only -- the search query filters on dateTerminated (SPEC.md
            # section 5), so this shouldn't normally happen. Skip, don't record.
            continue

        entries, complete = fetch_docket_entries(docket_id, checkpoint)
        if not complete:
            stopped_reason = "daily_cap_reached_mid_run"
            break

        print(f"[{new_cases_this_run + 1}] mined docket {docket_id} "
              f"({len(entries)} entries, requests_made_today="
              f"{checkpoint['requests_made_today']})")

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

    # SPEC.md section 5 — reset to 0 at the true end so future filings
    # (which appear at the top of a newest-first list) are never permanently missed.
    checkpoint["last_search_offset"] = 0 if reached_end else next_offset
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
