#!/usr/bin/env python3
"""
Builds one self-contained dashboard.html from the collected data.
SPEC.md section 11. No external scripts/CDNs — must work fully offline.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CASES_FILE = DATA_DIR / "cases.json"
ISSUES_FILE = DATA_DIR / "issues.json"
RUN_LOG_FILE = DATA_DIR / "run_log.json"
SEED_FILE = DATA_DIR / "seed_citations.json"
OUTPUT_FILE = DATA_DIR / "dashboard.html"


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def build_dashboard():
    cases = load(CASES_FILE, [])
    issues = load(ISSUES_FILE, [])
    run_log = load(RUN_LOG_FILE, [])
    seed = load(SEED_FILE, {})

    seed_cases = seed.get("cases", [])
    foundational = seed.get("foundational_authorities", {}).get("entries", [])
    seed_items = foundational + seed_cases

    opinions_found_count = sum(1 for c in seed_items if c.get("opinion_fetch_status") == "found_free_opinion")
    match_check_count = sum(1 for c in seed_items if c.get("needs_manual_match_check"))

    last_run = run_log[-1] if run_log else None
    stale_warning = False
    hours_since_last_run = 0
    if last_run:
        finished = datetime.fromisoformat(last_run["finished_at"])
        hours_since_last_run = (datetime.now(timezone.utc) - finished).total_seconds() / 3600
        stale_warning = hours_since_last_run > 48

    outcome_counts = {}
    for c in cases:
        o = c.get("outcome", "unknown")
        outcome_counts[o] = outcome_counts.get(o, 0) + 1

    pacer_needed_count = sum(1 for c in cases if c.get("pacer_fetch_needed"))
    review_needed_count = sum(1 for c in cases if c.get("disposition_confidence") == "needs_manual_review")

    # Same priority order opinion_fetcher.py fetches in: foundational authorities
    # first (universally relevant), then 9th Circuit, then D.D.C., then the rest.
    circuit_priority = {"9th": 0, "DC": 1}

    def seed_sort_key(item, is_foundational):
        return (0 if is_foundational else 1, circuit_priority.get(item.get("circuit"), 2), item.get("case_name", ""))

    seed_ordered = (
        sorted(((c, True) for c in foundational), key=lambda t: seed_sort_key(t[0], t[1]))
        + sorted(((c, False) for c in seed_cases), key=lambda t: seed_sort_key(t[0], t[1]))
    )

    STATUS_BADGE = {
        "found_free_opinion": "settled",
        "not_found": "unknown",
        "not_free_needs_pacer": "mtd_granted",
        "budget_exhausted": "review",
        "pending": "unknown",
    }

    def seed_row(item, is_foundational):
        status = item.get("opinion_fetch_status", "pending")
        badge_cls = STATUS_BADGE.get(status, "unknown")
        flags = ' <span class="badge review">check match</span>' if item.get("needs_manual_match_check") else ""
        link = ""
        if item.get("opinion_absolute_url"):
            link = f'<a href="https://www.courtlistener.com{item["opinion_absolute_url"]}" target="_blank">read →</a>'
        direction = item.get("why_it_matters", "") if is_foundational else item.get("direction", "")
        return (
            "    <tr>"
            f"<td>{item.get('case_name','')}</td>"
            f"<td>{item.get('citation','')}</td>"
            f"<td>{item.get('court','')}</td>"
            f"<td>{item.get('circuit','')}</td>"
            f"<td>{direction}</td>"
            f"<td><span class=\"badge {badge_cls}\">{status}</span>{flags}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )

    seed_rows = "\n".join(seed_row(item, is_f) for item, is_f in seed_ordered) or "    <tr><td colspan=\"7\">No seed data yet.</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Mandamus Case Dataset — Dashboard</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
          background: #f7f7f5; color: #1a1a1a; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .subtitle {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .banner {{ padding: 12px 16px; border-radius: 8px; margin-bottom: 16px; font-size: 14px; }}
  .banner.warn {{ background: #fdecea; color: #9c1f0e; border: 1px solid #f3b4ab; }}
  .banner.ok {{ background: #eaf7ec; color: #1e6b2e; border: 1px solid #b7e0bd; }}
  .stats {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 20px; }}
  .stat-card {{ background: white; border: 1px solid #e3e3e0; border-radius: 10px;
                padding: 14px 18px; min-width: 140px; }}
  .stat-card .num {{ font-size: 24px; font-weight: 600; }}
  .stat-card .label {{ font-size: 12px; color: #666; margin-top: 2px; }}
  .controls {{ margin-bottom: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
  input, select {{ padding: 6px 10px; border-radius: 6px; border: 1px solid #ccc; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px;
           overflow: hidden; font-size: 13px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; }}
  th {{ background: #efefec; cursor: pointer; position: sticky; top: 0; }}
  tr:hover {{ background: #fafafa; }}
  .badge {{ padding: 2px 8px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
  .badge.settled {{ background: #e6f4ea; color: #1e6b2e; }}
  .badge.mtd_denied {{ background: #e8f0fe; color: #1a56db; }}
  .badge.mtd_granted {{ background: #fdecea; color: #9c1f0e; }}
  .badge.unknown {{ background: #f1f1ee; color: #666; }}
  .badge.review {{ background: #fff4e5; color: #9a5b00; }}
  a {{ color: #1a56db; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  footer {{ margin-top: 16px; font-size: 11px; color: #999; }}
</style>
</head>
<body>

<h1>Mandamus Case Dataset</h1>
<div class="subtitle">Built per SPEC.md — auto-generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</div>

{f'<div class="banner warn">⚠ No successful run in {hours_since_last_run:.0f} hours — check the GitHub Actions tab.</div>' if stale_warning else '<div class="banner ok">Pipeline running normally.</div>'}

<div class="stats">
  <div class="stat-card"><div class="num">{len(cases)}</div><div class="label">Total cases collected</div></div>
  <div class="stat-card"><div class="num">{len(issues)}</div><div class="label">Flagged data issues</div></div>
  <div class="stat-card"><div class="num">{pacer_needed_count}</div><div class="label">Need PACER purchase for full text</div></div>
  <div class="stat-card"><div class="num">{review_needed_count}</div><div class="label">Settlement/mootness needs manual review</div></div>
  <div class="stat-card"><div class="num">{last_run['new_cases_this_run'] if last_run else 0}</div><div class="label">New cases in last run</div></div>
  <div class="stat-card"><div class="num">{opinions_found_count}/{len(seed_items)}</div><div class="label">Seed &amp; foundational opinions found (free)</div></div>
  {f'<div class="stat-card"><div class="num">{match_check_count}</div><div class="label">Opinion matches need manual check</div></div>' if match_check_count else ''}
</div>

<div class="controls">
  <input type="text" id="searchBox" placeholder="Search case name, court, docket #..." onkeyup="filterTable()" style="flex:1; min-width:240px;">
  <select id="outcomeFilter" onchange="filterTable()">
    <option value="">All outcomes</option>
    {''.join(f'<option value="{k}">{k} ({v})</option>' for k, v in sorted(outcome_counts.items()))}
  </select>
  <select id="sortBy" onchange="sortTable()">
    <option value="relevance_score">Sort: relevance score</option>
    <option value="days_to_resolution">Sort: days to resolution</option>
    <option value="date_filed">Sort: date filed</option>
  </select>
</div>

<table id="caseTable">
  <thead>
    <tr>
      <th>Case</th><th>Court</th><th>Filed</th><th>Terminated</th><th>Days</th>
      <th>Posture</th><th>Outcome</th><th>Relevance</th><th>Flags</th><th>Link</th>
    </tr>
  </thead>
  <tbody id="tableBody"></tbody>
</table>

<h2 style="font-size:16px; margin-top:28px;">Curated seed cases &amp; foundational authorities</h2>
<div class="subtitle">From advisory-sourced citations (SPEC.md section 14, tier 2) — full opinion text fetched free from CourtListener where available. 9th Circuit and foundational authorities are fetched first.</div>
<table id="seedTable">
  <thead>
    <tr>
      <th>Case</th><th>Citation</th><th>Court</th><th>Circuit</th><th>Direction</th>
      <th>Opinion status</th><th>Text</th>
    </tr>
  </thead>
  <tbody>
{seed_rows}
  </tbody>
</table>

<footer>Schema and rules defined in SPEC.md. No AI is used to generate any field in this table.</footer>

<script>
  const CASES = {json.dumps(cases)};

  function badgeClass(outcome) {{
    if (["mtd_denied","mtd_granted","settled"].includes(outcome)) return outcome;
    return "unknown";
  }}

  function renderTable(data) {{
    const body = document.getElementById('tableBody');
    body.innerHTML = data.map(c => `
      <tr>
        <td>${{c.case_name || ''}}</td>
        <td>${{c.court || ''}}</td>
        <td>${{c.date_filed || ''}}</td>
        <td>${{c.date_terminated || ''}}</td>
        <td>${{c.days_to_resolution ?? ''}}</td>
        <td>${{c.procedural_posture || ''}}</td>
        <td><span class="badge ${{badgeClass(c.outcome)}}">${{c.outcome || 'unknown'}}</span></td>
        <td>${{c.relevance_score ?? 0}}</td>
        <td>
          ${{c.pacer_fetch_needed ? '<span class="badge review">PACER needed</span>' : ''}}
          ${{c.disposition_confidence === 'needs_manual_review' ? '<span class="badge review">review</span>' : ''}}
          ${{c.similarity_to_own_case ? '<span class="badge mtd_denied">similar pattern</span>' : ''}}
        </td>
        <td><a href="${{c.source_url}}" target="_blank">view →</a></td>
      </tr>
    `).join('');
  }}

  function filterTable() {{
    const q = document.getElementById('searchBox').value.toLowerCase();
    const outcome = document.getElementById('outcomeFilter').value;
    const filtered = CASES.filter(c => {{
      const matchesText = !q || JSON.stringify(c).toLowerCase().includes(q);
      const matchesOutcome = !outcome || c.outcome === outcome;
      return matchesText && matchesOutcome;
    }});
    renderTable(filtered);
  }}

  function sortTable() {{
    const key = document.getElementById('sortBy').value;
    const sorted = [...CASES].sort((a, b) => (b[key] ?? -Infinity) - (a[key] ?? -Infinity));
    renderTable(sorted);
  }}

  renderTable(CASES);
</script>

</body>
</html>
"""
    OUTPUT_FILE.write_text(html, encoding="utf-8")
    print(f"Dashboard written to {OUTPUT_FILE}")


if __name__ == "__main__":
    build_dashboard()
