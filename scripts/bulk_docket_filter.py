#!/usr/bin/env python3
"""
One-time (quarterly) local filter over CourtListener's free bulk Dockets
CSV. SPEC.md section 5.2.

Why this exists: collector.py's live search API discovery competes with
docket-entries fetching for the same daily request budget (SPEC.md section
8). CourtListener publishes its entire dockets table as a free, unlimited,
no-membership-required bulk CSV snapshot -- confirmed 2026-08-09 by
browsing wiki.free.law and the actual S3 bucket (SPEC.md section 13). Doing
discovery here, once per quarterly refresh, against the full national
dataset for $0 means every API request collector.py makes afterward goes
toward the one thing bulk data can't replace: docket-entries (PACER-gated,
not included in the bulk export).

Streams and filters the .csv.bz2 in one pass so the multi-GB uncompressed
CSV is never fully materialized on disk or in memory -- only matching rows
are kept.

Not part of the 4-hour collect.yml schedule. Run manually whenever a new
quarterly dump lands (courtlistener publishes on the last day of Mar/Jun/
Sep/Dec):

USAGE
  python scripts/bulk_docket_filter.py path/to/dockets-YYYY-MM-DD.csv.bz2
"""

import bz2
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from extraction import NINTH_CIRCUIT_DISTRICTS, HIGH_VALUE_OUT_OF_CIRCUIT, PRIORITY_CAUSE_KEYWORDS

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
OUTPUT_FILE = DATA_DIR / "bulk_discovered_dockets.json"

TERMINATED_AFTER = "2020-01-01"
RELEVANT_COURTS = NINTH_CIRCUIT_DISTRICTS | HIGH_VALUE_OUT_OF_CIRCUIT

# Deliberately broader than collector.py's live SEARCH_QUERY (which ANDs
# three keyword groups together) -- over-matching here is the safe failure
# mode. Bulk-discovered candidates are just a pre-filter; relevance_score
# (extraction.py) does the real sorting later, and SPEC.md's existing
# philosophy is "store everything, let scoring sort it out" (section 13
# decision log). Under-matching would silently drop a real case forever.
CAUSE_KEYWORDS = PRIORITY_CAUSE_KEYWORDS + ["consular", "visa"]


def _boundary_pattern(keyword):
    """Word-boundary match, not plain substring -- a real run caught "visa"
    matching inside "Divisadero" (di-visa-dero), a false positive plain `in`
    matching would silently keep producing. \\b only makes sense next to an
    alnum character (e.g. "221(g)" ends in ")", where a trailing \\b would
    never fire), so boundaries are added only where the keyword's edge is
    alnum. Mirrors the \\b-delimited regex style already used in
    extraction.py's marker lists."""
    escaped = re.escape(keyword)
    prefix = r"\b" if keyword[0].isalnum() else ""
    suffix = r"\b" if keyword[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


CAUSE_KEYWORD_PATTERNS = [_boundary_pattern(kw) for kw in CAUSE_KEYWORDS]

# Nature-of-suit code conventionally used for APA/agency-review suits on the
# federal civil cover sheet. This is a HYPOTHESIS, not yet confirmed against
# real bulk data rows -- inspect actual matched nature_of_suit values on the
# first real run before trusting it as a signal (SPEC.md 13 decision log).
APA_NATURE_OF_SUIT_CODE = "899"

# The real export uses PostgreSQL's COPY ... FORCE_QUOTE * ESCAPE '\' —
# every field quoted, embedded quotes backslash-escaped rather than doubled.
# Standard csv.reader defaults (doublequote=True) would mis-parse that.
CSV_DIALECT_KWARGS = dict(delimiter=",", quotechar='"', escapechar="\\", doublequote=False)


def is_relevant(row):
    court = (row.get("court_id") or "").lower()
    if court not in RELEVANT_COURTS:
        return False

    date_terminated = row.get("date_terminated") or ""
    if not date_terminated or date_terminated < TERMINATED_AFTER:
        return False

    haystack = " ".join([
        row.get("case_name") or "",
        row.get("case_name_full") or "",
        row.get("cause") or "",
    ])
    keyword_hit = any(p.search(haystack) for p in CAUSE_KEYWORD_PATTERNS)
    nos_hit = APA_NATURE_OF_SUIT_CODE in (row.get("nature_of_suit") or "")

    return keyword_hit or nos_hit


def to_candidate(row):
    """Field names deliberately match what collector.py's search_dockets()
    results already use (caseName, court_id, docketNumber, dateFiled,
    dateTerminated, docket_absolute_url, cause) -- so compute_priority_score
    and build_case_record (extraction.py) need no changes to consume these."""
    docket_id = row.get("id")
    slug = row.get("slug") or ""
    return {
        "docket_id": int(docket_id) if docket_id else None,
        "caseName": row.get("case_name"),
        "court_id": row.get("court_id"),
        "docketNumber": row.get("docket_number"),
        "dateFiled": row.get("date_filed") or None,
        "dateTerminated": row.get("date_terminated") or None,
        "docket_absolute_url": f"/docket/{docket_id}/{slug}/" if docket_id else None,
        "cause": row.get("cause"),
        "nature_of_suit": row.get("nature_of_suit"),
        "source": "bulk_dockets",
    }


def filter_bulk_dockets(csv_path):
    """Streams the bz2-compressed CSV and yields matching candidate dicts.
    Never holds the full decompressed file in memory or on disk."""
    matched = 0
    scanned = 0
    with bz2.open(csv_path, mode="rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, **CSV_DIALECT_KWARGS)
        for row in reader:
            scanned += 1
            if is_relevant(row):
                matched += 1
                yield to_candidate(row)
            if scanned % 500_000 == 0:
                print(f"  scanned {scanned:,} rows, matched {matched:,} so far...", file=sys.stderr)
    print(f"Scanned {scanned:,} total rows, matched {matched:,}.", file=sys.stderr)


def extract_snapshot_date(csv_path):
    """CourtListener names bulk files by their generation date, e.g.
    dockets-2026-06-30.csv.bz2 -- collector.py needs this date to know which
    cases postdate the snapshot and still need a live-search catch-up."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(csv_path).name)
    return m.group(1) if m else None


def run(csv_path):
    candidates = list(filter_bulk_dockets(csv_path))
    output = {
        "snapshot_date": extract_snapshot_date(csv_path),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidates": candidates,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"Wrote {len(candidates)} candidate docket(s) to {OUTPUT_FILE} "
          f"(snapshot {output['snapshot_date']})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("USAGE: python scripts/bulk_docket_filter.py path/to/dockets-YYYY-MM-DD.csv.bz2",
              file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
