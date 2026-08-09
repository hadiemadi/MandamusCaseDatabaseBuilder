#!/usr/bin/env python3
"""
Builds a disk-backed SQLite index joining CourtListener's bulk opinion
clusters to their dockets (and therefore to a court). SPEC.md section 5.3.

Why this exists: the opinions bulk file (54.5GB) holds the full text of
every opinion, but a row there only knows its `cluster_id` -- it has no
idea which court the opinion came from. That link lives in two other bulk
files:

    search_opinion.cluster_id -> search_opinioncluster.id
    search_opinioncluster.docket_id -> search_docket.id -> court_id

Confirmed against the live schema (schema-2026-06-30.sql, 2026-08-09).
Without this index, a 9th Circuit opinion is indistinguishable from a
traffic case in Ohio, so venue ranking (SPEC.md section 15) is impossible.

SQLite rather than in-memory dicts deliberately: the dockets table is
~71.7M rows, which would be several GB of Python objects. SQLite keeps it
on disk and still answers point lookups in microseconds.

Runs entirely offline against already-downloaded files -- zero API
requests, so it never touches the daily budget (SPEC.md section 8).

USAGE
  python scripts/build_opinion_index.py \
      --clusters "C:/temp/.../opinion-clusters-2026-06-30.csv.bz2" \
      --dockets  "C:/temp/.../dockets-2026-06-30.csv.bz2" \
      --out      "C:/temp/.../opinion_index.sqlite"
"""

import argparse
import bz2
import csv
import sqlite3
import sys
from pathlib import Path

# Opinion text and cluster fields (syllabus, headmatter) blow past the
# default 128KB field cap. Without this, parsing dies partway through.
csv.field_size_limit(2**31 - 1)

# Same PostgreSQL COPY dialect the other bulk files use: every field quoted
# (FORCE_QUOTE *), embedded quotes backslash-escaped rather than doubled
# (ESCAPE '\'). Matches scripts/bulk_docket_filter.py.
CSV_DIALECT_KWARGS = dict(delimiter=",", quotechar='"', escapechar="\\", doublequote=False)

BATCH_SIZE = 50_000
PROGRESS_EVERY = 500_000


def connect(out_path):
    conn = sqlite3.connect(out_path)
    # Bulk-load tuning: this file is a rebuildable derived artifact, so
    # durability guarantees buy nothing and cost a lot of time.
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA cache_size = -200000")  # ~200MB page cache
    return conn


def create_schema(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS cluster;
        DROP TABLE IF EXISTS docket;
        CREATE TABLE cluster (
            id INTEGER PRIMARY KEY,
            docket_id INTEGER,
            case_name TEXT,
            date_filed TEXT,
            precedential_status TEXT,
            citation_count INTEGER
        );
        CREATE TABLE docket (
            id INTEGER PRIMARY KEY,
            court_id TEXT,
            docket_number TEXT,
            date_terminated TEXT
        );
    """)


def stream_rows(path, columns):
    """Yields tuples of the requested columns from a bz2-compressed bulk CSV.
    Never materializes the decompressed file."""
    with bz2.open(path, mode="rt", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f, **CSV_DIALECT_KWARGS)
        for row in reader:
            yield tuple(row.get(c) or None for c in columns)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def load_table(conn, path, table, columns, label):
    """Streams one bulk CSV into a SQLite table in batches."""
    placeholders = ",".join("?" * len(columns))
    sql = f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) VALUES ({placeholders})"

    batch = []
    count = 0
    for row in stream_rows(path, columns):
        row = list(row)
        row[0] = to_int(row[0])  # id is always first and must be an int PK
        if row[0] is None:
            continue
        batch.append(row)
        count += 1
        if len(batch) >= BATCH_SIZE:
            conn.executemany(sql, batch)
            batch.clear()
        if count % PROGRESS_EVERY == 0:
            print(f"  {label}: {count:,} rows...", file=sys.stderr, flush=True)
    if batch:
        conn.executemany(sql, batch)
    conn.commit()
    print(f"{label}: {count:,} rows loaded.", file=sys.stderr, flush=True)
    return count


def build_indexes(conn):
    # Created only after bulk insert -- maintaining an index during a
    # multi-million-row load is dramatically slower than building it once.
    print("Building secondary indexes...", file=sys.stderr, flush=True)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cluster_docket ON cluster(docket_id)")
    conn.commit()


def run(clusters_path, dockets_path, out_path):
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()  # rebuildable artifact; start clean

    conn = connect(str(out_path))
    try:
        create_schema(conn)
        load_table(conn, clusters_path, "cluster",
                   ["id", "docket_id", "case_name", "date_filed",
                    "precedential_status", "citation_count"], "clusters")
        load_table(conn, dockets_path, "docket",
                   ["id", "court_id", "docket_number", "date_terminated"], "dockets")
        build_indexes(conn)

        n_cluster = conn.execute("SELECT COUNT(*) FROM cluster").fetchone()[0]
        n_docket = conn.execute("SELECT COUNT(*) FROM docket").fetchone()[0]
        n_joined = conn.execute(
            "SELECT COUNT(*) FROM cluster c JOIN docket d ON c.docket_id = d.id"
        ).fetchone()[0]
        print(f"\nIndex built at {out_path}")
        print(f"  clusters: {n_cluster:,}")
        print(f"  dockets:  {n_docket:,}")
        print(f"  clusters successfully joined to a court: {n_joined:,}")
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--clusters", required=True)
    parser.add_argument("--dockets", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    run(args.clusters, args.dockets, args.out)
