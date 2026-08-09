#!/usr/bin/env python3
"""
Tests for scripts/dashboard.py -- previously had zero coverage.

Verifies the dashboard actually reflects non-trivial data correctly (not
just the empty-data happy path we'd tested by hand before): stat counts,
the stale-run warning banner, and that flagged issues don't silently
vanish from the rendered output.
"""

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

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
    {
        "docket_id": 1001, "case_name": "Doe v. Blinken", "court": "cand",
        "docket_number": "3:23-cv-00001", "date_filed": "2025-01-01", "date_terminated": "2025-06-01",
        "days_to_resolution": 151, "citation": "Doe v. Blinken, No. 3:23-cv-00001 (cand)",
        "source_url": "https://www.courtlistener.com/docket/1001/",
        "procedural_posture": "mtd_denied", "outcome": "mtd_denied", "disposition_confidence": "unknown",
        "similarity_to_own_case": True, "has_full_opinion_text": False, "pacer_fetch_needed": True,
        "court_reasoning_summary": "", "trac_factor_details": {}, "raw_entry_count": 5, "relevance_score": 5,
    },
    {
        "docket_id": 1002, "case_name": "Roe v. Rubio", "court": "dcd",
        "docket_number": "1:23-cv-00002", "date_filed": "2025-02-01", "date_terminated": "2025-03-01",
        "days_to_resolution": 28, "citation": "Roe v. Rubio, No. 1:23-cv-00002 (dcd)",
        "source_url": "https://www.courtlistener.com/docket/1002/",
        "procedural_posture": "settled_stipulated_dismissal", "outcome": "settled",
        "disposition_confidence": "needs_manual_review", "similarity_to_own_case": False,
        "has_full_opinion_text": True, "pacer_fetch_needed": False,
        "court_reasoning_summary": "", "trac_factor_details": {}, "raw_entry_count": 4, "relevance_score": 1,
    },
]

ISSUES = [
    {"docket_id": 1003, "issues": ["missing_required_field:case_name"], "flagged_at": "2025-01-01T00:00:00Z"},
]

SEED = {
    "foundational_authorities": {
        "entries": [
            {
                "case_name": "Telecommunications Research & Action Center v. FCC",
                "citation": "750 F.2d 70", "court": "cadc", "circuit": "DC",
                "why_it_matters": "Defines the six-factor unreasonable-delay test.",
                "opinion_fetch_status": "found_free_opinion",
                "opinion_absolute_url": "/opinion/1/trac-v-fcc/",
            },
        ]
    },
    "cases": [
        {
            "case_name": "Taherian v. Blinken", "citation": "2024 WL 1652625",
            "court": "cacd", "circuit": "9th", "direction": "favorable_to_plaintiff",
            "opinion_fetch_status": "found_free_opinion",
            "opinion_absolute_url": "/opinion/2/taherian-v-blinken/",
        },
        {
            "case_name": "Mohammad v. Blinken", "citation": "548 F. Supp. 3d 159",
            "court": "dcd", "circuit": "DC", "direction": "unfavorable_to_plaintiff",
            "opinion_fetch_status": "found_free_opinion",
            "opinion_absolute_url": "/opinion/3/mohammad-v-blinken/",
            "needs_manual_match_check": True,
        },
        {
            "case_name": "Nonexistent v. Nobody", "citation": "no such thing",
            "court": "cand", "circuit": "9th", "direction": "unknown",
            "opinion_fetch_status": "pending",
        },
    ],
}


def write_data_files(tmpdir, cases, issues, run_log):
    (tmpdir / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    (tmpdir / "issues.json").write_text(json.dumps(issues), encoding="utf-8")
    (tmpdir / "run_log.json").write_text(json.dumps(run_log), encoding="utf-8")


def patch_dashboard_paths(dashboard, tmpdir):
    dashboard.DATA_DIR = tmpdir
    dashboard.CASES_FILE = tmpdir / "cases.json"
    dashboard.ISSUES_FILE = tmpdir / "issues.json"
    dashboard.RUN_LOG_FILE = tmpdir / "run_log.json"
    dashboard.SEED_FILE = tmpdir / "seed_citations.json"
    dashboard.OUTPUT_FILE = tmpdir / "dashboard.html"


def test_stat_counts_and_ok_banner():
    tmpdir = tempfile.mkdtemp()
    try:
        import dashboard
        from pathlib import Path
        patch_dashboard_paths(dashboard, Path(tmpdir))

        recent_run_log = [{
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "new_cases_this_run": 2, "stopped_reason": "completed_search_results",
            "total_cases": 2, "total_issues": 1,
        }]
        write_data_files(Path(tmpdir), CASES, ISSUES, recent_run_log)

        dashboard.build_dashboard()
        html = dashboard.OUTPUT_FILE.read_text(encoding="utf-8")

        check_true("total cases count (2) appears in output", '<div class="num">2</div>' in html)
        check_true("flagged issues count (1) appears in output", '<div class="num">1</div>' in html)
        check_true("shows 'ok' banner for a recent run", 'banner ok' in html)
        check_true("does NOT show stale warning banner", 'banner warn' not in html)
        check_true("mtd_denied outcome badge rendered", "mtd_denied" in html)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_stale_run_shows_warning_banner():
    tmpdir = tempfile.mkdtemp()
    try:
        import dashboard
        from pathlib import Path
        patch_dashboard_paths(dashboard, Path(tmpdir))

        old_time = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        stale_run_log = [{
            "started_at": old_time, "finished_at": old_time,
            "new_cases_this_run": 0, "stopped_reason": "completed_search_results",
            "total_cases": 2, "total_issues": 0,
        }]
        write_data_files(Path(tmpdir), CASES, [], stale_run_log)

        dashboard.build_dashboard()
        html = dashboard.OUTPUT_FILE.read_text(encoding="utf-8")

        check_true("shows stale warning banner when last run was 72h ago", 'banner warn' in html)
        check_true("does NOT show the ok banner", 'banner ok' not in html)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_seed_and_foundational_progress_rendered():
    tmpdir = tempfile.mkdtemp()
    try:
        import dashboard
        from pathlib import Path
        patch_dashboard_paths(dashboard, Path(tmpdir))

        recent_run_log = [{
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "new_cases_this_run": 0, "stopped_reason": "completed_search_results",
            "total_cases": 0, "total_issues": 0,
        }]
        write_data_files(Path(tmpdir), [], [], recent_run_log)
        (Path(tmpdir) / "seed_citations.json").write_text(json.dumps(SEED), encoding="utf-8")

        dashboard.build_dashboard()
        html = dashboard.OUTPUT_FILE.read_text(encoding="utf-8")

        check_true("opinion progress stat card shows 3 of 4 found",
                   '<div class="num">3/4</div>' in html)
        check_true("manual match check count (1) rendered",
                   'need manual check' in html)
        check_true("9th Circuit seed case name rendered", "Taherian v. Blinken" in html)
        check_true("foundational authority rendered", "Telecommunications Research" in html)
        check_true("check-match flag rendered for low-confidence match", "check match" in html)
        check_true("opinion link uses courtlistener.com absolute url",
                   "https://www.courtlistener.com/opinion/2/taherian-v-blinken/" in html)

        # 9th Circuit + foundational fetched first; Taherian (9th) must appear
        # before Mohammad (DC) in the rendered seed table, matching
        # opinion_fetcher.py's fetch priority (SPEC.md section 15).
        taherian_pos = html.index("Taherian v. Blinken")
        mohammad_pos = html.index("Mohammad v. Blinken")
        check_true("9th Circuit case listed before D.D.C. case", taherian_pos < mohammad_pos)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_empty_data_does_not_crash():
    tmpdir = tempfile.mkdtemp()
    try:
        import dashboard
        from pathlib import Path
        patch_dashboard_paths(dashboard, Path(tmpdir))
        # No files written at all -- simulates the very first run before any data exists.
        dashboard.build_dashboard()
        html = dashboard.OUTPUT_FILE.read_text(encoding="utf-8")
        check_true("empty-state dashboard still renders total cases as 0", '<div class="num">0</div>' in html)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def run_all():
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n{t.__name__}")
        t()
    print(f"\n{'='*50}\n{PASS} passed, {FAIL} failed\n{'='*50}")
    if FAIL:
        sys.exit(1)


if __name__ == "__main__":
    run_all()
