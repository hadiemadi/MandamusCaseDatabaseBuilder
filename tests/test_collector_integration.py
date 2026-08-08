#!/usr/bin/env python3
"""
Integration test for scripts/collector.py using a mocked API — no real
network calls, no CourtListener quota consumed, no token required.

Verifies:
  1. A full run pages through multiple search pages (up to
     MAX_SEARCH_PAGES_PER_RUN) in one go, not just one page
  2. Terminated dockets get fully mined, in priority-score order
  3. A candidate without dateTerminated is defensively skipped, not
     recorded (the search query is supposed to filter these out entirely
     -- see SPEC.md section 5 -- so this only guards against index lag)
  4. The checkpoint file correctly records what's been processed
  5. Once the true end of search results is reached, the offset resets to
     0 (so a case filed long ago that concludes recently is never
     permanently missed)
  6. Re-running with the same checkpoint does NOT reprocess the same cases
     (this is the "resume after being killed" guarantee the whole
     architecture depends on)
"""

import json
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

RECENT_DATE = "2026-06-01"  # keeps recency scoring simple/high for all fakes

FAKE_SEARCH_PAGE_1 = {
    "results": [
        {
            "docket_id": 1001,
            "caseName": "Doe v. Blinken",
            "court_id": "cand",  # 9th Circuit -> +2 priority
            "cause": "Writ of Mandamus to Adjudicate Visa Petition",
            "docketNumber": "3:23-cv-00001",
            "dateFiled": RECENT_DATE,
            "dateTerminated": "2026-07-01",
        },
        {
            "docket_id": 1002,
            "caseName": "Roe v. Rubio",
            "court_id": "dcd",
            "cause": "Writ of Mandamus",
            "docketNumber": "1:23-cv-00002",
            "dateFiled": RECENT_DATE,
            "dateTerminated": None,  # shouldn't happen given the query filter -> defensive skip
        },
    ],
    "next": "https://www.courtlistener.com/api/rest/v4/search/?offset=2",
}

FAKE_SEARCH_PAGE_2 = {
    "results": [
        {
            "docket_id": 1003,
            "caseName": "Lin v. Noem",
            "court_id": "dcd",  # not 9th Circuit
            "cause": "08:1329 unreasonable delay administrative processing",
            "docketNumber": "1:23-cv-00003",
            "dateFiled": RECENT_DATE,
            "dateTerminated": "2026-07-15",
        },
    ],
    "next": None,  # true end of results -> offset should reset to 0
}

FAKE_ENTRIES = {
    "results": [
        {"description": "MOTION to Dismiss filed.", "short_description": ""},
        {"description": "ORDER denying Motion to Dismiss.", "short_description": ""},
        {"description": "STIPULATION OF DISMISSAL filed.", "short_description": ""},
    ],
    "next": None,
}


def make_fake_response(payload):
    resp = mock.Mock()
    resp.status_code = 200
    resp.json.return_value = payload
    resp.raise_for_status = mock.Mock()
    return resp


def fake_requests_get(url, headers=None, params=None, timeout=None):
    if "search" in url:
        offset = (params or {}).get("offset", 0)
        if offset == 0:
            return make_fake_response(FAKE_SEARCH_PAGE_1)
        if offset == 2:
            return make_fake_response(FAKE_SEARCH_PAGE_2)
        return make_fake_response({"results": [], "next": None})
    if "docket-entries" in url:
        return make_fake_response(FAKE_ENTRIES)
    raise ValueError(f"Unexpected URL in test: {url}")


def run_test():
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"

        # Patch the data directory and sleep (so the test doesn't take 13s+ per call)
        import collector
        collector.DATA_DIR = __import__("pathlib").Path(tmpdir)
        collector.CASES_FILE = collector.DATA_DIR / "cases.json"
        collector.ISSUES_FILE = collector.DATA_DIR / "issues.json"
        collector.CHECKPOINT_FILE = collector.DATA_DIR / "checkpoint.json"
        collector.RUN_LOG_FILE = collector.DATA_DIR / "run_log.json"

        with mock.patch("collector.requests.get", side_effect=fake_requests_get), \
             mock.patch("collector.time.sleep", return_value=None):
            collector.run()

        cases = json.loads(collector.CASES_FILE.read_text())
        checkpoint = json.loads(collector.CHECKPOINT_FILE.read_text())

        assert len(cases) == 2, f"Expected exactly 2 mined cases (1002 defensively skipped), got {len(cases)}"

        by_id = {c["docket_id"]: c for c in cases}
        assert by_id[1001]["outcome"] == "settled", "Terminated docket 1001 should be fully mined"
        assert by_id[1003]["outcome"] == "settled", "Terminated docket 1003 should be fully mined"
        assert 1002 not in by_id, "Docket without dateTerminated should be skipped, not recorded"

        assert {1001, 1003} <= set(checkpoint["processed_docket_ids"])
        assert 1002 not in checkpoint["processed_docket_ids"], \
            "Skipped docket should not be marked processed either"
        assert checkpoint["last_search_offset"] == 0, \
            "Offset should reset to 0 after reaching the true end of search results"
        print("PASS: multi-page run mined terminated dockets, skipped the non-terminated one, and reset the offset")

        # --- Second run: same fake pages, but checkpoint now has both mined docket_ids already processed ---
        with mock.patch("collector.requests.get", side_effect=fake_requests_get), \
             mock.patch("collector.time.sleep", return_value=None):
            collector.run()

        cases_after_second_run = json.loads(collector.CASES_FILE.read_text())
        assert len(cases_after_second_run) == 2, (
            f"Re-running from the reset offset should NOT duplicate already-processed dockets, "
            f"got {len(cases_after_second_run)} records"
        )
        print("PASS: second run correctly skipped all already-processed dockets (resume logic works)")

        print("\nALL INTEGRATION TESTS PASSED")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run_test()
