#!/usr/bin/env python3
"""
Checks CourtListener's recap-documents endpoint for filed documents that
another user already purchased from PACER and donated -- free to us via
`is_available=True`. SPEC.md section 14, tier 3.

Only runs against cases already in data/cases.json that still need PACER
(pacer_fetch_needed=True, i.e. no free opinion text was found for them by
scripts/opinion_fetcher.py). Never downloads or purchases anything -- flags
availability only, so a human decides what to do next. SPEC.md section 2
constraint 7 forbids any automatic PACER purchase; this stays on the free
side of that line.

NOT YET WIRED INTO collect.yml. The docket-scoped filter parameter below
(RECAP_DOCUMENTS_DOCKET_FILTER) is CourtListener's documented convention but
has not been confirmed against a live response -- verify with one real API
call before adding this to the scheduled workflow (SPEC.md section 13
decision log).

Shares the same daily request budget as collector.py and opinion_fetcher.py
through api_client's single throttle choke point (SPEC.md section 8).

USAGE
  export COURTLISTENER_TOKEN="..."
  python scripts/recap_checker.py
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from api_client import (
    RECAP_DOCUMENTS_ENDPOINT,
    EMPTY_CHECKPOINT,
    load_json,
    save_json,
    throttled_get,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CASES_FILE = DATA_DIR / "cases.json"
CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"

# CourtListener's documented filter convention for scoping recap-documents
# to one docket. UNVERIFIED against a live response -- see module docstring.
RECAP_DOCUMENTS_DOCKET_FILTER = "docket_entry__docket_id"


def load_checkpoint():
    return load_json(CHECKPOINT_FILE, dict(EMPTY_CHECKPOINT))


def save_checkpoint(cp):
    save_json(CHECKPOINT_FILE, cp)


def check_docket_for_free_documents(docket_id, checkpoint):
    """Returns (available_document_ids, budget_ok). available_document_ids
    is a list (possibly empty) of recap-document ids with is_available=True.
    budget_ok=False means the daily cap was hit -- the docket must stay
    unchecked for a later run, not be recorded as "none found"."""
    params = {RECAP_DOCUMENTS_DOCKET_FILTER: docket_id, "page_size": 20}
    data = throttled_get(RECAP_DOCUMENTS_ENDPOINT, params, checkpoint)
    if data is None:
        return None, False
    available = [
        doc["id"] for doc in data.get("results", [])
        if doc.get("is_available") and doc.get("id") is not None
    ]
    return available, True


def run():
    cases = load_json(CASES_FILE, [])
    if not cases:
        print("No cases.json data yet -- nothing to check.")
        return

    checkpoint = load_checkpoint()
    pending = [c for c in cases if c.get("pacer_fetch_needed") and not c.get("recap_checked_at")]
    print(f"{len(pending)} case(s) still need a RECAP free-document check "
          f"(of {len(cases)} total). Budget used today: {checkpoint['requests_made_today']}")

    checked = 0
    found = 0
    for case in pending:
        docket_id = case.get("docket_id")
        available_ids, budget_ok = check_docket_for_free_documents(docket_id, checkpoint)
        if not budget_ok:
            print(f"  stopping: daily request budget exhausted "
                  f"(used {checkpoint['requests_made_today']}). "
                  f"docket {docket_id} stays unchecked for the next run.")
            break

        case["recap_checked_at"] = datetime.now(timezone.utc).isoformat()
        if available_ids:
            case["recap_free_document_found"] = True
            case["recap_available_document_ids"] = available_ids
            case["pacer_fetch_needed"] = False
            found += 1
            print(f"  docket {docket_id}: {len(available_ids)} free RECAP document(s) found")
        else:
            case["recap_free_document_found"] = False
            print(f"  docket {docket_id}: no free RECAP documents available")

        checked += 1
        save_json(CASES_FILE, cases)
        save_checkpoint(checkpoint)

    print(f"\nChecked {checked} docket(s), found free documents for {found}.")
    remaining = sum(1 for c in cases if c.get("pacer_fetch_needed") and not c.get("recap_checked_at"))
    print(f"{remaining} still unchecked; rerun after the daily window resets.")


if __name__ == "__main__":
    run()
