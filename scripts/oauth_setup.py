#!/usr/bin/env python3
"""
ONE-TIME LOCAL SETUP ONLY -- never run in CI, never commit its output.

Runs the OAuth consent flow to authorize this project's Drive uploads as
your own Google account (drive.file scope only -- see drive_uploader.py for
why: service accounts can't create new files in a regular Drive folder,
only update existing ones, confirmed by a live storageQuotaExceeded failure,
SPEC.md section 13).

Opens your browser for a one-time login + consent screen, then prints the
refresh token you need to store as GitHub secrets. The token is printed to
your terminal only -- this script never writes it to a file in the repo,
and never sends it anywhere but your own terminal.

ONE-TIME SETUP (in Google Cloud Console, same project as the existing
service account):
  1. APIs & Services > OAuth consent screen -> User type "External",
     publishing status "Testing", add yourself as a test user.
  2. APIs & Services > Credentials > Create Credentials > OAuth client ID
     -> Application type "Desktop app".
  3. Copy the Client ID and Client Secret it gives you.

USAGE
  pip install google-auth-oauthlib
  python scripts/oauth_setup.py --client-id "..." --client-secret "..."
"""

import argparse

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def run(client_id, client_secret):
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    creds = flow.run_local_server(port=0)

    print("\nSuccess. Store these as GitHub repo secrets "
          "(Settings > Secrets and variables > Actions > New repository secret):\n")
    print(f"GOOGLE_OAUTH_CLIENT_ID={client_id}")
    print(f"GOOGLE_OAUTH_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_OAUTH_REFRESH_TOKEN={creds.refresh_token}")
    print("\nThis refresh token is a long-lived credential -- treat it like "
          "a password. Do not commit it, paste it in chat, or share it.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    args = parser.parse_args()
    run(args.client_id, args.client_secret)
