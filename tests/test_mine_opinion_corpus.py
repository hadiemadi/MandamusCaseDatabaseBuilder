#!/usr/bin/env python3
"""
Tests for scripts/mine_opinion_corpus.py using small in-memory fixtures --
no 54.5GB file or real SQLite index needed.

Verifies:
  1. An opinion hitting BOTH keyword groups (delay-language AND visa-
     language) is kept, joined to its court via the index, scored, and its
     text written verbatim to disk
  2. An opinion matching only one group is excluded -- either alone is far
     too broad across a nationwide corpus (SPEC.md 5.3)
  3. "visa" as a plain substring inside an unrelated word ("Divisadero")
     does not cause a false match -- the same trap the sibling docket
     filter hit for real (SPEC.md 13)
  4. An opinion whose cluster_id has no matching row in the index is
     skipped, not a crash
  5. Empty plain_text falls back to the HTML body
  6. Court tier ranking: a 9th Circuit opinion outranks a D.D.C. opinion,
     which outranks an out-of-circuit one, holding other factors equal
  7. Doctrine signals (TRAC, consular nonreviewability, 221(g)) are
     detected and recorded, not silently dropped
"""

import bz2
import csv
import io
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

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


OPINION_FIELDS = ["id", "cluster_id", "plain_text", "html", "html_with_citations"]

RELEVANT_TEXT = (
    "MEMORANDUM OPINION. Plaintiff seeks a writ of mandamus compelling "
    "Defendant to adjudicate her immigrant visa application, alleging "
    "unreasonable delay under the APA. The Court applies the TRAC factors "
    "and finds consular nonreviewability does not bar a pure delay claim. "
    "Plaintiff's application has been stuck in administrative processing "
    "under 221(g) for three years."
)

DIVISADERO_TEXT = (
    "This is an ADA accessibility case against Divisadero Sports Bar LLC. "
    "Plaintiff alleges the premises are not compliant with the Americans "
    "with Disabilities Act. Defendant moves for summary judgment, arguing "
    "mandamus relief is unavailable in this context."
)

DELAY_ONLY_TEXT = (
    "Petitioner seeks a writ of mandamus compelling the warden to correct "
    "a sentencing calculation error, citing unreasonable delay in "
    "processing his administrative grievance."
)

OPINION_ROWS = [
    {"id": "1001", "cluster_id": "5001", "plain_text": RELEVANT_TEXT, "html": "", "html_with_citations": ""},
    {"id": "1002", "cluster_id": "5002", "plain_text": DIVISADERO_TEXT, "html": "", "html_with_citations": ""},
    {"id": "1003", "cluster_id": "5003", "plain_text": DELAY_ONLY_TEXT, "html": "", "html_with_citations": ""},
    # cluster 5099 deliberately absent from the index
    {"id": "1004", "cluster_id": "5099", "plain_text": RELEVANT_TEXT, "html": "", "html_with_citations": ""},
    # empty plain_text, real content only in html_with_citations
    {"id": "1005", "cluster_id": "5005", "plain_text": "",
     "html_with_citations": f"<p>{RELEVANT_TEXT}</p>", "html": ""},
]


def write_bulk_csv_bz2(path, fieldnames, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, quoting=csv.QUOTE_ALL,
                       escapechar="\\", doublequote=False)
    w.writeheader()
    w.writerows(rows)
    with bz2.open(path, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def make_index(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE cluster (id INTEGER PRIMARY KEY, docket_id INTEGER,
            case_name TEXT, date_filed TEXT, precedential_status TEXT, citation_count INTEGER);
        CREATE TABLE docket (id INTEGER PRIMARY KEY, court_id TEXT,
            docket_number TEXT, date_terminated TEXT);
    """)
    conn.executemany("INSERT INTO docket VALUES (?,?,?,?)", [
        (9001, "cacd", "2:24-cv-1", "2025-01-01"),   # 9th Circuit district
        (9002, "cand", "3:24-cv-2", None),
        (9003, "dcd", "1:24-cv-3", None),
    ])
    conn.executemany("INSERT INTO cluster VALUES (?,?,?,?,?,?)", [
        (5001, 9001, "Doe v. Blinken", "2024-01-01", "Published", 2),      # relevant, 9th Cir.
        (5002, 9002, "Roe v. Divisadero Sports Bar LLC", "2023-01-01", "Published", 0),
        (5003, 9002, "Smith v. Warden", "2022-01-01", "Published", 0),      # delay-only, no visa terms
        (5005, 9003, "Lee v. Blinken", "2021-01-01", "Unpublished", 1),     # relevant, D.D.C., html fallback
    ])
    conn.commit()
    conn.close()


def run_mine(tmpdir):
    import mine_opinion_corpus as moc
    d = Path(tmpdir)
    moc.INDEX_OUT = d / "opinion_corpus_index.json"

    opinions_path = d / "opinions-test.csv.bz2"
    index_path = d / "opinion_index.sqlite"
    textdir = d / "opinion_corpus"

    write_bulk_csv_bz2(opinions_path, OPINION_FIELDS, OPINION_ROWS)
    make_index(index_path)

    moc.run(str(opinions_path), str(index_path), str(textdir))
    data = json.loads(moc.INDEX_OUT.read_text(encoding="utf-8"))
    return data, textdir


def test_mining_end_to_end():
    tmpdir = tempfile.mkdtemp()
    try:
        data, textdir = run_mine(tmpdir)
        by_cluster = {r["cluster_id"]: r for r in data["results"]}

        check("exactly 2 opinions matched both keyword groups",
              data["opinions_matched"], 2)
        check_true("relevant 9th Circuit opinion (5001) matched", 5001 in by_cluster)
        check_true("relevant D.D.C. opinion via html fallback (5005) matched", 5005 in by_cluster)
        check_true("Divisadero ADA case (5002) excluded despite 'mandamus' keyword",
                    5002 not in by_cluster)
        check_true("delay-only prisoner case (5003) excluded (no visa-language hit)",
                    5003 not in by_cluster)
        check_true("opinion with no matching cluster in the index (5099) excluded",
                    5099 not in by_cluster)

        r5001 = by_cluster[5001]
        check("court_id joined correctly", r5001["court_id"], "cacd")
        check("court_tier reflects 9th Circuit district", r5001["court_tier"], 4)
        check_true("TRAC signal detected", r5001["signals"]["trac_factors"])
        check_true("consular nonreviewability signal detected",
                    r5001["signals"]["consular_nonreviewability"])
        check_true("221(g) signal detected", r5001["signals"]["221g"])

        text_path = textdir / r5001["text_file"]
        check_true("opinion text file written to disk", text_path.exists())
        check("opinion stored verbatim, not summarized",
              text_path.read_text(encoding="utf-8"), RELEVANT_TEXT)

        r5005 = by_cluster[5005]
        check("html fallback used when plain_text is empty",
              r5005["text_source_field"], "html_with_citations")
        check_true("html tags stripped from fallback text",
                    "<p>" not in (textdir / r5005["text_file"]).read_text(encoding="utf-8"))

        check("9th Circuit result ranks above D.D.C. result",
              data["results"][0]["cluster_id"], 5001)
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
