#!/usr/bin/env python3
"""
Tests for scripts/drive_downloader.py -- the read side of SPEC.md section 16
(2026-08-11): Drive is now the durable long-term store, and the workflow no
longer commits data/ to git between runs, so each ephemeral run has to pull
its own starting state down before collector.py/opinion_fetcher.py run.

Verifies:
  1. Named state files (cases.json, checkpoint.json, etc.) come down from a
     mocked Drive folder into the right local paths
  2. checkpoint.json round-trips correctly -- it's resumption state rather
     than a user-facing output, but it's still in drive_uploader's
     FILES_TO_SYNC (imported here, not duplicated) and MUST come back down,
     or a run would silently restart from scratch every time
  3. Opinion .txt files are discovered by listing the Drive folder, not
     hardcoded, mirroring how the uploader side discovers them locally
  4. An empty/missing Drive folder is a no-op, not an error -- covers the
     very first run before anything has ever been uploaded
  5. A file that exists on Drive but isn't wanted (e.g. some unrelated file
     a human dropped in the folder) is left alone, not downloaded
"""

import io
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


def make_fake_drive_service(drive_contents):
    """Stand-in for the googleapiclient Drive service. `drive_contents` is
    {filename: bytes}, simulating what's currently sitting in the Drive
    folder."""
    service = mock.Mock()
    name_to_id = {name: f"id-{name}" for name in drive_contents}
    id_to_name = {v: k for k, v in name_to_id.items()}

    def files():
        files_api = mock.Mock()

        def list(q, fields, pageToken=None):
            return mock.Mock(execute=lambda: {
                "files": [{"id": i, "name": n} for n, i in name_to_id.items()],
                "nextPageToken": None,
            })

        def get_media(fileId):
            return mock.Mock(_fileId=fileId)

        files_api.list = mock.Mock(side_effect=list)
        files_api.get_media = mock.Mock(side_effect=get_media)
        return files_api

    service.files = mock.Mock(side_effect=files)

    def fake_download_file(_service, file_id, dest_path):
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(drive_contents[id_to_name[file_id]])

    return service, fake_download_file


def _set_fake_env():
    os.environ["GOOGLE_OAUTH_CLIENT_ID"] = "fake-client-id"
    os.environ["GOOGLE_OAUTH_CLIENT_SECRET"] = "fake-client-secret"
    os.environ["GOOGLE_OAUTH_REFRESH_TOKEN"] = "fake-refresh-token"
    os.environ["GDRIVE_FOLDER_ID"] = "fake-folder-id"


def test_named_state_files_and_checkpoint_are_downloaded():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_downloader as dd
        d = Path(tmpdir)
        dd.DATA_DIR = d
        dd.OPINIONS_DIR = d / "opinions"

        contents = {
            "cases.json": b"[]",
            "checkpoint.json": b'{"processed_docket_ids": [1, 2]}',
            "seed_citations.json": b'{"cases": []}',
        }
        service, fake_download = make_fake_drive_service(contents)
        _set_fake_env()

        with mock.patch("drive_downloader.get_drive_service", return_value=service), \
             mock.patch("drive_downloader.download_file", side_effect=fake_download):
            dd.run()

        check_true("cases.json landed in DATA_DIR", (d / "cases.json").exists())
        check_true("checkpoint.json landed in DATA_DIR", (d / "checkpoint.json").exists())
        check("checkpoint.json content round-tripped correctly",
              json.loads((d / "checkpoint.json").read_text()), {"processed_docket_ids": [1, 2]})
        check_true("seed_citations.json landed in DATA_DIR", (d / "seed_citations.json").exists())
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_opinion_files_discovered_and_downloaded_into_opinions_dir():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_downloader as dd
        d = Path(tmpdir)
        dd.DATA_DIR = d
        dd.OPINIONS_DIR = d / "opinions"

        contents = {
            "cases.json": b"[]",
            "taherian-v-blinken.txt": b"opinion text one",
            "trac-v-fcc.txt": b"opinion text two",
        }
        service, fake_download = make_fake_drive_service(contents)
        _set_fake_env()

        with mock.patch("drive_downloader.get_drive_service", return_value=service), \
             mock.patch("drive_downloader.download_file", side_effect=fake_download):
            dd.run()

        check_true("first opinion file downloaded into OPINIONS_DIR",
                   (dd.OPINIONS_DIR / "taherian-v-blinken.txt").exists())
        check_true("second opinion file downloaded into OPINIONS_DIR",
                   (dd.OPINIONS_DIR / "trac-v-fcc.txt").exists())
        check("opinion content round-tripped correctly",
              (dd.OPINIONS_DIR / "taherian-v-blinken.txt").read_bytes(), b"opinion text one")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_empty_drive_folder_is_a_noop_not_an_error():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_downloader as dd
        d = Path(tmpdir)
        dd.DATA_DIR = d
        dd.OPINIONS_DIR = d / "opinions"

        service, fake_download = make_fake_drive_service({})  # first run ever
        _set_fake_env()

        with mock.patch("drive_downloader.get_drive_service", return_value=service), \
             mock.patch("drive_downloader.download_file", side_effect=fake_download):
            dd.run()  # must not raise

        check("nothing downloaded from an empty Drive folder",
              list(d.glob("*.json")), [])
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_unrelated_file_on_drive_is_not_downloaded():
    tmpdir = tempfile.mkdtemp()
    try:
        import drive_downloader as dd
        d = Path(tmpdir)
        dd.DATA_DIR = d
        dd.OPINIONS_DIR = d / "opinions"

        # notes.md is neither a wanted named file nor a .txt opinion --
        # something a human could drop in the shared Drive folder by hand.
        contents = {"cases.json": b"[]", "notes.md": b"unrelated human note"}
        service, fake_download = make_fake_drive_service(contents)
        _set_fake_env()

        with mock.patch("drive_downloader.get_drive_service", return_value=service), \
             mock.patch("drive_downloader.download_file", side_effect=fake_download):
            dd.run()

        check_true("cases.json was downloaded", (d / "cases.json").exists())
        check("unrelated non-.txt file was left alone", (d / "notes.md").exists(), False)
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
