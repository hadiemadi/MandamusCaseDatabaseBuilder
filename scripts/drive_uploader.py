#!/usr/bin/env python3
"""
Uploads the collector's output files to a Google Drive folder.
SPEC.md section 9 and 12 — the Drive copy is a convenience copy, NOT the
source of truth (that's the GitHub repo). Uses a restricted service account
that can only touch the one shared folder.

USAGE
  pip install -r requirements.txt
  export GOOGLE_SERVICE_ACCOUNT_JSON='{...}'
  export GDRIVE_FOLDER_ID="..."
  python scripts/drive_uploader.py
"""

import json
import os
import sys
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
FILES_TO_SYNC = [
    ("cases.json", "application/json"),
    ("issues.json", "application/json"),
    ("run_log.json", "application/json"),
    ("dashboard.html", "text/html"),
]


def get_drive_service():
    key_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not key_json:
        print("FATAL: GOOGLE_SERVICE_ACCOUNT_JSON not set.", file=sys.stderr)
        sys.exit(1)
    creds = service_account.Credentials.from_service_account_info(
        json.loads(key_json), scopes=SCOPES
    )
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


if __name__ == "__main__":
    run()
