# Setup checklist — no code writing required

Everything below is clicking in a browser. You never need to open or edit
any code.

## 1. Clean out the repository, then upload the new content
You already have a repo: `hadiemadi/MandamusCaseDatabaseBulider`. It currently has
old content in it (`mandamus_cloud_bundle/` and possibly other leftover
files) that should NOT stay — see SPEC.md section 10 for why.

**On github.com, in the repo:**
1. Click into the `mandamus_cloud_bundle` folder, then click each file
   inside it → the trash-can/Delete icon → commit the deletion. Repeat
   until the folder is gone. (GitHub doesn't have a single "delete folder"
   button in the web UI — deleting every file inside it removes the folder
   automatically, since folders don't exist on their own in git.)
2. Check the repo's root for anything else left over (an old README,
   old requirements.txt, etc.) and delete those the same way.
3. Once the repo is empty (or only has what you intend to keep), use
   "Add file" → "Upload files" and drag in everything from this package's
   extracted folder.
4. Commit with a message like "Clean rebuild per SPEC.md".

## 2. Get a fresh CourtListener token
Don't reuse the old one (it's been visible in chat history before).
1. courtlistener.com → log in → Profile → API Tokens
2. Revoke the old token, generate a new one
3. Do NOT paste it in any chat — go straight to step 5 below

## 3. Create a Google service account (one-time, ~5 minutes)
1. console.cloud.google.com → create a project (any name)
2. Search bar → "Google Drive API" → Enable
3. APIs & Services → Credentials → Create Credentials → Service Account
4. Name it anything, click through defaults, Done
5. Click the service account → Keys tab → Add Key → Create new key → JSON
   (this downloads a file — keep it private)
6. Open that file, find `"client_email": "...@....iam.gserviceaccount.com"`,
   copy that address

## 4. Create and share the Google Drive folder
1. Google Drive → New folder (e.g. "Mandamus Case Data")
2. Right-click → Share → paste the service account email from 3.6 →
   Editor access → Send
3. Open the folder, copy the ID from the URL (the part after `/folders/`)

## 5. Add three GitHub secrets
Repo → Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `COURTLISTENER_TOKEN` | the fresh token from step 2 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | entire contents of the JSON file from step 3.5 |
| `GDRIVE_FOLDER_ID` | the folder ID from step 4.3 |

## 6. Enable Actions and run a manual test
1. Repo → Actions tab → enable workflows if prompted
2. Click "Daily mandamus case collection" → "Run workflow" (manual trigger,
   don't wait for the schedule)
3. Watch it run. It runs the test suite first automatically — if that fails,
   nothing touches the real API and you'll see exactly which test failed.
4. Green checkmark = working. Check your Drive folder for `dashboard.html`,
   `cases.json`, etc.
5. Red X = something failed — paste the error back and it gets fixed before
   re-running.

## After this
It runs itself daily at 3am UTC. Automatic email from GitHub if a run ever
fails. Open `dashboard.html` from Drive any time — no need to check GitHub.
