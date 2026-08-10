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

import pandas as pd

sys.path.insert(0, os.path.dirname(__file__))
from extraction import NINTH_CIRCUIT_DISTRICTS

# Opinion bodies routinely exceed the default 128KB field cap.
csv.field_size_limit(2**31 - 1)

CSV_DIALECT_KWARGS = dict(delimiter=",", quotechar='"', escapechar="\\", doublequote=False)

# pandas' C parser rather than the stdlib csv module. Measured on the real
# file: 17 rows/sec with csv (which cannot use its fast path once escapechar
# is set, so it walks ~250GB character-by-character in Python) versus 304
# rows/sec here -- 5.5 days down to ~9 hours. Decompression is single-
# threaded and irreducible, so this is close to the practical floor.
CHUNK_ROWS = 2000

TEXT_COLUMNS = ["plain_text", "html_with_citations", "html", "html_lawbox",
                "html_columbia", "xml_harvard"]
REQUIRED_COLUMNS = ["id", "cluster_id", "plain_text"]

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


def boundary_source(keyword):
    """Word-boundary regex source for one keyword -- not a plain substring
    match. A real run of the sibling docket filter matched "visa" inside
    "Divisadero" (SPEC.md 13), and the same trap applies here. \\b only
    fires next to an alnum character, so it's added only where the
    keyword's edge is alnum ("221(g)" ends in ")")."""
    escaped = re.escape(keyword)
    prefix = r"\b" if keyword[0].isalnum() else ""
    suffix = r"\b" if keyword[-1].isalnum() else ""
    return prefix + escaped + suffix


def boundary_pattern(keyword):
    return re.compile(boundary_source(keyword), re.IGNORECASE)


def any_of(terms, extra_sources=()):
    """One compiled alternation instead of N separate patterns, matched
    case-SENSITIVELY against text the caller has already lowercased.

    Two deliberate choices, both measured on the real file where this gate
    runs against every one of ~10M opinions:
      * one alternation, not N patterns -- N separate .search() calls meant
        re-scanning each opinion N times just to reject it
      * no re.IGNORECASE -- case-insensitive matching over a large
        alternation is markedly slower than lowercasing once (a single
        C-level pass) and matching case-sensitively. Semantics are identical
        because every term here is already lowercase and callers pass
        body.lower().
    """
    sources = [boundary_source(t.lower()) for t in terms] + list(extra_sources)
    return re.compile("|".join(sources))


# An opinion must hit BOTH groups to be kept. Either alone is far too broad
# across a nationwide corpus: "mandamus" alone sweeps in prisoner petitions,
# "visa" alone sweeps in credit-card disputes.
DELAY_TERMS = [
    "mandamus", "unreasonable delay", "unreasonably delayed", "TRAC",
    "compel agency action", "555(b)", "706(1)",
]
# The federal mandamus statute, but only when it actually reads as a
# citation. A bare \b1361\b matches page numbers and dollar amounts, which
# would wave through any immigration opinion that happens to contain that
# number. Lowercase, to match the lowercased text the gates run against.
MANDAMUS_STATUTE_SOURCE = r"(?:§|section|u\.?\s?s\.?\s?c\.?)\s*§?\s*1361"

VISA_TERMS = [
    "visa", "consular", "consulate", "221(g)", "administrative processing",
    "adjudicate", "immigrant visa",
]

DELAY_GATE = any_of(DELAY_TERMS, [MANDAMUS_STATUTE_SOURCE])
VISA_GATE = any_of(VISA_TERMS)

# Strong doctrinal signals -- these push a case up the reading list because
# they indicate the opinion actually engages the arguments this project
# cares about, not just mentions a visa in passing.
#
# TRAC is deliberately case-SENSITIVE and citation-anchored. A plain
# case-insensitive \btrac\b put a 1983 arbitration case and an 1863 mining
# case in the top 10 of the first real run, matching the company name "Trac
# Enterprises" and the OCR artifact "a trac t all miners" (i.e. "tract" split
# by a bad scan). Real references are the uppercase acronym in a citation
# context, or the case's full name.
SIGNAL_PATTERNS = {
    "trac_factors": re.compile(
        r"\bTRAC\b\s*(?:factors?|v\.)"          # "TRAC factors", "TRAC v. FCC"
        r"|\bunder\s+TRAC\b"                     # "under TRAC"
        r"|\bTRAC\b\s*,\s*750"                   # "TRAC, 750 F.2d 70"
        r"|Telecommunications\s+Research"        # the case's full name
        r"|750\s+F\.?\s?2d\s+70"                 # the reporter citation
    ),
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


# Signals that mean the opinion actually engages consular-delay doctrine,
# as opposed to merely containing the word "mandamus" somewhere. Bare
# "mandamus" is not here on purpose: it appears in ~75% of matches and
# carries almost no information by itself.
TOPICAL_SIGNALS = ("trac_factors", "consular_nonreviewability", "221g",
                   "administrative_processing")


def is_topical(signals):
    return any(signals.get(s) for s in TOPICAL_SIGNALS)


def score_opinion(meta, signals):
    """Transparent, rule-based ranking -- no AI judgment, same philosophy as
    extraction.py's relevance_score (SPEC.md 6.4).

    Topical signals deliberately outweigh venue. The first ranking put
    Juliana (climate change), Perry (marriage) and Optional Capital
    (corporate) in the top 20 purely for being recent, well-cited 9th
    Circuit cases that say "mandamus" once, while genuine consular-delay
    decisions from D.D.C. ranked below them. For a reading list, an
    on-point persuasive case beats a binding case about something else.
    """
    score = 0

    if signals.get("trac_factors"):
        score += 6
    if signals.get("consular_nonreviewability"):
        score += 6
    if signals.get("221g"):
        score += 5
    if signals.get("administrative_processing"):
        score += 3

    score += court_tier(meta["court_id"]) * 2

    if (meta.get("precedential_status") or "").lower() == "published":
        score += 2

    try:
        score += min(int(meta.get("citation_count") or 0), 3)
    except (TypeError, ValueError):
        pass

    date_filed = meta.get("date_filed") or ""
    if date_filed >= "2020-01-01":
        score += 3
    elif date_filed >= "2015-01-01":
        score += 1

    return score


def best_text(fields):
    """plain_text first; fall back to the HTML/XML bodies that older
    opinions use instead. Mirrors opinion_fetcher.fetch_opinion_text."""
    raw = (fields.get("plain_text") or "").strip()
    if raw:
        return raw, "plain_text"
    for field in TEXT_COLUMNS[1:]:
        candidate = (fields.get(field) or "").strip()
        if candidate:
            return strip_markup(candidate), field
    return None, None


def relevant_court_ids(scope):
    """Which courts are worth spending regex time on.

    Measured on the real index: only 10.2% of the 10.07M clusters sit in
    courts that matter to a 9th Circuit filing. The rest is dominated by
    state appellate courts (nyappdiv 773k, fladistctapp 360k, calctapp,
    illappct...), which cannot supply authority for a federal mandamus
    action against the State Department. Rejecting those on a set lookup,
    before any text assembly or regex, is what makes this pass finish
    overnight instead of in a day.
    """
    if scope == "all":
        return None  # no filtering
    priority = NINTH_CIRCUIT_DISTRICTS | {"ca9", "scotus", "dcd", "cadc"}
    if scope == "priority":
        return priority
    if scope == "federal":
        # Every federal circuit plus the specialised ones, on top of the
        # priority set. Broader persuasive net, roughly 3x the work.
        return priority | {f"ca{i}" for i in range(1, 12)} | {"cafc", "cit", "uscfc"}
    raise ValueError(f"unknown scope: {scope}")


def load_relevant_clusters(conn, scope):
    """Preloads matching cluster ids into a set. ~1M ints is cheap in RAM and
    turns the per-row court check into an O(1) lookup with no SQLite call."""
    courts = relevant_court_ids(scope)
    if courts is None:
        return None
    placeholders = ",".join("?" * len(courts))
    rows = conn.execute(
        f"SELECT c.id FROM cluster c JOIN docket d ON c.docket_id = d.id "
        f"WHERE d.court_id IN ({placeholders})", sorted(courts))
    return {r[0] for r in rows}


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


def rescore(textdir):
    """Recomputes signals and scores from the text already on disk, then
    rewrites the ranked index -- no 54.5GB rescan.

    Signal and scoring rules are tuning decisions that will keep changing as
    the reading list gets used; re-reading a few thousand local text files
    takes seconds, so there is no reason to spend hours re-streaming the bulk
    file to apply them. Also collapses the one-entry-per-opinion index into
    one entry per case, since a majority and its dissents are one thing to
    read, not three.
    """
    textdir = Path(textdir)
    existing = json.loads(INDEX_OUT.read_text(encoding="utf-8"))
    by_cluster = {}

    for record in existing["results"]:
        path = textdir / record["text_file"]
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        record = dict(record)
        record["signals"] = {name: bool(p.search(body)) for name, p in SIGNAL_PATTERNS.items()}
        record["topical"] = is_topical(record["signals"])
        record["text_chars"] = len(body)
        record["corpus_score"] = score_opinion(record, record["signals"])

        # Dedupe on case name + filing date, not cluster id: CourtListener
        # carries the same decision under several cluster ids when it has
        # ingested it from more than one source (Rivas v. Napolitano appears
        # under both 798644 and 8441361). Keep the longest text, which is the
        # substantive opinion rather than a short concurrence.
        key = ((record.get("case_name") or "").strip().lower(),
               record.get("date_filed"), record.get("court_id"))
        prior = by_cluster.get(key)
        if prior is None or record["text_chars"] > prior["text_chars"]:
            by_cluster[key] = record

    n_rows = len(existing["results"])
    results = sorted(by_cluster.values(),
                     key=lambda r: (-r["corpus_score"], r.get("date_filed") or ""))
    existing["results"] = results
    existing["rescored_at"] = datetime.now(timezone.utc).isoformat()
    existing["opinions_matched"] = len(results)
    existing["topical_count"] = sum(1 for r in results if r["topical"])
    INDEX_OUT.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    print(f"Rescored {len(results):,} unique cases (from {n_rows:,} opinion rows).")
    print(f"  on-point (engages TRAC / consular nonreviewability / 221(g) / "
          f"administrative processing): {existing['topical_count']:,}")
    for tier, label in ((5, "binding (ca9/scotus)"), (4, "9th Cir. districts"),
                        (3, "D.D.C. / D.C. Cir.")):
        n = sum(1 for r in results if r["court_tier"] == tier)
        print(f"  {label}: {n}")


def run(opinions_path, index_path, textdir, scope="priority"):
    textdir = Path(textdir)
    textdir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(index_path)

    relevant_clusters = load_relevant_clusters(conn, scope)
    if relevant_clusters is not None:
        print(f"Scope {scope!r}: {len(relevant_clusters):,} clusters in relevant courts "
              f"(everything else is rejected before any regex).", file=sys.stderr, flush=True)

    # Matches are appended here the moment they are found. The full pass takes
    # hours on a laptop that may well be shut down partway through, so results
    # must never live only in memory -- this file survives a hard stop.
    jsonl_path = INDEX_OUT.with_suffix(".jsonl")
    jsonl = jsonl_path.open("a", encoding="utf-8")

    results = []
    scanned = 0
    matched = 0
    started = datetime.now(timezone.utc)

    # Which of our columns this snapshot actually has. Passing usecols for a
    # column the file lacks is a hard error in pandas, and the fixture files
    # in tests/ deliberately carry only a subset.
    with bz2.open(opinions_path, mode="rt", encoding="utf-8", newline="") as probe:
        header = next(csv.reader(probe, **CSV_DIALECT_KWARGS))
    for required in REQUIRED_COLUMNS:
        if required not in header:
            print(f"FATAL: opinions file lacks a {required!r} column.", file=sys.stderr)
            sys.exit(1)
    usecols = [c for c in (["id", "cluster_id"] + TEXT_COLUMNS) if c in header]

    with bz2.open(opinions_path, mode="rt", encoding="utf-8", newline="") as f:
        reader = pd.read_csv(
            f,
            chunksize=CHUNK_ROWS,
            escapechar="\\",
            doublequote=False,
            usecols=usecols,
            dtype=str,
            engine="c",
            # na_filter=False keeps empty fields as "" rather than NaN floats,
            # which both avoids per-value type checks downstream and is faster.
            na_filter=False,
            on_bad_lines="skip",
        )

        for chunk in reader:
            columns = {c: chunk[c].tolist() for c in chunk.columns}
            n = len(chunk)

            for i in range(n):
                scanned += 1
                if scanned % PROGRESS_EVERY == 0:
                    elapsed = (datetime.now(timezone.utc) - started).total_seconds() / 60
                    print(f"  scanned {scanned:,} opinions, matched {matched:,} "
                          f"({elapsed:.0f} min elapsed)...", file=sys.stderr, flush=True)

                try:
                    cluster_id = int(columns["cluster_id"][i])
                except (TypeError, ValueError):
                    continue

                # Court check FIRST -- a set lookup, far cheaper than
                # assembling text and running regex, and it rejects ~90% of
                # the corpus (see relevant_court_ids).
                if relevant_clusters is not None and cluster_id not in relevant_clusters:
                    continue

                fields = {c: columns[c][i] for c in columns}

                # best_text() before keyword-matching -- older opinions
                # routinely have an empty plain_text with the real content
                # only in an HTML field (SPEC.md 5.3, caught by
                # tests/test_mine_opinion_corpus.py before this ever touched
                # the real 54.5GB file). Matching against plain_text alone
                # silently dropped every opinion that only has the fallback.
                body, source_field = best_text(fields)
                if not body:
                    continue

                # Lowercase once, then match case-sensitively -- see any_of().
                low = body.lower()

                # Delay gate first: it rejects far more of the corpus than
                # the visa gate does, so surviving opinions usually cost
                # exactly one regex pass before being discarded.
                if not DELAY_GATE.search(low):
                    continue
                if not VISA_GATE.search(low):
                    continue

                meta = lookup_cluster(conn, cluster_id)
                if not meta:
                    continue  # opinion whose cluster isn't in this snapshot

                signals = {name: bool(p.search(body)) for name, p in SIGNAL_PATTERNS.items()}
                score = score_opinion(meta, signals)

                # opinion_id, not just cluster_id: a cluster routinely holds a
                # majority plus dissents/concurrences as separate opinion rows.
                # Naming by cluster alone made them overwrite each other -- the
                # first real run silently lost the dissent text for 128 clusters
                # and left duplicate index entries pointing at one file.
                filename = f"{slugify(meta['case_name'])}-{cluster_id}-{fields['id']}.txt"
                (textdir / filename).write_text(body, encoding="utf-8")

                record = {
                    "cluster_id": cluster_id,
                    "opinion_id": fields["id"],
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
                }
                results.append(record)
                jsonl.write(json.dumps(record) + "\n")
                jsonl.flush()
                matched += 1

    jsonl.close()
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
    parser.add_argument("--opinions")
    parser.add_argument("--index")
    parser.add_argument("--textdir", required=True)
    parser.add_argument(
        "--rescore-only", action="store_true",
        help="skip the bulk scan; recompute signals/scores/ranking from the "
             "text already in --textdir and rewrite the index")
    parser.add_argument(
        "--scope", default="priority", choices=("priority", "federal", "all"),
        help="priority = 9th Cir. districts + ca9 + scotus + dcd + cadc (10%% of "
             "the corpus, what actually binds or persuades a 9th Circuit "
             "filing); federal = adds every federal circuit; all = no court "
             "filtering, roughly 10x the work for mostly state-court noise")
    args = parser.parse_args()
    if args.rescore_only:
        rescore(args.textdir)
    else:
        if not (args.opinions and args.index):
            parser.error("--opinions and --index are required unless --rescore-only")
        run(args.opinions, args.index, args.textdir, scope=args.scope)
