#!/usr/bin/env python3
"""
Tests for scripts/drive_uploader.py -- previously had zero coverage.

Verifies:
  1. seed_citations.json and every file under data/opinions/*.txt get
     synced -- this is the whole point of the free-opinion pipeline
     (SPEC.md section 14); without it, fetched opinions would sit in the
     git repo but never reach Drive, the user's actual interface
  2. A missing file (opinions/ doesn't exist yet, or a FILES_TO_SYNC entry
     hasn't been created yet) is skipped, not an error
  3. New opinion filenames are discovered fresh each run -- they aren't
     hardcoded like FILES_TO_SYNC, since new ones appear over time
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

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


def make_fake_drive_service(uploaded):
    """A minimal stand-in for the googleapiclient Drive service. Records
    every filename it's asked to create/update into `uploaded` (a list)."""
    service = mock.Mock()

    def files():
        files_api = mock.Mock()

        def list(q, fields):
            return mock.Mock(execute=lambda: {"files": []})  # nothing exists yet

        def create(body, media_body, fields):
            uploaded.append(body["name"])
            return mock.Mock(execute=lambda: {"id": "fake-id"})

        def update(fileId, media_body):
            return mock.Mock(execute=lambda: {"id": fileId})

        files_api.list = mock.Mock(side_effect=list)
        files_api.create = mock.Mock(side_effect=create)
        files_api.update = mock.Mock(side_effect=update)
        return files_api

    service.files = mock.Mock(side_effect=files)
    return service


def test_seed_citations_and_opinions_are_synced():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_uploader as du
        d = Path(tmpdir)
        du.DATA_DIR = d
        du.OPINIONS_DIR = d / "opinions"
        du.OPINIONS_DIR.mkdir()

        (d / "cases.json").write_text("[]", encoding="utf-8")
        (d / "issues.json").write_text("[]", encoding="utf-8")
        (d / "run_log.json").write_text("[]", encoding="utf-8")
        (d / "dashboard.html").write_text("<html></html>", encoding="utf-8")
        (d / "seed_citations.json").write_text(json.dumps({"cases": []}), encoding="utf-8")
        (du.OPINIONS_DIR / "taherian-v-blinken.txt").write_text("opinion text", encoding="utf-8")
        (du.OPINIONS_DIR / "trac-v-fcc.txt").write_text("opinion text", encoding="utf-8")

        uploaded = []
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "fake-client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "fake-client-secret"
        os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "fake-refresh-token"
        os.environ["GDRIVE_FOLDER_ID"] = "fake-folder-id"

        with mock.patch("drive_uploader.get_drive_service",
                         return_value=make_fake_drive_service(uploaded)):
            du.run()

        check_true("seed_citations.json was uploaded", "seed_citations.json" in uploaded)
        check_true("first opinion file was uploaded", "taherian-v-blinken.txt" in uploaded)
        check_true("second opinion file was uploaded", "trac-v-fcc.txt" in uploaded)
        check("exactly the expected file count uploaded", len(uploaded), 7)  # 5 FILES_TO_SYNC + 2 opinions
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_missing_files_are_skipped_not_errors():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_uploader as du
        d = Path(tmpdir)
        du.DATA_DIR = d
        du.OPINIONS_DIR = d / "opinions"
        # Nothing created at all -- simulates the very first run, or a run
        # where the collector hasn't produced any data yet.

        uploaded = []
        os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "fake-client-id"
        os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "fake-client-secret"
        os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "fake-refresh-token"
        os.environ["GDRIVE_FOLDER_ID"] = "fake-folder-id"

        with mock.patch("drive_uploader.get_drive_service",
                         return_value=make_fake_drive_service(uploaded)):
            du.run()  # must not raise

        check("nothing uploaded when no files exist", uploaded, [])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_discover_opinion_files_picks_up_new_files():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_uploader as du
        d = Path(tmpdir)
        du.OPINIONS_DIR = d / "opinions"
        du.OPINIONS_DIR.mkdir()

        check("no opinion files yet", len(du.discover_opinion_files()), 0)

        (du.OPINIONS_DIR / "new-case.txt").write_text("text", encoding="utf-8")
        check("newly created file is discovered without code changes",
              len(du.discover_opinion_files()), 1)
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
