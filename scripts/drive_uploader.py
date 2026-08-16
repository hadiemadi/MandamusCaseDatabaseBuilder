#!/usr/bin/env python3
"""
Uploads the collector's output files to a Google Drive folder.
SPEC.md section 16 (supersedes section 9/12 on this point, 2026-08-11):
Drive is the durable long-term store. GitHub hosts the code and runs the
automation, but is not meant to hold the data past the 30-day collection
window -- see SPEC.md section 16 for the removal plan.

Uses OAuth as the user's own Google account, not a service account. Service
accounts have a hard zero-byte storage quota and cannot CREATE new files in
a regular (non-Shared-Drive) folder -- confirmed by a live storageQuotaExceeded
failure (SPEC.md section 13, 2026-08-09). Scoped to drive.file (not the
broader drive scope) so this can only ever touch files it created itself,
not the rest of the user's Drive -- see scripts/oauth_setup.py for the
one-time local consent flow that mints the refresh token.

USAGE
  pip install -r requirements.txt
  export GOOGLE_OAUTH_CLIENT_ID="..."
  export GOOGLE_OAUTH_CLIENT_SECRET="..."
  export GOOGLE_OAUTH_REFRESH_TOKEN="..."
  export GDRIVE_FOLDER_ID="..."
  python scripts/drive_uploader.py
"""

import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

TOKEN_URI = "https://oauth2.googleapis.com/token"
# drive.file, not the broader drive scope: this identity can only ever see
# files it created itself, never the rest of the user's personal Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.file"]
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
OPINIONS_DIR = DATA_DIR / "opinions"
FILES_TO_SYNC = [
    ("cases.json", "application/json"),
    ("issues.json", "application/json"),
    ("run_log.json", "application/json"),
    ("dashboard.html", "text/html"),
    # seed_citations.json and every fetched opinion text file are the whole
    # point of the free-opinion pipeline (SPEC.md section 14) -- without
    # syncing them here they'd sit in the git repo but never reach Drive,
    # the user's actual interface (SPEC.md section 9).
    ("seed_citations.json", "application/json"),
    # Added 2026-08-11 (SPEC.md section 16): Drive is being promoted from
    # convenience copy to the actual long-term store, since GitHub is not
    # meant to hold this data past the run window. These two were the only
    # data/ outputs missing from the sync -- without them the bulk-discovery
    # candidate list and the mined opinion corpus index would never leave
    # the repo at all.
    ("bulk_discovered_dockets.json", "application/json"),
    ("opinion_corpus_index.json", "application/json"),
    # checkpoint.json is resumption state, not really a user-facing output,
    # but it MUST round-trip through Drive too now that git no longer holds
    # it between runs (drive_downloader.py fetches it back down at the start
    # of the next run) -- without this the request budget / dedup state
    # would silently reset every single run.
    ("checkpoint.json", "application/json"),
]


def discover_opinion_files():
    """Opinion filenames aren't known in advance -- new ones appear as
    opinion_fetcher.py resolves more seed cases -- so this list is built
    fresh each run instead of hardcoded like FILES_TO_SYNC."""
    if not OPINIONS_DIR.exists():
        return []
    return sorted(OPINIONS_DIR.glob("*.txt"))


def get_drive_service():
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        print("FATAL: GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET / "
              "GOOGLE_OAUTH_REFRESH_TOKEN not all set.", file=sys.stderr)
        sys.exit(1)
    creds = Credentials(
        token=None,  # no access token yet -- refreshed below on first use
        refresh_token=refresh_token,
        token_uri=TOKEN_URI,
        client_id=client_id,
        client_secret=client_secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build("drive", "v3", credentials=creds)


def find_existing_file(service, folder_id, filename):
    query = f"'{folder_id}' in parents and name = '{filename}' and trashed = false"
    results = service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get("files", [])
    return files[0]["id"] if files else None


def upload_or_update(service, folder_id, local_path, mime_type):
    filename = local_path.name
    media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=False)
    existing_id = find_existing_file(service, folder_id, filename)
    if existing_id:
        service.files().update(fileId=existing_id, media_body=media).execute()
        print(f"Updated {filename} in Drive.")
    else:
        service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media, fields="id",
        ).execute()
        print(f"Created {filename} in Drive.")


def run():
    folder_id = os.environ.get("GDRIVE_FOLDER_ID")
    if not folder_id:
        print("FATAL: GDRIVE_FOLDER_ID not set.", file=sys.stderr)
        sys.exit(1)

    service = get_drive_service()
    for filename, mime_type in FILES_TO_SYNC:
        local_path = DATA_DIR / filename
        if not local_path.exists():
            print(f"Skipping {filename} (not found yet).")
            continue
        upload_or_update(service, folder_id, local_path, mime_type)

    opinion_files = discover_opinion_files()
    print(f"Syncing {len(opinion_files)} opinion text file(s).")
    for path in opinion_files:
        upload_or_update(service, folder_id, path, "text/plain")


if __name__ == "__main__":
    run()
