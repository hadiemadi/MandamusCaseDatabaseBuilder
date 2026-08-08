# Mandamus Case Dataset — System Specification (v2)

Status: **authoritative**. This document supersedes:
- the earlier `mandamus_cloud_bundle/mandamus-cloud/` code found in the
  `hadiemadi/MandamusCaseDatabaseBuilder` repo (partial run, undocumented origin,
  stalled mid-collection ~7 weeks ago)
- the first draft built earlier in this same chat session

Nothing in this project should be built, changed, or assumed outside of
what's written here. If a decision isn't in this document, it hasn't been
made yet — ask, don't assume.

---

## 1. Purpose

Build an unattended, always-on pipeline that collects U.S. federal
consular-visa-delay mandamus/APA cases from CourtListener into a structured,
durable dataset. The dataset is a supporting research tool for a future,
separate project: drafting an individual mandamus complaint. **This project
does NOT draft, analyze, or reason about the cases with AI. It only fetches,
extracts fields with fixed rules, validates, and stores.**

## 2. Non-negotiable constraints

| # | Constraint | Why |
|---|---|---|
| 1 | No AI/LLM calls anywhere in the runtime pipeline | Data collection must be deterministic, auditable, and free of hallucination risk |
| 2 | Must run with laptop off | User is not always online; a scheduled cloud job is required |
| 3 | Must be free / near-zero cost | GitHub Actions free tier (public or private, see §4) + free CourtListener tier |
| 4 | Must respect CourtListener free-tier limits | 5 requests/min, 50/hour, 125/day, rolling windows |
| 5 | User does not write or manage code | All code is delivered pre-built; user only does one-time, no-code setup clicks |
| 6 | Full case text stored for ALL cases, not just a shortlist | Settled cases (no published opinion) may be the most strategically valuable; excluding them by default is wrong |
| 7 | PACER purchases (paid) are never automatic | Cost decisions are made later, manually, once cases can be ranked |

## 3. Explicitly out of scope for this build

- Any drafting, summarizing, or legal-reasoning use of the collected data — that is a **separate, future project**
- Automatic PACER purchases
- Semantic/AI-based similarity ranking (similarity is rule-based text matching only, see §6)
- Migrating or merging the old `mandamus-cloud` partial run — see §10 for what happens to it

## 4. Architecture

```
CourtListener API  <---- (scheduled fetch) ----  GitHub Actions (the "harness")
                                                        |
                                                        v
                                          data files committed to GitHub repo
                                          (source of truth + version history)
                                                        |
                                                        v
                                          copied to a Google Drive folder
                                          (the place the user actually opens)
```

- **Execution environment:** GitHub Actions, scheduled (cron). Runs whether
  the user's laptop is on or off. Each run is a fresh, temporary machine —
  no persistent server, no continuous process.
- **State across runs:** a checkpoint file, committed back to the GitHub
  repo after every run. This is what lets independent, temporary runs behave
  like one continuous collection process.
- **Source of truth for data:** the GitHub repo (git gives free version
  history / audit trail).
- **User-facing copy:** the same data files + a self-contained HTML
  dashboard, uploaded to a Google Drive folder after every run, so the user
  never has to open GitHub to see progress.
- **No component of this architecture requires an LLM at runtime.**

## 5. Data source and scope

- **Source:** CourtListener REST API v4 (`https://www.courtlistener.com/api/rest/v4/`)
- **Case types:** federal consular visa-delay mandamus / APA "unreasonable
  delay" cases, filed 2020–present
- **Inclusion keywords (search query):** `"writ of mandamus"`,
  `"unreasonable delay"`, `"221(g)"`, `"administrative processing"`,
  `"consular"`, `"visa"`
- **Exclusions:** USCIS adjustment-of-status cases, asylum cases,
  naturalization cases, plain visa denials with no delay claim (per the
  Phase 0 spec already on file in this project)
- **Only concluded (terminated) dockets are collected.** Revised
  2026-08-08 (second revision) — open dockets have no analyzable content
  (no outcome, no reasoning, no MTD ruling to learn from) and are
  explicitly not useful for this project's purpose. Rather than collecting
  them and filtering later, they are excluded at the query level: the
  search query includes a `dateTerminated:[2020-01-01 TO *]` range clause,
  so every result the API returns is already terminated, regardless of
  when it was originally filed. This also fixes an earlier problem with
  sorting by filing date: a case filed in 2021 that concluded last week
  would be buried deep in a filed-date ordering and effectively never
  seen. Filtering by conclusion date instead makes filing date irrelevant
  to discoverability.
- **Base search order: newest-filed-first** (`order_by=dateFiled desc`)
  *within* the terminated-only result set — this is just a stable
  pagination order; actual mining priority is decided locally (§5.1) using
  the real `dateTerminated` value, which every result now has.
- **Multiple search pages per run.** Each run fetches consecutive pages
  (advancing the checkpoint offset) up to `MAX_SEARCH_PAGES_PER_RUN` or
  until the daily request cap is reached — using most of the available
  budget instead of one page/run.
- **Offset resets to 0 once a run reaches the true end of search
  results** (no more `next` page). A case filed long ago that concludes
  recently would otherwise be missed forever once the offset has advanced
  past its position in the filed-date ordering. Resetting periodically
  re-scans the full terminated-only set from the top; already-seen
  dockets are skipped cheaply (by `docket_id`) before any per-docket
  entries fetch, so this costs only a few search-page requests, not full
  re-mining.
- **Defensive skip:** if a result somehow lacks `dateTerminated` despite
  the query filter (e.g. search-index lag), it is silently skipped, not
  recorded. This should not normally happen.

### 5.1 Pre-mining priority score — which dockets get mined first

Within a run's fetched batch, dockets are mined in **priority order**, not
raw search order. This is a separate score from `relevance_score` (§6.4)
— it must be computable from search-result fields alone, *before* any
docket-entries fetch, since its purpose is deciding what to spend API
budget on next. Fully rule-based, no AI:

Starts at 0. Add:
- **+2** if `court_id` is in the 9th Circuit district list (same list as
  §6.4)
- **+1** for each of these strings found in the docket's `cause` field
  (case-insensitive), capped at +3: `"mandamus"`, `"unreasonable delay"`,
  `"221(g)"`, `"administrative processing"`
- **+2** if `dateTerminated` is within the last 2 years (as of run time)
- **+1** if `dateTerminated` is within the last 4 years (and not already
  +2)

Highest score mined first within the run. This score is not stored on the
final record — it only controls processing order within a single run.

## 6. Data schema

One JSON record per case. Every field below is either pulled directly from
the API or derived by **fixed regular-expression matching against docket
entry text** — no field is an AI judgment call.

### 6.1 Identifiers (from API, no derivation)
| Field | Type | Source |
|---|---|---|
| `docket_id` | int | CourtListener docket ID |
| `case_name` | string | API |
| `court` | string | court ID, e.g. `cand` |
| `docket_number` | string | API |
| `date_filed` | date | API |
| `date_terminated` | date or null | API |
| `citation` | string | constructed: `{case_name}, No. {docket_number} ({court})` |
| `source_url` | string | constructed: `https://www.courtlistener.com/docket/{docket_id}/` |

`date_terminated` is never null in stored records — the search query
(§5) only ever returns already-terminated dockets, so every record gets
the full §6.2 mining pass.

### 6.2 Derived fields (rule-based, from docket entry text)
| Field | Type | Derivation rule |
|---|---|---|
| `days_to_resolution` | int or null | `date_terminated - date_filed` |
| `procedural_posture` | enum | furthest matching stage marker, see `PROCEDURAL_STAGE_MARKERS` in code |
| `outcome` | enum | first matching outcome marker, see `OUTCOME_MARKERS` in code |
| `disposition_confidence` | `high` \| `needs_manual_review` \| `unknown` | flags settlement/mootness ambiguity — see §6.3 |
| `similarity_to_own_case` | boolean | text match for derivative/principal-applicant language — **always needs manual confirmation, never trusted alone** |
| `relevance_score` | int 0–4 | transparent point rule, see §6.4 |
| `has_full_opinion_text` | boolean | whether CourtListener returned free full text |
| `pacer_fetch_needed` | boolean | `not has_full_opinion_text` — flag only, no purchase |

### 6.3 Disposition confidence — why this exists
A case closed by "stipulation of dismissal" could mean the government
settled (visa issued to end the suit favorably) OR that the case became
moot for an unrelated reason. Docket text alone can't always distinguish
these. Any case matching both a settlement marker and a mootness marker is
flagged `needs_manual_review` rather than silently guessed either way.

### 6.4 Relevance score — exact rule (fully auditable, not a model output)
Revised 2026-08-08 — weights now reflect that a real ruling on the
government's defense is far more useful than a plain settlement, which
usually has no substantive reasoning at all (see decision log, §13).

Starts at 0. Add:
1. **+3** if `outcome == "mtd_denied"` (government's defense failed —
   shows what argument works against it)
2. **+3** if `outcome == "mtd_granted"` (government's defense succeeded —
   shows what to prepare for or distinguish)
3. **+2** if `procedural_posture == "summary_judgment_stage"` (reached
   substantive argument, regardless of final outcome)
4. **+1** if court is in the 9th Circuit district list (see code)
5. **+1** if `outcome == "settled"` (lower weight — usually no real
   reasoning; the government relented before any ruling)
6. **+1** if `similarity_to_own_case == true`

This is a sort/filter aid only — it does not gate what gets collected.
**Every case matching the search query is stored regardless of score.**

### 6.5 Fields explicitly left blank at collection time
| Field | Why |
|---|---|
| `court_reasoning_summary` | Requires reading full opinion/filed-document text and judgment about *why* a court ruled — this is analysis, not extraction. Out of scope here; filled later, by hand, only for cases actually used in drafting. |
| `trac_factor_details` | Same reason — which TRAC factors favored which side requires reading substantive argument, not matching a phrase. |

These fields exist as empty placeholders in the schema now so the database
never needs to be restructured later — they get populated by a human (or a
separate, explicitly-scoped project) only for the small set of cases that
end up actually cited.

## 7. Validation (separate from extraction — catches bugs, not legal correctness)

Run automatically after every record is built. Rule-based only.

| Check | Flags as |
|---|---|
| Any of `docket_id`, `case_name`, `court`, `docket_number`, `citation` missing | `missing_required_field:{name}` |
| `docket_id` already seen this run or in checkpoint | `duplicate_docket_id` |
| `date_terminated` earlier than `date_filed` | `date_terminated_before_date_filed` |
| Zero docket entries retrieved for a case | `no_docket_entries_retrieved` |

Flagged issues are written to `issues.json`, never silently dropped or
auto-corrected.

## 8. Rate limiting

- Floor throttle: 13 seconds between every API call (keeps well under the
  5/minute cap)
- Daily cap: stop cleanly at 120 requests/run (under the 125/day limit,
  leaving headroom for retries)
- Each run fetches up to `MAX_SEARCH_PAGES_PER_RUN` (5) consecutive search
  pages before mining, using most of the 120-request budget rather than
  stopping after one page (see §5)
- HTTP 429: back off using the `Retry-After` header, then continue
- All of the above is enforced in one single function that every API call
  goes through — there is no code path that can bypass it

## 9. Storage locations — exact roles

| Location | Role | What lives there |
|---|---|---|
| GitHub repo | Source of truth, checkpoint, version history | `data/cases.json`, `data/issues.json`, `data/checkpoint.json`, `data/run_log.json`, all code |
| Google Drive folder | User-facing convenience copy | same data files + `dashboard.html`, overwritten each run |

The Drive copy is **not authoritative** — if it's ever deleted or corrupted,
the GitHub repo is the real record and Drive gets re-populated on the next
run.

## 10. What happens to the old `mandamus-cloud` partial run

**Revised 2026-08-08.** The original decision (keep it in `legacy/` for
reference) is superseded. Current decision: **removed entirely from the
repository.** Reasoning for the change:

- The repo is meant to hold exactly one system going forward — this spec's.
  A second, undocumented, schema-incompatible version sitting alongside it
  is itself a source of ambiguity, which is the thing being eliminated here.
- The old run's raw case list (~100+ dockets, 7 fully mined) is not lost —
  it is preserved outside the repo, in this chat's project knowledge, and
  can be retrieved and re-run through the v2 extraction logic later as a
  deliberate, separate step if wanted. Nothing of value is destroyed by
  removing it from the repo.
- Going forward, the repository contains only what's described in this
  document. No parallel or legacy pipeline.

## 11. Monitoring

- GitHub's default email notification on failed scheduled runs (automatic,
  no setup)
- `dashboard.html` shows: total cases, flagged issues, PACER-needed count,
  manual-review-needed count, and a warning banner if no successful run has
  completed in the last 48 hours
- GitHub Actions tab gives a visual green/red run history, viewable without
  reading any code

## 12. Security

- CourtListener token and Google service-account key are stored only as
  GitHub encrypted secrets, never in code or in this chat
- The Google service account has access to exactly one Drive folder, not
  the user's whole Drive
- The GitHub repo is private

## 13. Decision log (for traceability — every ambiguity resolved during design)

| Decision | Resolution |
|---|---|
| PDF vs. extracted text | Extracted text is the default record; PDF only pulled deliberately for shortlisted cases later |
| Store only a shortlist vs. everything | Everything — settled cases may be more valuable, not less |
| Runtime uses Claude Code / an LLM loop | No — deterministic script only. "Loop/harness/context engineering" applies to *building* the code, not running it |
| Where does state live between runs | A checkpoint file committed to the GitHub repo, not local disk, not chat memory |
| Storage: GitHub vs. Drive vs. both | Both — GitHub is the source of truth, Drive is the convenience copy |
| How many shortlist candidates | No fixed number — `relevance_score` + manual review decide later; nothing is excluded from raw collection based on this |
| Mining order within a run (2026-08-08) | Priority-scored (§5.1: venue + cause-text + recency-of-conclusion), not raw search order — spends limited daily API budget on the most likely-relevant cases first |
| Pages per run (2026-08-08) | Up to `MAX_SEARCH_PAGES_PER_RUN`, using most of the daily budget — original one-page-per-run design covered the backlog too slowly |
| Open dockets (2026-08-08, superseded same day) | First tried: record with identifier-only fields. Superseded a few hours later — user clarified open cases have zero content value (no outcome, no reasoning yet); excluding them at the query level (via `dateTerminated` filter, §5) is both simpler and correct, so this was dropped entirely rather than kept as a low-priority record |
| What counts as "concluded" (2026-08-08) | `date_terminated` is not null (CourtListener's own PACER-close signal) — the only signal available *before* mining. A stricter definition (real substantive ruling, e.g. `outcome != "unknown"`) can't be known until after the entries fetch, so it's used post-hoc for relevance scoring (§6.4) instead |
| Relevance score weighting (2026-08-08) | `mtd_denied`/`mtd_granted`/summary-judgment-stage outweigh `settled` — a real ruling on the government's defense is far more useful than a plain settlement, which usually has no substantive reasoning (government relented before any ruling) |
| Sort by termination date directly (2026-08-08) | Not possible — confirmed via live API test that CourtListener's search only supports `order_by` on `score`, `dateFiled`, and `entry_date_filed`; no termination-date sort exists. Worked around by filtering the query on `dateTerminated:[2020-01-01 TO *]` instead of trying to sort by it |
