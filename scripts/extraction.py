#!/usr/bin/env python3
"""
Pure, testable extraction logic — no network calls, no I/O.
Every function here takes plain data in and returns plain data out, so it
can be unit-tested in isolation (see tests/test_extraction.py) without ever
touching the CourtListener API.

This module implements exactly SPEC.md section 6 (schema) and section 7
(validation). If you change a rule here, update SPEC.md section 6 to match —
the two must never diverge.
"""

import re
from datetime import datetime

# ---------------------------------------------------------------------------
# SPEC.md 6.2 — procedural posture and outcome markers
# ---------------------------------------------------------------------------

PROCEDURAL_STAGE_MARKERS = [
    (r"\bmotion to dismiss\b", "motion_to_dismiss_filed"),
    (r"\border.{0,20}(denying|denies).{0,20}motion to dismiss\b", "mtd_denied"),
    (r"\border.{0,20}(granting|grants).{0,20}motion to dismiss\b", "mtd_granted"),
    (r"\bmotion for summary judgment\b", "summary_judgment_stage"),
    (r"\bstipulation of dismissal\b", "settled_stipulated_dismissal"),
    (r"\bnotice of voluntary dismissal\b", "settled_voluntary_dismissal"),
    (r"\bjudgment\b", "judgment_entered"),
]

OUTCOME_MARKERS = [
    (r"\bstipulation of dismissal\b", "settled"),
    (r"\bnotice of voluntary dismissal\b", "settled"),
    (r"\border.{0,20}(denying|denies).{0,20}motion to dismiss\b", "mtd_denied"),
    (r"\border.{0,20}(granting|grants).{0,20}motion to dismiss\b", "mtd_granted"),
    (r"\bmoot\b", "possibly_moot"),
    (r"\bjudgment for (the )?plaintiff", "plaintiff_win"),
    (r"\bjudgment for (the )?defendant", "defendant_win"),
]

MOOTNESS_AMBIGUITY_MARKERS = [r"\bmoot\b", r"\bvisa (has been |was )?issued\b"]

SIMILARITY_MARKERS = [
    r"\bderivative\b",
    r"\bprincipal applicant\b",
    r"\bspouse.{0,20}approved\b",
]

# SPEC.md 6.4 — 9th Circuit district court IDs used in relevance scoring
NINTH_CIRCUIT_DISTRICTS = {
    "cand", "caed", "cacd", "casd",
    "azd", "nvd", "ord", "waed", "wawd", "hid", "idd", "mtd", "akd",
}

REQUIRED_FIELDS = ["docket_id", "case_name", "court", "docket_number", "citation"]


def match_first(text, markers):
    text_lower = (text or "").lower()
    for pattern, label in markers:
        if re.search(pattern, text_lower):
            return label
    return None


def determine_procedural_posture(entries_text_blob):
    text = (entries_text_blob or "").lower()
    matches = [label for pattern, label in PROCEDURAL_STAGE_MARKERS if re.search(pattern, text)]
    if not matches:
        return "unknown"
    order = [label for _, label in PROCEDURAL_STAGE_MARKERS]
    matches.sort(key=lambda m: order.index(m))
    return matches[-1]  # "furthest" stage per the marker list's own ordering


def determine_outcome(entries_text_blob):
    return match_first(entries_text_blob, OUTCOME_MARKERS) or "unknown"


def determine_disposition_confidence(entries_text_blob):
    text = (entries_text_blob or "").lower()
    ambiguous = any(re.search(p, text) for p in MOOTNESS_AMBIGUITY_MARKERS)
    settled = "stipulation of dismissal" in text or "notice of voluntary dismissal" in text
    if settled and ambiguous:
        return "needs_manual_review"
    if settled:
        return "high"
    return "unknown"


def determine_similarity_flag(entries_text_blob):
    text = (entries_text_blob or "").lower()
    return any(re.search(p, text) for p in SIMILARITY_MARKERS)


def compute_relevance_score(record):
    score = 0
    if record.get("outcome") == "mtd_denied":
        score += 1
    if (record.get("court") or "").lower() in NINTH_CIRCUIT_DISTRICTS:
        score += 1
    if record.get("outcome") == "settled":
        score += 1
    if record.get("similarity_to_own_case"):
        score += 1
    return score


def compute_days_to_resolution(date_filed, date_terminated):
    if not date_filed or not date_terminated:
        return None
    try:
        d1 = datetime.fromisoformat(date_filed)
        d2 = datetime.fromisoformat(date_terminated)
        return (d2 - d1).days
    except ValueError:
        return None


def build_case_record(docket, entries, has_full_opinion_text=False):
    """docket: dict from CourtListener docket search result.
    entries: list of dicts with 'description' / 'short_description' keys."""
    entries_text_blob = " ".join(
        (e.get("description") or "") + " " + (e.get("short_description") or "")
        for e in entries
    )

    date_filed = docket.get("dateFiled")
    date_terminated = docket.get("dateTerminated")

    record = {
        "docket_id": docket.get("id"),
        "case_name": docket.get("caseName"),
        "court": docket.get("court_id"),
        "docket_number": docket.get("docketNumber"),
        "date_filed": date_filed,
        "date_terminated": date_terminated,
        "days_to_resolution": compute_days_to_resolution(date_filed, date_terminated),
        "citation": f"{docket.get('caseName', 'Unknown')}, No. {docket.get('docketNumber', '?')} "
                    f"({docket.get('court_id', '?')})",
        "source_url": f"https://www.courtlistener.com/docket/{docket.get('id')}/",

        "procedural_posture": determine_procedural_posture(entries_text_blob),
        "outcome": determine_outcome(entries_text_blob),
        "disposition_confidence": determine_disposition_confidence(entries_text_blob),
        "similarity_to_own_case": determine_similarity_flag(entries_text_blob),
        "has_full_opinion_text": has_full_opinion_text,
        "pacer_fetch_needed": not has_full_opinion_text,

        # SPEC.md 6.5 — deliberately blank, not derived here
        "court_reasoning_summary": "",
        "trac_factor_details": {},

        "raw_entry_count": len(entries),
    }
    record["relevance_score"] = compute_relevance_score(record)
    return record


def validate_record(record, seen_docket_ids):
    """SPEC.md section 7. Returns a list of issue strings (empty = clean)."""
    issues = []
    for field in REQUIRED_FIELDS:
        if not record.get(field):
            issues.append(f"missing_required_field:{field}")

    if record.get("docket_id") in seen_docket_ids:
        issues.append("duplicate_docket_id")

    date_filed = record.get("date_filed")
    date_terminated = record.get("date_terminated")
    if date_filed and date_terminated:
        try:
            d1 = datetime.fromisoformat(date_filed)
            d2 = datetime.fromisoformat(date_terminated)
            if d2 < d1:
                issues.append("date_terminated_before_date_filed")
        except ValueError:
            issues.append("unparseable_date")

    if record.get("raw_entry_count", 0) == 0:
        issues.append("no_docket_entries_retrieved")

    return issues
