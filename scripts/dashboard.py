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
OUTPUT_FILE = DATA_DIR / "dashboard.html"


def load(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def build_dashboard():
    cases = load(CASES_FILE, [])
    issues = load(ISSUES_FILE, [])
    run_log = load(RUN_LOG_FILE, [])

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
