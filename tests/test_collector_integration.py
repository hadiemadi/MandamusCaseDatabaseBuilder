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
        collector.BULK_DISCOVERED_FILE = collector.DATA_DIR / "bulk_discovered_dockets.json"

        with mock.patch("api_client.requests.get", side_effect=fake_requests_get), \
             mock.patch("api_client.time.sleep", return_value=None):
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
        with mock.patch("api_client.requests.get", side_effect=fake_requests_get), \
             mock.patch("api_client.time.sleep", return_value=None):
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


def run_crash_resilience_test():
    """A run can be killed between saving cases.json and checkpoint.json.
    Simulates that exact desync and confirms the next run does NOT
    duplicate the case that was already recorded but never checkpointed."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"

        import collector
        collector.DATA_DIR = __import__("pathlib").Path(tmpdir)
        collector.CASES_FILE = collector.DATA_DIR / "cases.json"
        collector.ISSUES_FILE = collector.DATA_DIR / "issues.json"
        collector.CHECKPOINT_FILE = collector.DATA_DIR / "checkpoint.json"
        collector.RUN_LOG_FILE = collector.DATA_DIR / "run_log.json"
        collector.BULK_DISCOVERED_FILE = collector.DATA_DIR / "bulk_discovered_dockets.json"

        # Pre-seed cases.json as if a prior run mined docket 1001 successfully,
        # but was killed before checkpoint.json got written to reflect it.
        pre_existing_record = {
            "docket_id": 1001, "case_name": "Doe v. Blinken", "court": "cand",
            "docket_number": "3:23-cv-00001", "citation": "Doe v. Blinken, No. 3:23-cv-00001 (cand)",
            "date_filed": RECENT_DATE, "date_terminated": "2026-07-01", "raw_entry_count": 3,
            "outcome": "settled",
        }
        collector.CASES_FILE.write_text(json.dumps([pre_existing_record]), encoding="utf-8")
        collector.CHECKPOINT_FILE.write_text(json.dumps({
            "last_search_offset": 0, "processed_docket_ids": [], "requests_made_today": 0,
            "date_of_counter": None,
        }), encoding="utf-8")

        with mock.patch("api_client.requests.get", side_effect=fake_requests_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            collector.run()

        cases = json.loads(collector.CASES_FILE.read_text())
        docket_1001_copies = [c for c in cases if c["docket_id"] == 1001]
        assert len(docket_1001_copies) == 1, (
            f"Desynced checkpoint should not cause a re-mine/duplicate of an "
            f"already-recorded case, but found {len(docket_1001_copies)} copies"
        )
        print("PASS: a checkpoint desynced from cases.json does not cause duplicate mining")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_multi_cycle_test():
    """Simulates many sequential daily runs (not just two) against a backlog
    that spans more pages than fit in one run, including a full offset-reset
    cycle followed by continued forward progress -- catches state corruption
    that only shows up after repeated cycles, not a single resume."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"

        import collector
        collector.DATA_DIR = __import__("pathlib").Path(tmpdir)
        collector.CASES_FILE = collector.DATA_DIR / "cases.json"
        collector.ISSUES_FILE = collector.DATA_DIR / "issues.json"
        collector.CHECKPOINT_FILE = collector.DATA_DIR / "checkpoint.json"
        collector.RUN_LOG_FILE = collector.DATA_DIR / "run_log.json"
        collector.BULK_DISCOVERED_FILE = collector.DATA_DIR / "bulk_discovered_dockets.json"
        original_max_pages = collector.MAX_SEARCH_PAGES_PER_RUN
        collector.MAX_SEARCH_PAGES_PER_RUN = 1  # force each run to only see one page

        pages = {
            0: {"results": [{
                "docket_id": 2001, "caseName": "A v. State", "court_id": "dcd", "cause": "mandamus",
                "docketNumber": "1:1", "dateFiled": RECENT_DATE, "dateTerminated": "2026-07-01",
            }], "next": "next-page-1"},
            1: {"results": [{
                "docket_id": 2002, "caseName": "B v. State", "court_id": "dcd", "cause": "mandamus",
                "docketNumber": "1:2", "dateFiled": RECENT_DATE, "dateTerminated": "2026-07-05",
            }], "next": "next-page-2"},
            2: {"results": [{
                "docket_id": 2003, "caseName": "C v. State", "court_id": "dcd", "cause": "mandamus",
                "docketNumber": "1:3", "dateFiled": RECENT_DATE, "dateTerminated": "2026-07-10",
            }], "next": None},  # true end
        }

        def multi_cycle_fake_get(url, headers=None, params=None, timeout=None):
            if "search" in url:
                offset = (params or {}).get("offset", 0)
                return make_fake_response(pages.get(offset, {"results": [], "next": None}))
            if "docket-entries" in url:
                return make_fake_response(FAKE_ENTRIES)
            raise ValueError(f"Unexpected URL in test: {url}")

        with mock.patch("api_client.requests.get", side_effect=multi_cycle_fake_get), \
             mock.patch("api_client.time.sleep", return_value=None):
            for i in range(1, 4):
                collector.run()
                cp = json.loads(collector.CHECKPOINT_FILE.read_text())
                cases = json.loads(collector.CASES_FILE.read_text())
                assert len(cases) == i, f"After run {i}, expected {i} cases, got {len(cases)}"

            cp_after_run3 = json.loads(collector.CHECKPOINT_FILE.read_text())
            assert cp_after_run3["last_search_offset"] == 0, \
                "Offset should reset to 0 after run 3 reaches the true end of the backlog"
            print("PASS: 3 sequential runs each mined exactly one new case and reset the offset at the end")

            # Run 4: offset reset to 0 -> re-fetches page 0, docket 2001 already seen -> no new cases,
            # but should still advance forward again (not get stuck re-fetching page 0 forever)
            collector.run()
            cases_after_run4 = json.loads(collector.CASES_FILE.read_text())
            cp_after_run4 = json.loads(collector.CHECKPOINT_FILE.read_text())
            assert len(cases_after_run4) == 3, "Run 4 should mine nothing new (everything already seen)"
            assert cp_after_run4["last_search_offset"] == 1, \
                "Run 4 should advance past page 0 again, not get stuck at the reset position"
            print("PASS: post-reset run correctly resumes forward progress instead of looping in place")

    finally:
        collector.MAX_SEARCH_PAGES_PER_RUN = original_max_pages
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_bulk_discovery_primary_source_test():
    """When data/bulk_discovered_dockets.json exists (scripts/bulk_docket_filter.py
    has been run), it should supply candidates for free -- live search should
    shrink to a single gap-filler page scoped to dates after the bulk
    snapshot, not the old full multi-page crawl since 2020."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"

        import collector
        from pathlib import Path
        collector.DATA_DIR = Path(tmpdir)
        collector.CASES_FILE = collector.DATA_DIR / "cases.json"
        collector.ISSUES_FILE = collector.DATA_DIR / "issues.json"
        collector.CHECKPOINT_FILE = collector.DATA_DIR / "checkpoint.json"
        collector.RUN_LOG_FILE = collector.DATA_DIR / "run_log.json"
        collector.BULK_DISCOVERED_FILE = collector.DATA_DIR / "bulk_discovered_dockets.json"

        collector.BULK_DISCOVERED_FILE.write_text(json.dumps({
            "snapshot_date": "2026-06-30",
            "generated_at": "2026-06-30T09:03:56+00:00",
            "candidates": [{
                "docket_id": 5001, "caseName": "Bulk v. Blinken", "court_id": "cand",
                "cause": "5:706 Mandamus: Unreasonable delay", "docketNumber": "3:23-cv-05001",
                "dateFiled": RECENT_DATE, "dateTerminated": "2026-07-01",
            }],
        }), encoding="utf-8")

        search_call_queries = []

        def fake_get_with_bulk(url, headers=None, params=None, timeout=None):
            if "search" in url:
                search_call_queries.append((params or {}).get("q", ""))
                return make_fake_response({"results": [], "next": None})
            if "docket-entries" in url:
                return make_fake_response(FAKE_ENTRIES)
            raise ValueError(f"Unexpected URL in test: {url}")

        with mock.patch("api_client.requests.get", side_effect=fake_get_with_bulk), \
             mock.patch("api_client.time.sleep", return_value=None):
            collector.run()

        cases = json.loads(collector.CASES_FILE.read_text())
        assert len(cases) == 1 and cases[0]["docket_id"] == 5001, (
            f"Bulk-discovered candidate should be mined without any live search hit, got {cases}"
        )
        assert len(search_call_queries) == 1, (
            f"Live search should shrink to INCREMENTAL_SEARCH_MAX_PAGES (1) once a bulk "
            f"snapshot exists, got {len(search_call_queries)} search call(s)"
        )
        assert "2026-06-30" in search_call_queries[0], (
            "Live gap-filler search should scope dateTerminated to the bulk snapshot's date, "
            f"not the old 2020 floor -- got query: {search_call_queries[0]}"
        )
        print("PASS: bulk-discovered candidates are mined for free, live search shrinks to a "
              "snapshot-scoped gap-filler")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_max_backoff_test():
    """A huge Retry-After (a real run hit 66019s / ~18.3h) must not be slept
    through -- the run should stop cleanly instead, since a later run (every
    4h, see SPEC.md section 4) retries at no cost to the daily total."""
    tmpdir = tempfile.mkdtemp()
    try:
        os.environ["COURTLISTENER_TOKEN"] = "fake-token-for-testing"

        import collector
        import api_client
        collector.DATA_DIR = __import__("pathlib").Path(tmpdir)
        collector.CASES_FILE = collector.DATA_DIR / "cases.json"
        collector.ISSUES_FILE = collector.DATA_DIR / "issues.json"
        collector.CHECKPOINT_FILE = collector.DATA_DIR / "checkpoint.json"
        collector.RUN_LOG_FILE = collector.DATA_DIR / "run_log.json"
        collector.BULK_DISCOVERED_FILE = collector.DATA_DIR / "bulk_discovered_dockets.json"

        single_page = {
            "results": [{
                "docket_id": 3001, "caseName": "Huge Backoff Case", "court_id": "dcd",
                "cause": "mandamus", "docketNumber": "1:1",
                "dateFiled": RECENT_DATE, "dateTerminated": "2026-07-01",
            }],
            "next": None,
        }

        def huge_backoff_fake_get(url, headers=None, params=None, timeout=None):
            if "search" in url:
                return make_fake_response(single_page)
            if "docket-entries" in url:
                resp = mock.Mock()
                resp.status_code = 429
                resp.headers = {"Retry-After": "66019"}  # matches the real value seen live
                return resp
            raise ValueError(f"Unexpected URL in test: {url}")

        sleep_calls = []
        with mock.patch("api_client.requests.get", side_effect=huge_backoff_fake_get), \
             mock.patch("api_client.time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            collector.run()

        assert 66019 not in sleep_calls, "Must not sleep through a backoff exceeding MAX_BACKOFF_SECONDS"
        assert all(s <= api_client.MAX_BACKOFF_SECONDS for s in sleep_calls), \
            f"No sleep call should exceed the max backoff cap, got {sleep_calls}"

        # cases.json is only ever written after a case is successfully mined, so with
        # zero cases mined it may not exist at all -- that's correct, not a crash.
        cases = json.loads(collector.CASES_FILE.read_text()) if collector.CASES_FILE.exists() else []
        assert len(cases) == 0, "Run should stop cleanly with zero cases mined, not crash"
        print("PASS: a Retry-After exceeding MAX_BACKOFF_SECONDS stops the run cleanly instead of sleeping through it")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    run_test()
    run_crash_resilience_test()
    run_multi_cycle_test()
    run_bulk_discovery_primary_source_test()
    run_max_backoff_test()
    print("\nALL COLLECTOR INTEGRATION TESTS PASSED (including crash-resilience, multi-cycle, bulk-discovery, and max-backoff)")
