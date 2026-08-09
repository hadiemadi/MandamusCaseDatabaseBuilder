#!/usr/bin/env python3
"""
Tests for scripts/recap_checker.py using a mocked API -- no real network
calls, no CourtListener quota consumed, no token required.

Verifies:
  1. A docket with an available RECAP document gets flagged and
     pacer_fetch_needed flips to False (a free path now exists)
  2. A docket with documents but none available stays pacer_fetch_needed=True
  3. A case that already has full opinion text (pacer_fetch_needed=False)
     is skipped entirely -- never queried, since it doesn't need PACER
  4. An already-checked case (recap_checked_at set) is not re-queried on a
     second run
  5. Running out of daily budget leaves a case unchecked (recap_checked_at
     stays unset), not falsely marked "no documents found"
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

PASS = 0
FAIL = 0


def check(label, actual, expected):
    global PASS, FAIL
    if actual == expected:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}: expected {expected!r}, got {actual!r}")


def check_true(label, condition):
    check(label, bool(condition), True)


CASES = [
    {"docket_id": 1001, "case_name": "Doe v. Blinken", "pacer_fetch_needed": True},
    {"docket_id": 1002, "case_name": "Roe v. Rubio", "pacer_fetch_needed": True},
    {"docket_id": 1003, "case_name": "Already Free", "pacer_fetch_needed": False,
     "has_full_opinion_text": True},
]


def make_resp(payload):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status = mock.Mock()
    return r


def fake_get(url, headers=None, params=None, timeout=None):
    docket_id = (params or {}).get("docket_entry__docket_id")
    if docket_id == 1001:
        return make_resp({"results": [
            {"id": 5001, "is_available": False},
            {"id": 5002, "is_available": True},
        ]})
    if docket_id == 1002:
        return make_resp({"results": [
            {"id": 5003, "is_available": False},
        ]})
    raise ValueError(f"Unexpected docket_id in test: {docket_id}")


def setup_module_paths(tmpdir):
    import recap_checker
    d = Path(tmpdir)
    recap_checker.CASES_FILE = d / "cases.json"
    recap_checker.CHECKPOINT_FILE = d / "checkpoint.json"
    (d / "cases.json").write_text(json.dumps([dict(c) for c in CASES]), encoding="utf-8")
    return recap_checker


def test_available_and_unavailable_and_skip():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"
        rc = setup_module_paths(tmpdir)

        queried_docket_ids = []

        def tracking_fake_get(url, headers=None, params=None, timeout=None):
            queried_docket_ids.append((params or {}).get("docket_entry__docket_id"))
            return fake_get(url, headers=headers, params=params, timeout=timeout)

        with mock.patch("api_client.requests.get", side_effect=tracking_fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            rc.run()

        cases = json.loads((Path(tmpdir) / "cases.json").read_text(encoding="utf-8"))
        by_id = {c["docket_id"]: c for c in cases}

        check_true("docket with an available document is flagged found",
                    by_id[1001]["recap_free_document_found"])
        check("available document id recorded",
              by_id[1001]["recap_available_document_ids"], [5002])
        check_true("pacer_fetch_needed flips to False once a free doc exists",
                    not by_id[1001]["pacer_fetch_needed"])

        check_true("docket with only unavailable documents is flagged not found",
                    by_id[1002]["recap_free_document_found"] is False)
        check_true("pacer_fetch_needed stays True when nothing is free",
                    by_id[1002]["pacer_fetch_needed"])

        check_true("case that already has free opinion text is never queried",
                    1003 not in queried_docket_ids)
        check_true("case that already has free opinion text keeps no recap fields",
                    "recap_checked_at" not in by_id[1003])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_already_checked_case_is_not_requeried():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"
        rc = setup_module_paths(tmpdir)

        cases = json.loads(rc.CASES_FILE.read_text(encoding="utf-8"))
        cases[0]["recap_checked_at"] = "2026-08-01T00:00:00+00:00"
        cases[0]["recap_free_document_found"] = False
        rc.CASES_FILE.write_text(json.dumps(cases), encoding="utf-8")

        queried_docket_ids = []

        def tracking_fake_get(url, headers=None, params=None, timeout=None):
            queried_docket_ids.append((params or {}).get("docket_entry__docket_id"))
            return fake_get(url, headers=headers, params=params, timeout=timeout)

        with mock.patch("api_client.requests.get", side_effect=tracking_fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            rc.run()

        check_true("already-checked docket is not re-queried", 1001 not in queried_docket_ids)
        check_true("unchecked docket still gets queried", 1002 in queried_docket_ids)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_budget_exhaustion_leaves_case_unchecked():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"
        rc = setup_module_paths(tmpdir)

        import api_client
        from datetime import datetime, timezone
        rc.CHECKPOINT_FILE.write_text(json.dumps({
            "last_search_offset": 0, "processed_docket_ids": [],
            "requests_made_today": api_client.DAILY_REQUEST_CAP,
            "date_of_counter": datetime.now(timezone.utc).date().isoformat(),
        }), encoding="utf-8")

        with mock.patch("api_client.requests.get", side_effect=fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            rc.run()

        cases = json.loads(rc.CASES_FILE.read_text(encoding="utf-8"))
        by_id = {c["docket_id"]: c for c in cases}
        check_true("case stays unchecked when budget is exhausted",
                    "recap_checked_at" not in by_id[1001])
        check_true("pacer_fetch_needed untouched when budget is exhausted",
                    by_id[1001]["pacer_fetch_needed"])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            print(f"\n{name}")
            fn()
    print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed\n{'='*50}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
