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
| 3 | **Hard budget ceiling of $50 total** (revised 2026-08-09; was $0) | Money is allowed, but only where it buys *reasoning* that is unavailable free. Free sources must be exhausted first — see §14. Runner cost is $0 because the repo is public (§12). |
| 4 | Must respect CourtListener rate limits | Free tier: 5/min, 50/hour, 125/day. Tier 1 membership ($10/mo): 10/min, 75/hour, 300/day — also the only way to reach the PACER APIs. Rolling windows either way. |
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

- **Execution environment:** GitHub Actions, scheduled (cron), **every 4
  hours** (not once/day — revised 2026-08-09). Runs whether the user's
  laptop is on or off. Each run is a fresh, temporary machine — no
  persistent server, no continuous process. Running more often does not
  increase total daily API volume (still capped at 125/day by
  CourtListener, see §8) — it exists for two reasons: (1) fresher data,
  since a case that concludes mid-day no longer waits up to 24h to be
  noticed, and (2) resilience against a single huge rate-limit backoff
  consuming an entire run's timeout unproductively (see §8's max-backoff
  cap).
- **Only one run at a time** (`concurrency: group: mandamus-collection,
  cancel-in-progress: false`, added 2026-08-09). Every-4-hours scheduling
  makes overlapping runs a real possibility (e.g. runner-queue delays);
  a new run queues behind an in-progress one rather than racing it or
  killing its in-progress collection.
- **The final commit rebases onto `main` before pushing**
  (`git pull --rebase origin main`, added 2026-08-09). If any other
  commit lands on `main` while a run is executing — a manual push during
  active development, or, in principle, an overlapping run — a plain
  `git push` would be rejected as non-fast-forward, and since the runner
  is destroyed right after, that run's entire collected dataset would be
  lost, not just delayed. Rebasing first (onto the isolated `data/` path
  the bot owns) avoids that.
- **State across runs:** a checkpoint file, committed back to the GitHub
  repo after every run. This is what lets independent, temporary runs behave
  like one continuous collection process. `seen_ids` is derived from both
  the checkpoint *and* the actual `docket_id`s already present in
  `cases.json` (union of the two) — `cases.json` is written before
  `checkpoint.json` on every save, so if a run is killed between those two
  writes, trusting the checkpoint alone would let a case be silently
  re-mined and duplicated on the next run.
- **Workflow steps after collection run independently of each other's
  success** (`if: always()` on dashboard build, Drive upload, and the
  repo commit). A transient Drive API failure must never block the
  GitHub commit — GitHub is the source of truth (below); losing it because
  a convenience-copy step hiccuped would defeat the whole point of that
  distinction. The pre-API test gate is unaffected by this — it still
  blocks the collector step outright on failure.
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
  §6.4); **+1** instead if it is `dcd` (persuasive, see §15)
- **+1** for each of these strings found in the docket's `cause` field
  (case-insensitive), capped at +3: `"mandamus"`, `"unreasonable delay"`,
  `"221(g)"`, `"administrative processing"`
- **+2** if `dateTerminated` is within the last 2 years (as of run time)
- **+1** if `dateTerminated` is within the last 4 years (and not already
  +2)

Highest score mined first within the run. This score is not stored on the
final record — it only controls processing order within a single run.

### 5.2 Bulk discovery — two-tier candidate sourcing (added 2026-08-09)

Discovery no longer relies solely on the live search API. CourtListener
publishes its entire `search_docket` table as a free, unlimited, no-
membership-required bulk CSV snapshot, refreshed quarterly (last day of
Mar/Jun/Sep/Dec) — confirmed by directly browsing wiki.free.law and the
live S3 bucket (`com-courtlistener-storage`), not assumed (§13). Before
this, `collector.py` spent a meaningful share of the shared daily request
budget (§8) on paginated search requests just to *discover* candidates —
budget that never went toward the one step bulk data can't replace
(`docket-entries`, PACER-gated, not included in the bulk export).

**Tier 1 — bulk snapshot (free, offline, quarterly):**
`scripts/bulk_docket_filter.py` streams and filters a downloaded
`dockets-YYYY-MM-DD.csv.bz2` (~5GB compressed) in one pass — the full
uncompressed CSV (~15–25GB) is never materialized on disk. Filters on
`court_id` (9th Circuit ∪ `dcd`, same set as §6.4/§15), `date_terminated >=
2020-01-01`, and a keyword/`nature_of_suit` match (deliberately broader
than the live query — over-matching here is the safe failure mode, same
philosophy as "store everything" in §13's decision log). Output:
`data/bulk_discovered_dockets.json`, with `caseName`, `court_id`,
`docketNumber`, `dateFiled`, `dateTerminated`, `docket_absolute_url`, and
`cause` fields matching the live search API's result schema exactly, so
`compute_priority_score`/`build_case_record` (extraction.py) need no
changes to consume either source. **Not part of the 4-hour schedule** — run
manually whenever a new quarterly dump lands, since the source itself only
refreshes that often.

**Tier 2 — live search (gap-filler only):** `collector.py` reads the bulk
file as its primary candidate source, then runs a small live search capped
at `INCREMENTAL_SEARCH_MAX_PAGES` (1 page, down from `MAX_SEARCH_PAGES_PER_RUN`
= 5) scoped to `dateTerminated >= <bulk snapshot date>` — catching only
cases newer than the snapshot, not re-crawling everything since 2020. If no
bulk snapshot exists yet, `collector.py` falls back to the original
full-crawl behavior unchanged (graceful degradation).

**What bulk data does NOT replace:** `docket-entries` (PACER-gated, not in
the bulk export) — every bulk- or search-discovered candidate still needs
this per-docket API call, which is why moving discovery off the budget
matters: the full daily budget now goes toward entries fetches instead of
being split with search pagination.

A much larger, free, nationwide **Opinions bulk file** (~54.5GB compressed,
full text for essentially every opinion CourtListener has) was identified
during the same research pass but deliberately scoped out of that change.
It has since been built out — see §5.3.

### 5.3 Bulk opinion corpus — replacing the API opinion fetcher (added 2026-08-09)

`scripts/opinion_fetcher.py` fetched published opinion text through the
CourtListener API: ~2 requests per case, 13s apart, capped at 120/day, for
the 35 hand-curated seed citations. Its first real run exposed two problems
at once — it produced only 18 hits, and 3 of the 4 low-confidence matches
were entirely the wrong case, because API search matches on a *guessed case
name* (§13). Meanwhile the same text sat locally, in full, in a bulk file
that costs nothing to read.

**Three bulk files, joined offline, replace that entirely:**

| File | Size | Supplies |
|---|---|---|
| `opinions-YYYY-MM-DD.csv.bz2` | 54.5 GB | `plain_text` (and HTML/XML fallbacks) + `cluster_id` |
| `opinion-clusters-YYYY-MM-DD.csv.bz2` | 2.46 GB | `case_name`, `date_filed`, `precedential_status`, `citation_count`, `docket_id` |
| `dockets-YYYY-MM-DD.csv.bz2` | 5.01 GB | `court_id` (already downloaded for §5.2) |

Join chain, confirmed against the live `schema-2026-06-30.sql`:
`search_opinion.cluster_id` → `search_opinioncluster.id` →
`.docket_id` → `search_docket.id` → `.court_id`. The clusters file is the
load-bearing middle link: without it an opinion's text has no court, so
venue ranking (§15) is impossible.

**`scripts/build_opinion_index.py`** streams clusters and dockets into a
disk-backed SQLite index. SQLite rather than in-memory dicts deliberately —
the dockets table is ~71.7M rows, which would be several GB of Python
objects. Indexes are built after bulk insert, not during.

**`scripts/mine_opinion_corpus.py`** streams the 54.5GB opinions file once,
keeping an opinion only if its text hits **both** keyword groups: delay
language (`mandamus`, `unreasonable delay`, `TRAC`, `1361`, `706(1)`, …)
**and** visa language (`visa`, `consular`, `221(g)`, `administrative
processing`, …). Either group alone is far too broad nationwide —
"mandamus" alone sweeps in prisoner petitions, "visa" alone sweeps in
credit-card disputes. All keyword matching uses word-boundary regex, not
substring, after the `"visa"`-inside-`"Divisadero"` false positive the
sibling docket filter hit for real (§13).

Ranking is transparent and rule-based, same philosophy as §6.4's
`relevance_score` — court tier (`ca9`/`scotus` binding > 9th Cir. districts
> `dcd`/`cadc` > rest), published status, citation count, recency, and
doctrine signals (TRAC, consular nonreviewability, 221(g)).

**Still no AI in the pipeline.** Consistent with §6.5, these scripts rank
and tag mechanically and store opinion text verbatim; they never summarize
or interpret. Analysis of the resulting corpus happens in conversation with
a human reading and verifying, not as a generated field in the data.

**Cost:** zero API requests — the daily budget (§8) is untouched, freeing it
entirely for `collector.py`'s docket-entries work. The one real cost is
time: bz2 decompression is inherently single-threaded (measured 17.6 MB/s
on this hardware), so the full pass takes roughly 4–5 hours. It is a
one-time, offline, unattended job per quarterly snapshot, and is **not** on
the 4-hour schedule.

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
4. **+2** if court is in the 9th Circuit district list (see code) —
   raised from +1 on 2026-08-09 once the filing venue was confirmed (§15)
5. **+1** if court is `dcd` — out of circuit and only persuasive, but most
   consular-delay law is made there, so it beats a neutral district (§15)
6. **+1** if `outcome == "settled"` (lower weight — usually no real
   reasoning; the government relented before any ruling)
7. **+1** if `similarity_to_own_case == true`

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
- Daily cap: stop cleanly at 120 requests **per calendar day** (under the
  125/day limit, leaving headroom for retries) — shared across all runs
  that day via the checkpoint's `date_of_counter`/`requests_made_today`,
  not reset per run. Now that runs happen every 4 hours (§4), this is
  what actually prevents exceeding CourtListener's real daily limit.
- Each run fetches up to `MAX_SEARCH_PAGES_PER_RUN` (5) consecutive search
  pages before mining, using most of the remaining daily budget rather
  than stopping after one page (see §5)
- HTTP 429: back off using the `Retry-After` header, then continue —
  **unless** it exceeds `MAX_BACKOFF_SECONDS` (1800 / 30 minutes,
  revised 2026-08-09), in which case the run stops cleanly instead of
  sleeping through it. One real case hit a 66,019-second (~18.3 hour)
  backoff; sleeping through that would waste nearly an entire run's
  timeout doing nothing. Since runs now happen every 4 hours, giving up
  and letting a later run retry costs nothing — the daily total is
  capped either way.
- All of the above is enforced in one single function that every API call
  goes through — there is no code path that can bypass it
- Workflow job timeout: 90 minutes. A single bad-luck `Retry-After` under
  the 30-minute cap, combined with ~120 requests at the 13-second floor
  throttle (~26 minutes), can still take a while even with nothing
  broken — 90 minutes gives a run room to reach its own natural stopping
  point instead of being cut off early by an arbitrary shorter ceiling.

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
- **The GitHub repo is public** (revised 2026-08-09; was private). Private
  repos get only 2,000 free Actions minutes/month and bill $0.006/min
  after; at the every-4-hours cadence (§4) that is ~$10–20/month of pure
  runner overage, much of it spent sleeping through rate-limit backoffs.
  Public repos get unlimited free minutes. Nothing in this repo is
  sensitive: it holds public federal court records, code, and docs. Before
  the switch, the full git history was audited for committed credentials
  (all blobs, all commits) — the only match was a placeholder string in
  SETUP.md, and `secrets/` has always been gitignored.
- Verify before every future commit that no real credential has entered
  the working tree — the repo being public makes any leak immediate and
  permanent, not merely risky.

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
| Run frequency (2026-08-09) | Every 4 hours, not once/day — same 125/day ceiling either way, but fresher data (a case concluding mid-day isn't noticed a day late) and avoids one run wasting its timeout sleeping through a single huge backoff |
| Max backoff tolerance (2026-08-09) | Capped at 1800s (30 min) — a live run hit a 66,019s (~18.3h) `Retry-After`; sleeping through that wastes almost an entire run for no benefit once runs happen every 4 hours, since a later run picks up the retry at no cost to the daily total |
| Overlapping runs (2026-08-09) | Guarded with a `concurrency` group (queue, don't cancel) — more-frequent scheduling makes overlap a real possibility, and two runs racing on `data/` and the git push is worse than one waiting its turn |
| Rebase before push (2026-08-09) | Added `git pull --rebase` before the final push — a plain push failing as non-fast-forward (e.g. a manual commit landing mid-run) would silently lose that entire run's collected data, since the runner is destroyed immediately after |
| Repo visibility (2026-08-09) | Public, not private — private repos bill $0.006/min past 2,000 Actions minutes/month, which at a 4-hourly cadence is ~$10–20/month of pure runner overage. Public is unlimited and free. The repo holds only public court records; git history was audited for credentials before the switch |
| Budget ceiling (2026-08-09) | Raised from $0 to $50, but spend order is enforced: exhaust free sources (curated advisories → free full-text opinions → free RECAP documents) before any purchase. Money buys *reasoning*, never data that is already free |
| CourtListener membership (2026-08-09) | Tier 1 at $10/month — the single highest-leverage spend available. Raises 125→300 requests/day and is the only route to the PACER APIs (closed to free accounts since May 2026). Cancellable once collection completes |
| Primary substance source (2026-08-09) | Reported decisions via the free Opinions API (`type=o`, `plain_text`), **not** the PACER-gated RECAP dockets used for discovery. This is what closes the "we can see *what* happened but not *why*" gap that limited the original design |
| Rate limiter location (2026-08-09) | Moved out of collector.py into api_client.py once opinion_fetcher.py became a second consumer. SPEC 8 forbids a second parallel implementation, and both scripts must share one daily budget via the same checkpoint file |
| Seed vs discovery priority (2026-08-09) | opinion_fetcher runs BEFORE collector in the workflow. A curated seed opinion (already vetted by practicing litigators) outranks another arbitrary discovered docket when they compete for the same request budget. Self-balancing: once the seed resolves, the fetcher no-ops and the collector reclaims the full budget |
| Wrong-opinion safety (2026-08-09) | opinion_fetcher flags any case-name match below 0.6 word overlap as `needs_manual_match_check` rather than trusting or discarding it. A misattributed opinion feeding a legal filing is a worse failure than a missing one |
| Foundational authorities (2026-08-09) | Added a separate `foundational_authorities` section to the seed list -- TRAC v. FCC itself, Kerry v. Din, Kleindienst v. Mandel, Saavedra Bruno v. Albright. These define the legal standard rather than serving as case-outcome analogs, so they don't fit the `direction` field and get their own schema. Fetched first in every run, ahead of even 9th Circuit priority cases, since they are few (4) and relevant to every other case regardless of circuit |
| Drive sync gap (2026-08-09) | drive_uploader.py never synced seed_citations.json or data/opinions/*.txt -- found while doing parallel-safe local dev during the rate-limit wait. Fixed: both now sync, opinion filenames discovered fresh each run (not hardcoded, since new ones appear as opinion_fetcher.py resolves more cases). Without this, fetched opinions would sit in the git repo but never reach Drive, the user's actual interface (section 9) |
| Dashboard visibility gap (2026-08-09) | dashboard.py had zero awareness of seed_citations.json -- another parallel-safe fix found the same way. Added a stat card (opinions found / total seed+foundational items) and a second table listing every seed case and foundational authority, sorted 9th Circuit/foundational first (matching opinion_fetcher.py's own fetch order), with a `needs_manual_match_check` flag surfaced inline rather than only visible in the raw JSON |
| Bulk data is free, no membership needed (2026-08-09) | User asked whether pairing a paid membership with the bulk CSV would be more efficient. Verified live (browsed wiki.free.law + the actual `com-courtlistener-storage` S3 bucket, zero auth required) that bulk data is unlimited/free regardless of membership tier -- membership only raises REST API rate limits, unrelated to bulk-data access. This reframed the ask: the real lever is moving discovery off the API budget entirely, not paying for a higher rate limit |
| Bulk file sizes (2026-08-09) | Confirmed directly from the S3 bucket listing, not estimated: `dockets-2026-06-30.csv.bz2` = 5.01GB compressed; `opinions-2026-06-30.csv.bz2` = 54.5GB compressed. Schema pulled from the matching `schema-2026-06-30.sql` confirmed `search_docket` has `court_id`, `case_name`, `cause`, `nature_of_suit`, `date_filed`, `date_terminated`, `slug` -- everything live search discovery needs -- but no entries/document-text columns, confirming bulk data replaces discovery only, not the PACER-gated docket-entries step |
| Opinions bulk file scoped out (2026-08-09) | User chose to keep this change scoped to the 5GB Dockets file only; the 54.5GB Opinions file (nationwide full-text case-law mining, beyond the 31 curated seed cases) was downloaded in parallel for future use but is not processed or wired into the pipeline by this change |
| Membership tier decision deferred (2026-08-09) | Whether any paid tier is worth it now depends on the real candidate count the bulk filter produces, not a guess -- see §5.2. Compute backlog-clearing time at each tier's daily cap once that number exists, and only spend if the free-tier timeline is unreasonable, per the existing spend-order rule below |
| First real bulk-filter run found a false-positive bug (2026-08-09) | Ran `bulk_docket_filter.py` against the real 71.7M-row `dockets-2026-06-30.csv.bz2` -- spot-checking a random sample of matches (per this file's own "verify before trusting" rule) caught `"visa"` matching as a plain substring inside unrelated words, e.g. `"Garcia v. Divisadero Sports Bar LLC"` (an ADA case, di-**visa**-dero). Fixed by switching all keyword matching to word-boundary regex (mirroring the `\b...\b` style already used in `extraction.py`'s marker lists), with a regression test locking in the exact case. Filter rerun after the fix; this is the kind of thing the mandated spot-check step exists to catch |
| RECAP checker built but not wired live (2026-08-09) | `scripts/recap_checker.py` (SPEC §14 tier 3) built and unit-tested during the same parallel-safe window as the dashboard/Drive fixes, following the same test-first pattern as `opinion_fetcher.py`. Deliberately NOT added to `collect.yml` yet: its docket-scoped filter parameter is CourtListener's documented convention, not something confirmed against a live response. Wiring an unverified param into an unattended 4-hour cron risks silently burning budget on an endpoint that returns nothing -- verify with one real call first |
| Drive uploader switched to OAuth (2026-08-09) | A live run hit `storageQuotaExceeded` creating a new file via the service account -- Google service accounts have zero storage quota and can only ever update pre-existing files, never create new ones, in a regular (non-Shared-Drive) folder. Personal Gmail doesn't support Shared Drives (Workspace-only), so switched `drive_uploader.py` to OAuth as the user's own account instead, scoped to `drive.file` (not the broader `drive` scope) so the credential can only ever touch files it created itself, not the rest of the user's personal Drive. One-time local consent flow lives in `scripts/oauth_setup.py` (never run in CI). Caveat: because `drive.file` scope only sees files the app itself created, files the old service account created won't be visible to the new identity -- `find_existing_file` will create fresh duplicates for those specific filenames until the old ones are manually deleted from the Drive folder |
| Bulk corpus replaces the API opinion fetcher (2026-08-09) | User challenged why we fetch opinion text through the API at all when the 54.5GB opinions bulk file sits locally holding the same text. They were right, and the framing I'd given earlier ("the bulk file covers different data than the scheduled runs") was wrong for half the pipeline -- true of `collector.py`, false of `opinion_fetcher.py`, which was re-fetching data we already had. Verified the missing link is `opinion-clusters` (2.46GB, downloaded), confirmed no `docket-entries` bulk file exists (so `collector.py`'s API path is genuinely irreplaceable), and built §5.3 to mine the corpus offline instead. Bonus: exact `cluster_id` joins eliminate the wrong-opinion failure mode that name-guessing search produced |
| No AI fields in the corpus output (2026-08-09) | Asked directly whether "AI" meant an LLM-generated field inside the pipeline's own output or me analyzing on request in conversation. User chose the latter, so §6.5's no-AI-in-pipeline rule stands unchanged: scripts rank and tag mechanically and store text verbatim; interpretation happens in conversation with a human verifying, never as a written-back data field. Avoided a real SPEC violation by asking rather than assuming |
| bz2 mining cannot be parallelized here (2026-08-09) | User asked to use ~80% of CPU to speed up the 54.5GB pass. Measured actual throughput (17.6 MB/s) and checked options honestly rather than promising a speedup: bzip2 is a continuous stream, `pbzip2`/`lbzip2` are not installed (and installing an unvetted binary is off-limits), and pre-decompressing to disk needs ~250-300GB against ~96GB free. Machine has 4 cores/8 threads, but this stage stays single-threaded at ~4-5 hours. Accepted as an unattended background cost since it blocks nothing |
| Case profile is the real missing input (2026-08-09) | Strategic review surfaced that SPEC.md records the venue (9th Circuit) but never the user's own case facts -- visa category, delay duration, 221(g) status, principal vs derivative. Without those, "find cases similar to mine" has no definition of "mine". Highest-value remaining input and it costs zero compute. Collected into `C:\temp\...\case_details\` deliberately OUTSIDE the repo, since the repo is public (§12) |
| First real opinion_fetcher run validated the match-confidence safety net (2026-08-09) | The rate-limit cooldown cleared and the ~16:13 UTC scheduled run (delayed to 16:52 by GitHub's queue) made real calls for the first time: 18/35 seed items resolved to `found_free_opinion`, 4 flagged `needs_manual_match_check`. Content-verified all 4 by reading the actual fetched text, not just trusting the flag: 3 were genuinely wrong opinions (`Patel v. Reno` matched an unrelated SF firefighters sanctions case, `Rivas v. Napolitano` matched an IRS tax-lien case, `Mohammad v. Blinken` matched `Zoroofchi v. Rubio`) and 1 was a false alarm (`Al-Gharawy v. DHS` matched correctly, just under the 0.6 threshold because of the fuller legal name). No code bug -- the flag caught every real problem and over-flagged by exactly one, the safe direction. Notable: both of the only two 9th Circuit matches this run were the wrong-opinion cases, so 9th Circuit opinion coverage is still effectively zero pending a resolved rerun |

## 14. Budget allocation and spend order (added 2026-08-09)

Total ceiling: **$50** (§2, constraint 3). Money is spent only on what is
provably unavailable for free, and only in this order. Each tier must be
exhausted before the next is touched.

| Tier | Source | Cost | What it yields |
|---|---|---|---|
| 0 | GitHub Actions on a public repo | $0 | All automation runtime |
| 1 | Curated practice advisories (NILA / American Immigration Council) | $0 | ~23 consular-processing cases already analyzed by practicing litigators, grouped by which government argument they answer |
| 2 | CourtListener Opinions API (`type=o` → `plain_text`) | $0 | Full text of *reported* decisions — the actual judicial reasoning |
| 3 | RECAP documents where `is_available=True` | $0 | Filed documents another user already purchased and donated. **`scripts/recap_checker.py` built and unit-tested (2026-08-09), not yet wired into `collect.yml`** — the docket-scoped filter param it uses (`docket_entry__docket_id`) is CourtListener's documented convention but unverified against a live response; confirm with one real API call before enabling on the schedule |
| 4 | CourtListener Tier 1 membership | ~$10–20 | **Deferred, not yet purchased.** Buys 300 req/day (2.4×) and opens the PACER *endpoints* — it does NOT unlock any content non-members cannot see, and does NOT make PACER documents free. Only worth buying if tier 5 turns out to be necessary. |
| 5 | Targeted PACER purchases | ~$25–30 | Only motion-to-dismiss rulings that are (a) 9th Circuit, (b) high relevance, (c) provably absent from tiers 1–3 |

**Why tier 4 is deferred (2026-08-09):** the seed backfill is ~27 cases at
~2 requests each (~54 requests), and the free tier allows 125/day — so the
single highest-value piece of work fits inside the free tier at no cost. The
judicial reasoning this project actually needs comes from the free Opinions
API, not from membership. Membership buys throughput on lower-value bulk
discovery plus the *ability* to purchase PACER documents; neither is known to
be needed until the free tiers are exhausted. Revisit only if a 9th Circuit
motion-to-dismiss ruling proves genuinely unavailable free.

**Tier 5 is never automatic** (§2, constraint 7). Every purchase requires
explicit per-document approval, and the running total is tracked in
`data/spend_log.json` with a hard stop before the ceiling.

## 15. Venue targeting (added 2026-08-09)

The user's case will be filed in the **9th Circuit**. This changes what
counts as valuable, and the scoring must reflect it:

- **9th Circuit district decisions are binding-adjacent** — most persuasive
  for the eventual complaint, and the top priority for any paid acquisition.
- **D.D.C. decisions carry outsized weight despite being out-of-circuit**,
  because the majority of consular-delay mandamus litigation is filed there
  and its TRAC-factor case law is the most developed. Persuasive, not
  binding — but unavoidable as context.
- All other districts are background signal only.
