#!/usr/bin/env python3
"""
Mines the full text of every relevant published opinion out of
CourtListener's 54.5GB opinions bulk file. SPEC.md section 5.3.

This replaces the API path in scripts/opinion_fetcher.py, which fetched the
same text one case at a time -- ~2 requests each, 13s apart, capped at 120
requests/day -- from data we already hold locally in full. Beyond being
slower, name-based API search guessed wrong: of 4 low-confidence matches on
its first real run, 3 were entirely the wrong case (SPEC.md section 13,
2026-08-09). Matching here is an exact cluster_id join, so that failure mode
disappears.

Zero API requests -- never touches the shared daily budget (SPEC.md 8).

Deliberately does NOT summarize or interpret. Consistent with SPEC.md 6.5,
`court_reasoning_summary` stays human-written; this script's job is to put
the real text in front of the user, ranked by how likely it is to matter,
and stop there.

Requires the SQLite index from scripts/build_opinion_index.py, which is what
supplies each opinion's court (the opinions file alone has no idea).

USAGE
  python scripts/mine_opinion_corpus.py \
      --opinions "C:/temp/.../opinions-2026-06-30.csv.bz2" \
      --index    "C:/temp/.../opinion_index.sqlite" \
      --textdir  "C:/temp/.../opinion_corpus"
"""

import argparse
import bz2
import csv
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from extraction import NINTH_CIRCUIT_DISTRICTS

# Opinion bodies routinely exceed the default 128KB field cap.
csv.field_size_limit(2**31 - 1)

CSV_DIALECT_KWARGS = dict(delimiter=",", quotechar='"', escapechar="\\", doublequote=False)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
INDEX_OUT = DATA_DIR / "opinion_corpus_index.json"

PROGRESS_EVERY = 100_000

# Binding on a 9th Circuit filing (SPEC.md 15). ca9 is the circuit court
# itself; scotus binds everyone.
BINDING_COURTS = {"ca9", "scotus"}
# Where most consular-delay doctrine is actually made -- persuasive only,
# but heavily cited, so worth ranking above the rest of the country.
HIGH_VALUE_PERSUASIVE = {"dcd", "cadc"}


def boundary_pattern(keyword):
    """Word-boundary match, not plain substring. A real run of the sibling
    docket filter matched "visa" inside "Divisadero" (SPEC.md 13), and the
    same trap applies here. \\b only fires next to an alnum character, so
    it's added only where the keyword's edge is alnum ("221(g)" ends in
    ")")."""
    escaped = re.escape(keyword)
    prefix = r"\b" if keyword[0].isalnum() else ""
    suffix = r"\b" if keyword[-1].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


# An opinion must hit BOTH groups to be kept. Either alone is far too broad
# across a nationwide corpus: "mandamus" alone sweeps in prisoner petitions,
# "visa" alone sweeps in credit-card disputes.
DELAY_TERMS = [
    "mandamus", "unreasonable delay", "unreasonably delayed", "TRAC",
    "compel agency action", "1361", "555(b)", "706(1)",
]
VISA_TERMS = [
    "visa", "consular", "consulate", "221(g)", "administrative processing",
    "adjudicate", "immigrant visa",
]

DELAY_PATTERNS = [boundary_pattern(k) for k in DELAY_TERMS]
VISA_PATTERNS = [boundary_pattern(k) for k in VISA_TERMS]

# Strong doctrinal signals -- these push a case up the reading list because
# they indicate the opinion actually engages the arguments this project
# cares about, not just mentions a visa in passing.
SIGNAL_PATTERNS = {
    "trac_factors": boundary_pattern("TRAC"),
    "consular_nonreviewability": re.compile(r"consular\s+nonreviewability", re.IGNORECASE),
    "221g": boundary_pattern("221(g)"),
    "mandamus": boundary_pattern("mandamus"),
    "administrative_processing": boundary_pattern("administrative processing"),
}

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\n{3,}")


def strip_markup(text):
    """Older opinions have empty plain_text and only HTML/XML bodies. This
    is presentation cleanup for human reading, not interpretation."""
    return WS_RE.sub("\n\n", TAG_RE.sub("", text)).strip()


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "unknown").lower())
    return slug.strip("-")[:80] or "unknown"


def court_tier(court_id):
    """Higher is more useful to a 9th Circuit filing (SPEC.md 15)."""
    court = (court_id or "").lower()
    if court in BINDING_COURTS:
        return 5
    if court in NINTH_CIRCUIT_DISTRICTS:
        return 4
    if court in HIGH_VALUE_PERSUASIVE:
        return 3
    return 1


def score_opinion(meta, signals):
    """Transparent, rule-based ranking -- no AI judgment, same philosophy as
    extraction.py's relevance_score (SPEC.md 6.4)."""
    score = court_tier(meta["court_id"]) * 2

    if (meta.get("precedential_status") or "").lower() == "published":
        score += 2

    try:
        score += min(int(meta.get("citation_count") or 0), 5)
    except (TypeError, ValueError):
        pass

    date_filed = meta.get("date_filed") or ""
    if date_filed >= "2020-01-01":
        score += 3
    elif date_filed >= "2015-01-01":
        score += 1

    if signals.get("trac_factors"):
        score += 3
    if signals.get("consular_nonreviewability"):
        score += 3
    if signals.get("221g"):
        score += 2

    return score


def best_text(row, idx):
    """plain_text first; fall back to the HTML/XML bodies that older
    opinions use instead. Mirrors opinion_fetcher.fetch_opinion_text."""
    raw = (row[idx["plain_text"]] or "").strip()
    if raw:
        return raw, "plain_text"
    for field in ("html_with_citations", "html", "html_lawbox",
                  "html_columbia", "xml_harvard"):
        if field not in idx:
            continue
        candidate = (row[idx[field]] or "").strip()
        if candidate:
            return strip_markup(candidate), field
    return None, None


def lookup_cluster(conn, cluster_id):
    row = conn.execute("""
        SELECT c.case_name, c.date_filed, c.precedential_status, c.citation_count,
               d.court_id, d.docket_number
        FROM cluster c LEFT JOIN docket d ON c.docket_id = d.id
        WHERE c.id = ?
    """, (cluster_id,)).fetchone()
    if not row:
        return None
    return {
        "case_name": row[0], "date_filed": row[1], "precedential_status": row[2],
        "citation_count": row[3], "court_id": row[4], "docket_number": row[5],
    }


def run(opinions_path, index_path, textdir):
    textdir = Path(textdir)
    textdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)

    results = []
    scanned = 0
    matched = 0
    started = datetime.now(timezone.utc)

    with bz2.open(opinions_path, mode="rt", encoding="utf-8", newline="") as f:
        reader = csv.reader(f, **CSV_DIALECT_KWARGS)
        header = next(reader)
        idx = {name: i for i, name in enumerate(header)}
        for required in ("id", "cluster_id", "plain_text"):
            if required not in idx:
                print(f"FATAL: opinions file lacks a {required!r} column.", file=sys.stderr)
                sys.exit(1)

        for row in reader:
            scanned += 1
            if scanned % PROGRESS_EVERY == 0:
                elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 60
                print(f"  scanned {scanned:,} opinions, matched {matched:,} "
                      f"({elapsed:.0f} min elapsed)...", file=sys.stderr, flush=True)

            try:
                row[idx["plain_text"]]
            except IndexError:
                continue  # short/ragged row; skip rather than abort the pass

            # best_text() first, THEN keyword-match against it -- older
            # opinions routinely have an empty plain_text with the real
            # content only in an HTML field (SPEC.md 5.3, caught by
            # tests/test_mine_opinion_corpus.py before this ever touched
            # the real 54.5GB file). Matching against plain_text alone
            # silently dropped every opinion that only has the fallback.
            body, source_field = best_text(row, idx)
            if not body:
                continue

            if not any(p.search(body) for p in VISA_PATTERNS):
                continue
            if not any(p.search(body) for p in DELAY_PATTERNS):
                continue

            try:
                cluster_id = int(row[idx["cluster_id"]])
            except (TypeError, ValueError):
                continue

            meta = lookup_cluster(conn, cluster_id)
            if not meta:
                continue  # opinion whose cluster isn't in this snapshot

            signals = {name: bool(p.search(body)) for name, p in SIGNAL_PATTERNS.items()}
            score = score_opinion(meta, signals)

            filename = f"{slugify(meta['case_name'])}-{cluster_id}.txt"
            (textdir / filename).write_text(body, encoding="utf-8")

            results.append({
                "cluster_id": cluster_id,
                "opinion_id": row[idx["id"]],
                "case_name": meta["case_name"],
                "court_id": meta["court_id"],
                "court_tier": court_tier(meta["court_id"]),
                "date_filed": meta["date_filed"],
                "docket_number": meta["docket_number"],
                "precedential_status": meta["precedential_status"],
                "citation_count": meta["citation_count"],
                "corpus_score": score,
                "signals": signals,
                "text_file": filename,
                "text_chars": len(body),
                "text_source_field": source_field,
            })
            matched += 1

    conn.close()
    results.sort(key=lambda r: (-r["corpus_score"], r.get("date_filed") or ""))

    INDEX_OUT.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_file": Path(opinions_path).name,
        "text_dir": str(textdir),
        "opinions_scanned": scanned,
        "opinions_matched": matched,
        "results": results,
    }, indent=2), encoding="utf-8")

    print(f"\nScanned {scanned:,} opinions; matched {matched:,}.")
    print(f"Text written to {textdir}")
    print(f"Ranked index written to {INDEX_OUT}")
    for tier, label in ((5, "binding (ca9/scotus)"), (4, "9th Cir. districts"),
                        (3, "D.D.C. / D.C. Cir.")):
        n = sum(1 for r in results if r["court_tier"] == tier)
        print(f"  {label}: {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--opinions", required=True)
    parser.add_argument("--index", required=True)
    parser.add_argument("--textdir", required=True)
    args = parser.parse_args()
    run(args.opinions, args.index, args.textdir)
