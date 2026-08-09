#!/usr/bin/env python3
"""
Tests for scripts/bulk_docket_filter.py using a small in-memory bz2 CSV
fixture -- no real 5GB download needed.

The fixture mirrors CourtListener's actual PostgreSQL COPY export dialect
(FORCE_QUOTE *, ESCAPE '\\') per their own bulk-data release notes, so a
real quoting bug would be caught here rather than on the real file.

Verifies:
  1. A 9th Circuit consular/mandamus docket terminated after 2020 matches
  2. A docket in an irrelevant court is excluded despite a keyword hit
  3. A docket terminated before 2020-01-01 is excluded
  4. A docket matched purely on nature_of_suit (APA code), with no keyword
     hit in case_name/cause, is still included
  5. An unrelated case (no keyword, no NOS hit) is excluded
  6. Output field names match what collector.py's search_dockets() results
     already use -- so compute_priority_score/build_case_record need no
     changes to consume bulk-discovered candidates
  7. A backslash-escaped embedded double-quote in a case name parses
     correctly (real-world quoting gotcha, not a hypothetical one)
"""

import bz2
import csv
import io
import os
import shutil
import sys
import tempfile

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


FIELDNAMES = [
    "id", "case_name", "case_name_full", "slug", "docket_number", "court_id",
    "cause", "date_filed", "date_terminated", "nature_of_suit",
]

ROWS = [
    # 9th Circuit, keyword hit, terminated after 2020 -- should match
    {
        "id": "1001", "case_name": "Doe v. Blinken",
        "case_name_full": "John Doe v. Antony Blinken, Secretary of State",
        "slug": "doe-v-blinken", "docket_number": "3:23-cv-00001",
        "court_id": "cand", "cause": "5:706 Mandamus: Unreasonable delay",
        "date_filed": "2023-01-01", "date_terminated": "2025-06-01",
        "nature_of_suit": "440 Other Civil Rights",
    },
    # irrelevant court -- should NOT match despite keyword hit
    {
        "id": "1002", "case_name": "Roe v. Rubio",
        "case_name_full": "Jane Roe v. Marco Rubio, Secretary of State",
        "slug": "roe-v-rubio", "docket_number": "1:23-cv-00099",
        "court_id": "nysd", "cause": "5:706 Mandamus: Unreasonable delay",
        "date_filed": "2023-01-01", "date_terminated": "2025-06-01",
        "nature_of_suit": "440 Other Civil Rights",
    },
    # 9th Circuit, keyword hit, terminated before 2020 -- should NOT match
    {
        "id": "1003", "case_name": "Old v. Pompeo",
        "case_name_full": "Old Plaintiff v. Mike Pompeo, Secretary of State",
        "slug": "old-v-pompeo", "docket_number": "2:18-cv-00050",
        "court_id": "cacd", "cause": "5:706 Mandamus: Unreasonable delay",
        "date_filed": "2018-01-01", "date_terminated": "2019-06-01",
        "nature_of_suit": "440 Other Civil Rights",
    },
    # 9th Circuit, NOS 899 (APA) hit, no keyword in case_name/cause -- should match
    {
        "id": "1004", "case_name": "Smith v. Department of State",
        "case_name_full": "Jane Smith v. United States Department of State",
        "slug": "smith-v-dos", "docket_number": "9:24-cv-00042",
        "court_id": "azd", "cause": "review of agency action",
        "date_filed": "2024-01-01", "date_terminated": "2025-01-01",
        "nature_of_suit": "899 Administrative Procedure Act",
    },
    # unrelated contract case -- no keyword, no NOS hit -- should NOT match
    {
        "id": "1005", "case_name": "Acme Corp v. Widget Inc",
        "case_name_full": "Acme Corporation v. Widget Incorporated",
        "slug": "acme-v-widget", "docket_number": "3:24-cv-00777",
        "court_id": "cand", "cause": "contract dispute",
        "date_filed": "2024-01-01", "date_terminated": "2025-01-01",
        "nature_of_suit": "190 Other Contract",
    },
    # D.D.C. (out-of-circuit but high-value), embedded escaped quote in case name
    {
        "id": "1006", "case_name": 'In re "Consular" Processing Delay',
        "case_name_full": 'In re "Consular" Processing Delay Mandamus Action',
        "slug": "in-re-consular-processing-delay", "docket_number": "1:22-cv-00333",
        "court_id": "dcd", "cause": "5:706 Mandamus: Unreasonable delay",
        "date_filed": "2022-01-01", "date_terminated": "2023-01-01",
        "nature_of_suit": "440 Other Civil Rights",
    },
    # 9th Circuit, "visa" appears only as a substring of an unrelated word
    # ("Divisadero") -- a real false positive a plain `in` check produced on
    # the live 2026-06-30 dump ("Garcia v. Divisadero Sports Bar LLC",
    # an ADA case). Word-boundary matching must exclude this.
    {
        "id": "1007", "case_name": "Garcia v. Divisadero Sports Bar LLC",
        "case_name_full": "Garcia v. Divisadero Sports Bar LLC",
        "slug": "garcia-v-divisadero-sports-bar-llc", "docket_number": "4:21-cv-06548",
        "court_id": "cand", "cause": "42:12101 Americans w/ Disabilities Act (ADA)",
        "date_filed": "2021-08-25", "date_terminated": "2021-10-20",
        "nature_of_suit": "American with Disabilities - Other",
    },
    # 9th Circuit, matches on "221(g)" specifically -- a keyword ending in a
    # non-alnum character, exercising the boundary-pattern edge case.
    {
        "id": "1008", "case_name": "Chen v. Blinken",
        "case_name_full": "Chen v. Antony Blinken, Secretary of State",
        "slug": "chen-v-blinken", "docket_number": "2:23-cv-00456",
        "court_id": "cacd", "cause": "8:1329 221(g) refusal, unreasonable delay",
        "date_filed": "2023-01-01", "date_terminated": "2024-01-01",
        "nature_of_suit": "465 Other Immigration Actions",
    },
]


def make_bulk_csv_bz2(path, rows):
    """Writes a .csv.bz2 fixture matching CourtListener's actual export
    dialect: every field double-quoted (FORCE_QUOTE *), embedded double
    quotes backslash-escaped (ESCAPE '\\'), not doubled."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=FIELDNAMES, quoting=csv.QUOTE_ALL,
        escapechar="\\", doublequote=False,
    )
    writer.writeheader()
    writer.writerows(rows)
    with bz2.open(path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def test_bulk_filter_matches_and_excludes_correctly():
    tmpdir = tempfile.mkdtemp()
    try:
        csv_path = os.path.join(tmpdir, "dockets-test.csv.bz2")
        make_bulk_csv_bz2(csv_path, ROWS)

        import bulk_docket_filter as bdf
        candidates = list(bdf.filter_bulk_dockets(csv_path))
        by_id = {c["docket_id"]: c for c in candidates}

        check("exactly 4 of 8 rows matched", len(candidates), 4)
        check_true("9th Circuit keyword-hit case matched", 1001 in by_id)
        check_true("irrelevant-court case excluded despite keyword hit", 1002 not in by_id)
        check_true("pre-2020-termination case excluded", 1003 not in by_id)
        check_true("NOS-899-only match (no keyword) included", 1004 in by_id)
        check_true("unrelated contract case excluded", 1005 not in by_id)
        check_true("D.D.C. case with escaped-quote case name matched", 1006 in by_id)
        check_true("'visa' inside 'Divisadero' is NOT a false-positive match",
                    1007 not in by_id)
        check_true("'221(g)' (non-alnum-ending keyword) matches correctly", 1008 in by_id)

        doe = by_id[1001]
        check("caseName field matches collector.py's search-result schema",
              doe["caseName"], "Doe v. Blinken")
        check("docket_absolute_url built from id + slug",
              doe["docket_absolute_url"], "/docket/1001/doe-v-blinken/")
        check("dateTerminated field name matches collector.py's schema",
              doe["dateTerminated"], "2025-06-01")

        consular = by_id[1006]
        check("backslash-escaped embedded quote parsed correctly",
              consular["caseName"], 'In re "Consular" Processing Delay')
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_run_writes_output_file():
    tmpdir = tempfile.mkdtemp()
    try:
        import bulk_docket_filter as bdf
        from pathlib import Path
        bdf.OUTPUT_FILE = Path(tmpdir) / "bulk_discovered_dockets.json"

        # Real CourtListener filenames encode the snapshot date this way --
        # collector.py needs it to know what postdates the snapshot.
        csv_path = os.path.join(tmpdir, "dockets-2026-06-30.csv.bz2")
        make_bulk_csv_bz2(csv_path, ROWS)

        bdf.run(csv_path)

        check_true("output file written", bdf.OUTPUT_FILE.exists())
        import json
        written = json.loads(bdf.OUTPUT_FILE.read_text(encoding="utf-8"))
        check("output file contains the 4 matches", len(written["candidates"]), 4)
        check("snapshot_date parsed from filename", written["snapshot_date"], "2026-06-30")
        check_true("generated_at timestamp present", written.get("generated_at"))
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
