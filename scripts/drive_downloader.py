#!/usr/bin/env python3
"""
Downloads the collector's state files FROM Google Drive into data/, before
a run starts. SPEC.md section 16 (2026-08-11): Drive is now the durable
long-term store, and GitHub no longer holds case data between runs -- so
each ephemeral run has to fetch its own starting state instead of finding
it already checked out from git.

Companion to drive_uploader.py, which pushes state back up at the end of a
run. Reuses that module's auth and file-lookup helpers rather than
duplicating them.

Downloads exactly what drive_uploader.py uploads (same FILES_TO_SYNC list,
imported rather than duplicated, so the two can never drift out of sync)
plus opinion text files, discovered the same way the uploader discovers them
locally: by listing what's actually there rather than a hardcoded name list.

Missing files are not an error -- the very first run, or a fresh Drive
folder, legitimately has nothing to download yet, and every downstream
script already treats a missing file as "start from empty" (see
collector.py's load_checkpoint/load_json calls).

USAGE
  pip install -r requirements.txt
  export GOOGLE_OAUTH_CLIENT_ID="..."
  export GOOGLE_OAUTH_CLIENT_SECRET="..."
  export GOOGLE_OAUTH_REFRESH_TOKEN="..."
  export GDRIVE_FOLDER_ID="..."
  python scripts/drive_downloader.py
"""

import io
import os
import sys
from pathlib import Path

from googleapiclient.http import MediaIoBaseDownload

from drive_uploader import DATA_DIR, OPINIONS_DIR, FILES_TO_SYNC, get_drive_service


def list_drive_files(service, folder_id):
    """One listing call instead of one find_existing_file() lookup per file."""
    files, page_token = {}, None
    while True:
        results = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name)",
            pageToken=page_token,
        ).execute()
        for f in results.get("files", []):
            files[f["name"]] = f["id"]
        page_token = results.get("nextPageToken")
        if not page_token:
            return files


def download_file(service, file_id, dest_path):
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    dest_path.write_bytes(buf.getvalue())


def run():
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        print("FATAL: GDRIVE_FOLDER_ID not set.", file=sys.stderr)
        sys.exit(1)

    service = get_drive_service()
    drive_files = list_drive_files(service, folder_id)
    print(f"Found {len(drive_files)} file(s) in the Drive folder.")

    for filename, _mime_type in FILES_TO_SYNC:
        file_id = drive_files.get(filename)
        if not file_id:
            print(f"Skipping {filename} (not on Drive yet).")
            continue
        download_file(service, file_id, DATA_DIR / filename)
        print(f"Downloaded {filename} from Drive.")

    # Opinion text files: same "discover by listing" problem as the uploader,
    # solved the same way -- anything in the Drive folder that looks like an
    # opinion (a .txt file that isn't one of the named JSON/HTML outputs)
    # gets pulled into data/opinions/.
    named = {f for f, _ in FILES_TO_SYNC}
    opinion_names = [n for n in drive_files if n.endswith(".txt") and n not in named]
    print(f"Downloading {len(opinion_names)} opinion text file(s).")
    for name in opinion_names:
        download_file(service, drive_files[name], OPINIONS_DIR / name)


if __name__ == "__main__":
    run()
