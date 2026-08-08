#!/usr/bin/env python3
"""
Integration test for scripts/collector.py using a mocked API — no real
network calls, no CourtListener quota consumed, no token required.

Verifies:
  1. A full run against fake search results produces correct case records
  2. The checkpoint file correctly records what's been processed
  3. Re-running with the same checkpoint does NOT reprocess the same cases
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

FAKE_DOCKETS = {
    "results": [
        {
            "docket_id": 1001,
            "caseName": "Doe v. Blinken",
            "court_id": "cand",
            "docketNumber": "3:23-cv-00001",
            "dateFiled": "2023-01-01",
            "dateTerminated": "2023-08-01",
        },
        {
            "docket_id": 1002,
            "caseName": "Roe v. Rubio",
            "court_id": "dcd",
            "docketNumber": "1:23-cv-00002",
            "dateFiled": "2023-02-01",
            "dateTerminated": None,  # not terminated -> should be skipped
        },
    ],
    "next": None,
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
        return make_fake_response(FAKE_DOCKETS)
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

        assert len(cases) == 1, f"Expected exactly 1 case (only terminated dockets mined), got {len(cases)}"
        assert cases[0]["docket_id"] == 1001, "Wrong docket was mined"
        assert cases[0]["outcome"] == "settled", "Extraction logic didn't run correctly end-to-end"
        assert 1001 in checkpoint["processed_docket_ids"], "Checkpoint didn't record the processed docket"
        assert 1002 not in checkpoint["processed_docket_ids"], "Non-terminated docket should never be marked processed"
        print("PASS: first run collected the correct single case and checkpointed it")

        # --- Second run: same fake API results, but checkpoint now has 1001 already processed ---
        with mock.patch("collector.requests.get", side_effect=fake_requests_get), \
             mock.patch("collector.time.sleep", return_value=None):
            collector.run()

        cases_after_second_run = json.loads(collector.CASES_FILE.read_text())
        assert len(cases_after_second_run) == 1, (
            f"Re-running should NOT duplicate an already-processed case, "
            f"got {len(cases_after_second_run)} records"
        )
        print("PASS: second run correctly skipped the already-processed case (resume logic works)")

        print("\nALL INTEGRATION TESTS PASSED")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run_test()
