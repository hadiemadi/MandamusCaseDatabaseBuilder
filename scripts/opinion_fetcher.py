#!/usr/bin/env python3
"""
Fetches the FULL TEXT of reported decisions, free, from CourtListener's
case-law (Opinions) API. SPEC.md section 14, tier 2.

Why this exists separately from collector.py: collector.py reads the RECAP
docket side, which gives one-line docket entries -- enough to classify WHAT
happened to a case, almost never enough to know WHY. Reported decisions
(the `594 F. Supp. 3d 76` style citations in data/seed_citations.json) are
published opinions, and their full text is free here. This is the endpoint
that closes the "what but not why" gap without spending a cent on PACER.

Deliberately does NOT summarize anything. SPEC.md section 6.5 keeps
`court_reasoning_summary` a human-filled field; this script's job is to put
the real text in front of the user, not to interpret it.

Shares one daily request budget with collector.py through api_client and the
same checkpoint file -- see SPEC.md section 8.

USAGE
  export COURTLISTENER_TOKEN="..."
  python scripts/opinion_fetcher.py
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from api_client import (
    SEARCH_ENDPOINT,
    OPINIONS_ENDPOINT,
    EMPTY_CHECKPOINT,
    load_json,
    save_json,
    throttled_get,
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
SEED_FILE = DATA_DIR / "seed_citations.json"
CHECKPOINT_FILE = DATA_DIR / "checkpoint.json"
OPINIONS_DIR = DATA_DIR / "opinions"

# Highest-value first: the user's case is in the 9th Circuit (SPEC.md 15).
# D.D.C. next -- out of circuit, but most consular-delay law is made there.
CIRCUIT_PRIORITY = {"9th": 0, "DC": 1}
DEFAULT_CIRCUIT_PRIORITY = 2


def load_checkpoint():
    return load_json(CHECKPOINT_FILE, dict(EMPTY_CHECKPOINT))


def save_checkpoint(cp):
    save_json(CHECKPOINT_FILE, cp)


def slugify(case_name):
    slug = re.sub(r"[^a-z0-9]+", "-", (case_name or "unknown").lower())
    return slug.strip("-")[:80]


def normalize_name(name):
    """Strip punctuation/case so 'Al-Gharawy v. DHS' and 'Al Gharawy v. D.H.S.'
    compare equal enough to catch an obvious mismatch."""
    return re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()


def name_overlap(expected, actual):
    """Cheap similarity: fraction of the expected case name's words present in
    the returned one. Used only to FLAG a doubtful match for human review --
    never to silently discard or silently trust a result."""
    e = set(normalize_name(expected).split())
    a = set(normalize_name(actual).split())
    e.discard("v")
    a.discard("v")
    if not e:
        return 0.0
    return len(e & a) / len(e)


def sort_key(case):
    """9th Circuit first, then D.C., then everything else. Within a tier,
    favorable-to-plaintiff decisions first -- those are the ones the eventual
    complaint will cite affirmatively."""
    circuit_rank = CIRCUIT_PRIORITY.get(case.get("circuit"), DEFAULT_CIRCUIT_PRIORITY)
    direction_rank = 0 if case.get("direction") == "favorable_to_plaintiff" else 1
    return (circuit_rank, direction_rank, case.get("case_name", ""))


def search_opinion(case, checkpoint):
    """Find the published opinion for one seed case.

    Returns (result, budget_ok). Distinguishing "no such opinion" from "we ran
    out of API budget" matters: the first is a final answer worth recording,
    the second must leave the case pending so a later run retries it."""
    params = {
        "q": case["case_name"],
        "type": "o",
        "court": case.get("court", ""),
    }
    data = throttled_get(SEARCH_ENDPOINT, params, checkpoint)
    if data is None:
        return None, False
    results = data.get("results", [])
    return (results[0] if results else None), True


def fetch_opinion_text(cluster_id, checkpoint):
    """Pull the opinion body for a cluster. Prefers plain_text; falls back to
    html fields, which are populated for older opinions where plain_text is
    empty. Returns (text, source_field) or (None, reason)."""
    data = throttled_get(OPINIONS_ENDPOINT, {"cluster": cluster_id}, checkpoint)
    if data is None:
        return None, "budget_exhausted"
    results = data.get("results", [])
    if not results:
        return None, "no_opinion_records"

    for field in ("plain_text", "html_with_citations", "html", "html_lawbox", "xml_harvard"):
        for op in results:
            text = (op.get(field) or "").strip()
            if text:
                return text, field
    return None, "all_text_fields_empty"


def process_case(case, checkpoint):
    """Returns a status string; mutates `case` in place with results.
    Leaves opinion_fetch_status untouched on budget exhaustion so the case
    stays pending for a later run."""
    result, budget_ok = search_opinion(case, checkpoint)
    if not budget_ok:
        return "budget_exhausted"
    if result is None:
        case["opinion_fetch_status"] = "not_found"
        case["opinion_fetch_note"] = "no case-law search hit; may be unreported"
        return "not_found"

    matched_name = result.get("caseName", "")
    overlap = name_overlap(case["case_name"], matched_name)

    cluster_id = result.get("cluster_id") or result.get("id")
    if not cluster_id:
        case["opinion_fetch_status"] = "not_found"
        return "not_found"

    text, source = fetch_opinion_text(cluster_id, checkpoint)
    if text is None:
        case["opinion_fetch_status"] = (
            "budget_exhausted" if source == "budget_exhausted" else "not_free_needs_pacer"
        )
        case["opinion_fetch_note"] = source
        return case["opinion_fetch_status"]

    OPINIONS_DIR.mkdir(exist_ok=True)
    path = OPINIONS_DIR / f"{slugify(case['case_name'])}.txt"
    path.write_text(text, encoding="utf-8")

    case["opinion_fetch_status"] = "found_free_opinion"
    case["opinion_text_path"] = f"data/opinions/{path.name}"
    case["opinion_text_chars"] = len(text)
    case["opinion_text_source_field"] = source
    case["matched_case_name"] = matched_name
    case["match_confidence"] = round(overlap, 2)
    case["opinion_absolute_url"] = result.get("absolute_url", "")
    case["fetched_at"] = datetime.now(timezone.utc).isoformat()
    # Below ~0.6 word overlap the search probably landed on a different case.
    # Flag it loudly rather than quietly trusting it -- a wrong opinion is
    # worse than a missing one when the output feeds a legal filing.
    if overlap < 0.6:
        case["needs_manual_match_check"] = True
    return "found_free_opinion"


def run():
    seed = load_json(SEED_FILE, None)
    if not seed:
        print(f"FATAL: {SEED_FILE} not found or empty.", file=sys.stderr)
        sys.exit(1)

    checkpoint = load_checkpoint()
    cases = seed["cases"]
    pending = [c for c in cases if c.get("opinion_fetch_status") == "pending"]
    pending.sort(key=sort_key)

    print(f"{len(pending)} case(s) pending of {len(cases)} total. "
          f"Budget used today: {checkpoint['requests_made_today']}")

    tally = {}
    for case in pending:
        status = process_case(case, checkpoint)

        if status == "budget_exhausted":
            print(f"  stopping: daily request budget exhausted "
                  f"(used {checkpoint['requests_made_today']}). "
                  f"'{case['case_name']}' stays pending for the next run.")
            break

        tally[status] = tally.get(status, 0) + 1
        flag = "  [CHECK MATCH]" if case.get("needs_manual_match_check") else ""
        print(f"  [{case.get('circuit','?'):>3}] {case['case_name'][:38]:38} -> {status}"
              f"{flag}")

        save_json(SEED_FILE, seed)
        save_checkpoint(checkpoint)

    save_json(SEED_FILE, seed)
    save_checkpoint(checkpoint)

    print("\nSummary:", tally if tally else "nothing processed")
    remaining = sum(1 for c in cases if c.get("opinion_fetch_status") == "pending")
    print(f"{remaining} still pending; rerun after the daily window resets.")


if __name__ == "__main__":
    run()
