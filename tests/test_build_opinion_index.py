#!/usr/bin/env python3
"""
Tests for scripts/build_opinion_index.py using small in-memory bz2 CSV
fixtures -- no multi-GB files needed.

Verifies:
  1. Clusters and dockets both load, and the cluster->docket->court join
     actually resolves (this join is the whole reason the script exists:
     without it a 9th Circuit opinion can't be told apart from any other)
  2. The real PostgreSQL export dialect (FORCE_QUOTE *, ESCAPE '\\') parses,
     including a backslash-escaped embedded quote in a case name
  3. A cluster whose docket is missing from the dockets file doesn't crash
     the build and simply doesn't join -- real bulk snapshots are not
     guaranteed to be referentially complete
  4. Re-running against an existing index rebuilds cleanly rather than
     doubling rows (the file is a derived, rebuildable artifact)
"""

import bz2
import csv
import io
import os
import shutil
import sqlite3
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


CLUSTER_FIELDS = ["id", "date_filed", "case_name", "nature_of_suit",
                  "citation_count", "precedential_status", "docket_id"]

CLUSTER_ROWS = [
    {"id": "5001", "date_filed": "2023-04-01", "case_name": "Doe v. Blinken",
     "nature_of_suit": "", "citation_count": "7", "precedential_status": "Published",
     "docket_id": "9001"},
    {"id": "5002", "date_filed": "2022-02-02", "case_name": 'In re "Consular" Delay',
     "nature_of_suit": "", "citation_count": "0", "precedential_status": "Unpublished",
     "docket_id": "9002"},
    # docket 9999 deliberately absent from the dockets fixture
    {"id": "5003", "date_filed": "2021-01-01", "case_name": "Orphan v. Nobody",
     "nature_of_suit": "", "citation_count": "1", "precedential_status": "Published",
     "docket_id": "9999"},
]

DOCKET_FIELDS = ["id", "court_id", "docket_number", "date_terminated"]

DOCKET_ROWS = [
    {"id": "9001", "court_id": "cand", "docket_number": "3:23-cv-1", "date_terminated": "2024-01-01"},
    {"id": "9002", "court_id": "dcd", "docket_number": "1:22-cv-2", "date_terminated": "2023-06-01"},
]


def write_bulk_csv_bz2(path, fieldnames, rows):
    """Mirrors CourtListener's real export dialect: every field quoted,
    embedded quotes backslash-escaped rather than doubled."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, quoting=csv.QUOTE_ALL,
                       escapechar="\\", doublequote=False)
    w.writeheader()
    w.writerows(rows)
    with bz2.open(path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def build_fixture_index(tmpdir):
    import build_opinion_index as boi
    clusters = os.path.join(tmpdir, "opinion-clusters-test.csv.bz2")
    dockets = os.path.join(tmpdir, "dockets-test.csv.bz2")
    out = os.path.join(tmpdir, "opinion_index.sqlite")
    write_bulk_csv_bz2(clusters, CLUSTER_FIELDS, CLUSTER_ROWS)
    write_bulk_csv_bz2(dockets, DOCKET_FIELDS, DOCKET_ROWS)
    boi.run(clusters, dockets, out)
    return out, (clusters, dockets)


def test_join_resolves_cluster_to_court():
    tmpdir = tempfile.mkdtemp()
    try:
        out, _ = build_fixture_index(tmpdir)
        conn = sqlite3.connect(out)

        rows = dict(conn.execute("""
            SELECT c.id, d.court_id FROM cluster c JOIN docket d ON c.docket_id = d.id
        """).fetchall())

        check("two clusters join to a court", len(rows), 2)
        check("9th Circuit cluster resolves to cand", rows.get(5001), "cand")
        check("D.D.C. cluster resolves to dcd", rows.get(5002), "dcd")
        check_true("cluster with a missing docket does not join", 5003 not in rows)

        name = conn.execute("SELECT case_name FROM cluster WHERE id = 5002").fetchone()[0]
        check("backslash-escaped embedded quote parsed correctly",
              name, 'In re "Consular" Delay')

        status = conn.execute("SELECT precedential_status FROM cluster WHERE id = 5001").fetchone()[0]
        check("precedential_status preserved for ranking", status, "Published")

        cites = conn.execute("SELECT citation_count FROM cluster WHERE id = 5001").fetchone()[0]
        check("citation_count stored as an integer for ranking", cites, 7)
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_orphan_cluster_does_not_crash_build():
    tmpdir = tempfile.mkdtemp()
    try:
        out, _ = build_fixture_index(tmpdir)  # must not raise
        conn = sqlite3.connect(out)
        total = conn.execute("SELECT COUNT(*) FROM cluster").fetchone()[0]
        check("all clusters loaded even when one docket is missing", total, 3)
        conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_rebuild_is_idempotent():
    tmpdir = tempfile.mkdtemp()
    try:
        import build_opinion_index as boi
        out, (clusters, dockets) = build_fixture_index(tmpdir)
        boi.run(clusters, dockets, out)  # rebuild over the existing file

        conn = sqlite3.connect(out)
        check("rebuild does not duplicate cluster rows",
              conn.execute("SELECT COUNT(*) FROM cluster").fetchone()[0], 3)
        check("rebuild does not duplicate docket rows",
              conn.execute("SELECT COUNT(*) FROM docket").fetchone()[0], 2)
        conn.close()
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
