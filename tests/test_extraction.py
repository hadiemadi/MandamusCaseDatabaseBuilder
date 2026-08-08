#!/usr/bin/env python3
"""
Unit tests for scripts/extraction.py.
Run with: python -m pytest tests/ -v   (or plain: python tests/test_extraction.py)

These tests use realistic docket-entry text pulled from the pattern of real
CourtListener entries (see the legacy mandamus-cloud data for examples of
this phrasing) — not synthetic nonsense — so a pass here is meaningful.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from extraction import (
    determine_procedural_posture,
    determine_outcome,
    determine_disposition_confidence,
    determine_similarity_flag,
    compute_relevance_score,
    compute_days_to_resolution,
    build_case_record,
    validate_record,
)

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


def test_procedural_posture_and_outcome_mtd_denied():
    text = "MOTION to Dismiss for failure to state a claim filed. " \
           "ORDER denying Defendants' Motion to Dismiss."
    check("posture: mtd_denied", determine_procedural_posture(text), "mtd_denied")
    check("outcome: mtd_denied", determine_outcome(text), "mtd_denied")


def test_procedural_posture_and_outcome_mtd_granted():
    text = "MOTION to Dismiss filed by defendants. " \
           "ORDER granting Motion to Dismiss. Case closed."
    check("posture: mtd_granted", determine_procedural_posture(text), "mtd_granted")
    check("outcome: mtd_granted", determine_outcome(text), "mtd_granted")


def test_settlement_clean():
    text = "MOTION to Dismiss filed. ORDER denying Motion to Dismiss. " \
           "STIPULATION OF DISMISSAL filed by all parties."
    check("outcome: settled", determine_outcome(text), "settled")
    check("disposition_confidence: high (clean settlement)",
          determine_disposition_confidence(text), "high")


def test_settlement_with_mootness_ambiguity():
    text = "Plaintiff's visa was issued. STIPULATION OF DISMISSAL filed as case is moot."
    check("disposition_confidence: needs_manual_review (settlement+moot both present)",
          determine_disposition_confidence(text), "needs_manual_review")


def test_no_markers_at_all():
    text = "Summons issued. Answer filed."
    check("posture: unknown when no markers match", determine_procedural_posture(text), "unknown")
    check("outcome: unknown when no markers match", determine_outcome(text), "unknown")
    check("disposition_confidence: unknown when no settlement language",
          determine_disposition_confidence(text), "unknown")


def test_similarity_flag():
    check("similarity flag: derivative applicant mentioned",
          determine_similarity_flag("Plaintiff is the derivative applicant on the I-140 petition."), True)
    check("similarity flag: absent when not mentioned",
          determine_similarity_flag("Plaintiff filed this action pro se."), False)


def test_relevance_score_all_four_criteria():
    record = {"outcome": "mtd_denied", "court": "cand", "similarity_to_own_case": True}
    # mtd_denied (+1) + cand is 9th circuit (+1) + similarity (+1) = 3
    # (outcome can't be both mtd_denied and settled at once, so max realistic is 3)
    check("relevance_score: mtd_denied + 9th circuit + similarity",
          compute_relevance_score(record), 3)


def test_relevance_score_settled_case():
    record = {"outcome": "settled", "court": "dcd", "similarity_to_own_case": False}
    check("relevance_score: settled only, non-9th-circuit court",
          compute_relevance_score(record), 1)


def test_relevance_score_zero():
    record = {"outcome": "unknown", "court": "dcd", "similarity_to_own_case": False}
    check("relevance_score: no criteria met", compute_relevance_score(record), 0)


def test_days_to_resolution():
    check("days_to_resolution: normal case",
          compute_days_to_resolution("2023-03-01", "2023-11-15"), 259)
    check("days_to_resolution: missing termination date returns None",
          compute_days_to_resolution("2023-03-01", None), None)


def test_full_record_build_and_validation():
    docket = {
        "docket_id": 111,
        "caseName": "Doe v. Blinken",
        "court_id": "cand",
        "docketNumber": "3:23-cv-01234",
        "dateFiled": "2023-03-01",
        "dateTerminated": "2023-11-15",
    }
    entries = [
        {"description": "MOTION to Dismiss filed.", "short_description": ""},
        {"description": "ORDER denying Motion to Dismiss.", "short_description": ""},
        {"description": "STIPULATION OF DISMISSAL filed.", "short_description": ""},
    ]
    record = build_case_record(docket, entries, has_full_opinion_text=False)

    check("built record: outcome", record["outcome"], "settled")
    check("built record: days_to_resolution", record["days_to_resolution"], 259)
    check("built record: pacer_fetch_needed (no opinion text)", record["pacer_fetch_needed"], True)
    check("built record: relevance_score (settled, cand=9th cir, no similarity match)",
          record["relevance_score"], 2)

    issues = validate_record(record, seen_docket_ids=set())
    check("valid record has no issues", issues, [])

    # now check duplicate detection
    issues_dup = validate_record(record, seen_docket_ids={111})
    check("duplicate docket_id is flagged", "duplicate_docket_id" in issues_dup, True)


def test_validation_catches_missing_fields():
    bad_record = {
        "docket_id": 222,
        "case_name": "",  # missing
        "court": "dcd",
        "docket_number": "1:22-cv-05678",
        "citation": "Smith v. Mayorkas, No. 1:22-cv-05678 (dcd)",
        "date_filed": "2022-06-10",
        "date_terminated": "2021-01-01",  # BEFORE date_filed — should be flagged
        "raw_entry_count": 0,  # should be flagged
    }
    issues = validate_record(bad_record, seen_docket_ids=set())
    check("missing case_name flagged", "missing_required_field:case_name" in issues, True)
    check("date order violation flagged", "date_terminated_before_date_filed" in issues, True)
    check("zero entries flagged", "no_docket_entries_retrieved" in issues, True)


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
