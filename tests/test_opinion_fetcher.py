#!/usr/bin/env python3
"""
Tests for scripts/opinion_fetcher.py using a mocked API -- no real network
calls, no CourtListener quota consumed, no token required.

Verifies:
  1. 9th Circuit cases are processed before out-of-circuit ones (SPEC.md 15)
  2. A found opinion is written verbatim to disk and recorded in the seed file
  3. A wrong-case search hit is FLAGGED for manual review rather than silently
     trusted -- a misattributed opinion feeding a legal filing is worse than
     a missing one
  4. Running out of daily budget leaves the case 'pending', never marks it
     as a final answer
  5. A case with no case-law hit is recorded as not_found (likely unreported)
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


SEED = {
    "_README": {"what": "test fixture"},
    "cases": [
        {
            "case_name": "Mohammad v. Blinken", "citation": "548 F. Supp. 3d 159",
            "court": "dcd", "circuit": "DC", "direction": "unfavorable_to_plaintiff",
            "opinion_fetch_status": "pending",
        },
        {
            "case_name": "Taherian v. Blinken", "citation": "2024 WL 1652625",
            "court": "cacd", "circuit": "9th", "direction": "favorable_to_plaintiff",
            "opinion_fetch_status": "pending",
        },
        {
            "case_name": "Nonexistent v. Nobody", "citation": "no such thing",
            "court": "cand", "circuit": "9th", "direction": "unknown",
            "opinion_fetch_status": "pending",
        },
    ],
}

OPINION_BODY = "MEMORANDUM OPINION. The Court DENIES the motion to dismiss. " * 20


def make_resp(payload):
    r = mock.Mock()
    r.status_code = 200
    r.json.return_value = payload
    r.raise_for_status = mock.Mock()
    return r


def fake_get(url, headers=None, params=None, timeout=None):
    if "search" in url:
        q = (params or {}).get("q", "")
        if "Nonexistent" in q:
            return make_resp({"results": []})
        # Return a caseName that matches well for Taherian, poorly for Mohammad
        name = "Taherian v. Blinken" if "Taherian" in q else "Totally Different Case"
        return make_resp({"results": [{
            "caseName": name, "cluster_id": 999,
            "absolute_url": "/opinion/999/x/",
        }]})
    if "opinions" in url:
        return make_resp({"results": [{"plain_text": OPINION_BODY}]})
    raise ValueError(f"Unexpected URL in test: {url}")


def setup_module_paths(tmpdir):
    import opinion_fetcher
    d = Path(tmpdir)
    opinion_fetcher.DATA_DIR = d
    opinion_fetcher.SEED_FILE = d / "seed_citations.json"
    opinion_fetcher.CHECKPOINT_FILE = d / "checkpoint.json"
    opinion_fetcher.OPINIONS_DIR = d / "opinions"
    (d / "seed_citations.json").write_text(json.dumps(SEED), encoding="utf-8")
    return opinion_fetcher


def test_priority_order_and_fetch():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"
        of = setup_module_paths(tmpdir)

        with mock.patch("api_client.requests.get", side_effect=fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            of.run()

        seed = json.loads((Path(tmpdir) / "seed_citations.json").read_text(encoding="utf-8"))
        by_name = {c["case_name"]: c for c in seed["cases"]}

        check("Taherian (9th Cir) opinion found",
              by_name["Taherian v. Blinken"]["opinion_fetch_status"], "found_free_opinion")
        check("high-confidence match not flagged",
              by_name["Taherian v. Blinken"].get("needs_manual_match_check"), None)

        text_path = Path(tmpdir) / "opinions" / "taherian-v-blinken.txt"
        check_true("opinion text written to disk", text_path.exists())
        # Verbatim apart from surrounding whitespace, which the fetcher strips.
        check("opinion stored verbatim, not summarized",
              text_path.read_text(encoding="utf-8"), OPINION_BODY.strip())

        check("wrong-case hit is flagged for manual check",
              by_name["Mohammad v. Blinken"].get("needs_manual_match_check"), True)

        check("no search hit recorded as not_found",
              by_name["Nonexistent v. Nobody"]["opinion_fetch_status"], "not_found")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_sort_puts_ninth_circuit_first():
    import opinion_fetcher as of
    ordered = sorted(SEED["cases"], key=of.sort_key)
    check("9th Circuit favorable case sorts first", ordered[0]["case_name"], "Taherian v. Blinken")
    check("out-of-circuit case sorts last", ordered[-1]["circuit"], "DC")


def test_budget_exhaustion_leaves_case_pending():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"
        of = setup_module_paths(tmpdir)
        # Pre-set the counter at the cap so throttled_get refuses immediately.
        import api_client
        from datetime import datetime, timezone
        (Path(tmpdir) / "checkpoint.json").write_text(json.dumps({
            "last_search_offset": 0, "processed_docket_ids": [],
            "requests_made_today": api_client.DAILY_REQUEST_CAP,
            "date_of_counter": datetime.now(timezone.utc).date().isoformat(),
        }), encoding="utf-8")

        with mock.patch("api_client.requests.get", side_effect=fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            of.run()

        seed = json.loads((Path(tmpdir) / "seed_citations.json").read_text(encoding="utf-8"))
        statuses = {c["opinion_fetch_status"] for c in seed["cases"]}
        check("all cases remain pending when budget is exhausted", statuses, {"pending"})
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
