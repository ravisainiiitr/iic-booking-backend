"""HTML pages for Phase 4 Live Commissioning + Fault Injection (admin Toolkit)."""

from __future__ import annotations

import json


def render_live_commissioning_html(payload: dict) -> str:
    data_json = json.dumps(payload, default=str).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Live Commissioning</title>
<meta http-equiv="refresh" content="30"/>
<style>
:root {{ --bg:#0f1419; --card:#1a2332; --line:#2d3a4d; --text:#e7ecf3; --muted:#9aa8bc;
  --ok:#3dd68c; --bad:#f07178; --warn:#e6b450; --accent:#59c2ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:1rem 1.25rem; border-bottom:1px solid var(--line); display:flex; gap:1rem; flex-wrap:wrap; align-items:center; }}
header h1 {{ margin:0; font-size:1.2rem; }}
header a {{ color:var(--accent); text-decoration:none; font-size:.85rem; }}
.overall {{ font-weight:700; padding:.25rem .6rem; border-radius:6px; }}
.overall.GREEN {{ background:#143528; color:var(--ok); }}
.overall.AMBER {{ background:#3a2e14; color:var(--warn); }}
.overall.RED {{ background:#3a1a1d; color:var(--bad); }}
main {{ padding:1rem 1.25rem 2rem; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:.75rem; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.9rem; border-left:4px solid var(--line); }}
.card.GREEN {{ border-left-color:var(--ok); }}
.card.AMBER {{ border-left-color:var(--warn); }}
.card.RED {{ border-left-color:var(--bad); }}
.card h2 {{ margin:0 0 .35rem; font-size:.95rem; display:flex; justify-content:space-between; gap:.5rem; }}
.status {{ font-size:.75rem; font-weight:700; }}
.detail {{ color:var(--muted); font-size:.8rem; margin:.35rem 0; }}
pre {{ background:#0b1016; border:1px solid var(--line); border-radius:6px; padding:.5rem; font-size:.7rem; overflow:auto; max-height:120px; }}
.timeline {{ margin-top:1.25rem; }}
.timeline table {{ width:100%; border-collapse:collapse; font-size:.8rem; }}
th,td {{ border-bottom:1px solid var(--line); padding:.35rem; text-align:left; }}
th {{ color:var(--muted); }}
.actions {{ display:flex; gap:.5rem; flex-wrap:wrap; margin:.75rem 0; }}
button {{ background:#132033; border:1px solid var(--accent); color:var(--text); padding:.4rem .75rem; border-radius:6px; cursor:pointer; }}
input {{ background:#0b1016; border:1px solid var(--line); color:var(--text); padding:.35rem .5rem; border-radius:6px; }}
</style></head><body>
<header>
  <h1>Live Commissioning</h1>
  <span class="overall" id="overall">…</span>
  <span id="clock" style="color:var(--muted);font-size:.8rem"></span>
  <a href="/api/v1/analysis/operations/toolkit/?view=html">Toolkit</a>
  <a href="/api/v1/analysis/operations/toolkit/faults/?view=html">Fault injection</a>
  <a href="/api/v1/analysis/operations/commissioning/?view=html">Commissioning console</a>
</header>
<main>
  <div class="actions">
    <button onclick="refreshLive()">Refresh</button>
    <input id="wsId" placeholder="workstation_id (optional)" style="min-width:220px"/>
    <input id="runId" placeholder="commissioning_run_id" style="min-width:220px"/>
    <input id="bookingId" placeholder="booking_id" style="width:120px"/>
    <button onclick="loadTimeline()">Load timeline</button>
    <button onclick="downloadEvidence()">Evidence ZIP</button>
  </div>
  <div class="grid" id="cards"></div>
  <section class="timeline card" style="margin-top:1rem;border-left-color:var(--accent)">
    <h2>Live Session Timeline</h2>
    <table><thead><tr>
      <th>Timestamp</th><th>Event</th><th>Duration</th><th>Booking</th><th>Workstation</th><th>Tunnel</th><th>Job</th>
    </tr></thead><tbody id="tlRows"></tbody></table>
  </section>
  <section class="card" style="margin-top:1rem">
    <h2>Live Log Tail (portal)</h2>
    <pre id="logTail">…</pre>
  </section>
</main>
<script>
const INITIAL = {data_json};
const BASE = "/api/v1/analysis/operations/toolkit";
const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";

function esc(s) {{
  return String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
}}

function render(data) {{
  document.getElementById("overall").textContent = data.overall || "—";
  document.getElementById("overall").className = "overall " + (data.overall || "");
  document.getElementById("clock").textContent = data.generated_at || "";
  document.getElementById("cards").innerHTML = (data.cards || []).map(c => `
    <div class="card ${{esc(c.status)}}">
      <h2><span>${{esc(c.name)}}</span><span class="status">${{esc(c.status)}}</span></h2>
      <div class="detail">${{esc(c.detail)}}</div>
      <pre>${{esc(JSON.stringify(c.metrics || {{}}, null, 2))}}</pre>
    </div>`).join("");
}}

async function api(path, opts={{}}) {{
  const res = await fetch(BASE + path, {{
    credentials: "same-origin",
    headers: {{ "Accept": "application/json", "Content-Type": "application/json", "X-CSRFToken": csrftoken, ...(opts.headers||{{}}) }},
    ...opts,
  }});
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
  return data;
}}

async function refreshLive() {{
  const ws = document.getElementById("wsId").value.trim();
  const q = ws ? ("?workstation_id=" + encodeURIComponent(ws)) : "";
  const data = await api("/live/" + q);
  render(data);
}}

async function loadTimeline() {{
  const q = new URLSearchParams();
  const run = document.getElementById("runId").value.trim();
  const booking = document.getElementById("bookingId").value.trim();
  if (run) q.set("run_id", run);
  if (booking) q.set("booking_id", booking);
  const data = await api("/live/timeline/?" + q.toString());
  document.getElementById("tlRows").innerHTML = (data.events || []).map(e => `
    <tr>
      <td class="mono">${{esc(e.timestamp)}}</td>
      <td>${{esc(e.event)}}</td>
      <td>${{e.duration_ms ?? "—"}}</td>
      <td>${{esc(e.booking_id)}}</td>
      <td>${{esc(e.workstation_id)}}</td>
      <td>${{esc(e.tunnel_id)}}</td>
      <td>${{esc(e.analysis_job_id)}}</td>
    </tr>`).join("") || `<tr><td colspan="7">No events</td></tr>`;
}}

async function downloadEvidence() {{
  const run = document.getElementById("runId").value.trim();
  if (!run) {{ alert("Set commissioning_run_id first (POST /toolkit/runs/ to start)"); return; }}
  window.location = BASE + "/runs/" + encodeURIComponent(run) + "/evidence/";
}}

async function loadLogs() {{
  try {{
    const data = await api("/logs/?limit=40");
    document.getElementById("logTail").textContent = (data.entries || []).map(e =>
      `${{e.created_at}}  ${{(e.severity||"").toUpperCase()}}  ${{e.source}}  ${{e.action||""}}  ${{e.details||""}}`
    ).join("\\n") || "(empty)";
  }} catch (e) {{ document.getElementById("logTail").textContent = String(e); }}
}}

render(INITIAL);
loadLogs();
setInterval(() => {{ refreshLive().catch(()=>{{}}); loadLogs(); }}, 30000);
</script>
</body></html>"""


def render_fault_injection_html(payload: dict) -> str:
    data_json = json.dumps(payload, default=str).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Fault Injection — Commissioning</title>
<style>
:root {{ --bg:#0f1419; --card:#1a2332; --line:#2d3a4d; --text:#e7ecf3; --muted:#9aa8bc; --bad:#f07178; --accent:#59c2ff; --warn:#e6b450; }}
body {{ margin:0; font-family:Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:1rem 1.25rem; border-bottom:1px solid var(--line); display:flex; gap:1rem; align-items:center; flex-wrap:wrap; }}
header h1 {{ margin:0; font-size:1.15rem; }}
header a {{ color:var(--accent); text-decoration:none; font-size:.85rem; }}
main {{ padding:1rem 1.25rem; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:1rem; margin-bottom:1rem; }}
.warn {{ color:var(--warn); font-size:.85rem; }}
table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
th,td {{ border-bottom:1px solid var(--line); padding:.4rem; text-align:left; }}
button {{ background:#3a1a1d; border:1px solid var(--bad); color:var(--text); padding:.35rem .7rem; border-radius:6px; cursor:pointer; }}
input {{ background:#0b1016; border:1px solid var(--line); color:var(--text); padding:.35rem .5rem; border-radius:6px; margin-right:.35rem; }}
pre {{ background:#0b1016; border:1px solid var(--line); padding:.75rem; border-radius:6px; overflow:auto; font-size:.8rem; }}
</style></head><body>
<header>
  <h1>Fault Injection (admin)</h1>
  <a href="/api/v1/analysis/operations/toolkit/live/?view=html">Live Commissioning</a>
  <a href="/api/v1/analysis/operations/toolkit/?view=html">Toolkit</a>
</header>
<main>
  <p class="warn">Use only during commissioning. Prefer dry-run first. Host-level restarts are recorded as hints — execute them on the infrastructure host.</p>
  <div class="card">
    <label>workstation_id <input id="ws" style="min-width:260px"/></label>
    <label>booking_id <input id="booking" style="width:100px"/></label>
    <label>run_id <input id="run" style="min-width:260px"/></label>
  </div>
  <div class="card">
    <h2>Catalog</h2>
    <table><thead><tr><th>Fault</th><th>Requires</th><th>Action</th></tr></thead>
    <tbody id="rows"></tbody></table>
  </div>
  <div class="card"><h2>Result</h2><pre id="out">Select a fault…</pre></div>
  <div class="card"><h2>Recovery checklist</h2><pre id="rec"></pre></div>
</main>
<script>
const INITIAL = {data_json};
const BASE = "/api/v1/analysis/operations/toolkit";
const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";

document.getElementById("rows").innerHTML = (INITIAL.faults || []).map(f => `
  <tr>
    <td><strong>${{f.label}}</strong><div style="color:var(--muted);font-size:.75rem">${{f.description}}</div><code>${{f.id}}</code></td>
    <td>${{(f.requires||[]).join(", ") || "—"}}</td>
    <td>
      <button onclick="inject('${{f.id}}', true)">Dry-run</button>
      <button onclick="inject('${{f.id}}', false)">Inject</button>
    </td>
  </tr>`).join("");
document.getElementById("rec").textContent = JSON.stringify(INITIAL.recovery || {{}}, null, 2);

async function inject(faultId, dry) {{
  const body = {{
    fault_id: faultId,
    dry_run: !!dry,
    workstation_id: document.getElementById("ws").value.trim() || null,
    booking_id: document.getElementById("booking").value.trim() ? Number(document.getElementById("booking").value.trim()) : null,
    run_id: document.getElementById("run").value.trim() || null,
  }};
  if (!dry && !confirm("Inject fault " + faultId + "?")) return;
  const res = await fetch(BASE + "/faults/inject/", {{
    method: "POST",
    credentials: "same-origin",
    headers: {{ "Content-Type": "application/json", "X-CSRFToken": csrftoken, "Accept": "application/json" }},
    body: JSON.stringify(body),
  }});
  const data = await res.json().catch(() => ({{}}));
  document.getElementById("out").textContent = JSON.stringify(data, null, 2);
  if (data.commissioning_run_id) document.getElementById("run").value = data.commissioning_run_id;
}}
</script>
</body></html>"""
