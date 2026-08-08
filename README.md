# Mandamus Case Dataset

**Start here: [SPEC.md](SPEC.md)** — the full requirements and architecture
document. Nothing about this system should be assumed or changed without
checking it against SPEC.md first.

**Setup (no code required): [SETUP.md](SETUP.md)**

## Repo layout
```
SPEC.md                        <- read this first, it's the source of truth
SETUP.md                       <- one-time, no-code setup checklist
requirements.txt
scripts/
  extraction.py                <- pure rule-based field logic (unit-tested)
  collector.py                 <- talks to CourtListener, applies rate limits
  dashboard.py                 <- builds the offline HTML viewer
  drive_uploader.py            <- syncs results to Google Drive
tests/
  test_extraction.py           <- unit tests for every derived field
  test_collector_integration.py <- proves checkpoint/resume/crash-recovery actually work
  test_dashboard.py            <- proves the dashboard reflects real data, not just the empty case
.github/workflows/collect.yml  <- the daily automation; runs tests first
data/                          <- output lands here (empty until first run)
```

This repo contains exactly one system: this one. No legacy or parallel code
(see SPEC.md section 10 for what happened to the earlier partial run).

## Before you deploy
Run the tests locally or just trust that GitHub Actions runs them
automatically before every collection run (see the workflow file) — either
way, nothing untested touches the real CourtListener API.
