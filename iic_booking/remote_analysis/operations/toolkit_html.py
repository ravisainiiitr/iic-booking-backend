"""HTML shell for the Commissioning & Diagnostics Toolkit."""

from __future__ import annotations

import json

from django.utils.html import escape


def render_toolkit_html(payload: dict) -> str:
    data_json = json.dumps(payload, default=str).replace("<", "\\u003c")
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>RA Commissioning Toolkit</title>
<style>
:root {{ --bg:#0f1419; --card:#1a2332; --line:#2d3a4d; --text:#e7ecf3; --muted:#9aa8bc; --ok:#3dd68c; --bad:#f07178; --warn:#e6b450; --accent:#59c2ff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Segoe UI,system-ui,sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:1rem 1.25rem; border-bottom:1px solid var(--line); display:flex; gap:1rem; flex-wrap:wrap; align-items:center; }}
header h1 {{ margin:0; font-size:1.15rem; font-weight:600; }}
header a {{ color:var(--accent); text-decoration:none; font-size:.85rem; }}
.tabs {{ display:flex; gap:.35rem; flex-wrap:wrap; padding:.75rem 1.25rem; border-bottom:1px solid var(--line); }}
.tab {{ background:transparent; border:1px solid var(--line); color:var(--muted); padding:.4rem .75rem; border-radius:6px; cursor:pointer; font-size:.85rem; }}
.tab.active {{ color:var(--text); border-color:var(--accent); background:#132033; }}
main {{ padding:1rem 1.25rem 2rem; }}
.panel {{ display:none; }}
.panel.active {{ display:block; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(160px,1fr)); gap:.75rem; margin-bottom:1rem; }}
.stat {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.75rem; }}
.stat .v {{ font-size:1.35rem; font-weight:600; }}
.stat .l {{ color:var(--muted); font-size:.75rem; margin-top:.2rem; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:8px; padding:1rem; margin-bottom:1rem; }}
.card h2 {{ margin:0 0 .75rem; font-size:1rem; }}
table {{ width:100%; border-collapse:collapse; font-size:.85rem; }}
th,td {{ border-bottom:1px solid var(--line); padding:.4rem .35rem; text-align:left; vertical-align:top; }}
th {{ color:var(--muted); font-weight:500; }}
.pill {{ display:inline-block; padding:.1rem .45rem; border-radius:999px; font-size:.7rem; font-weight:600; }}
.pill.ok {{ background:#143528; color:var(--ok); }}
.pill.bad {{ background:#3a1a1d; color:var(--bad); }}
.pill.warn {{ background:#3a2e14; color:var(--warn); }}
.pill.neu {{ background:#243044; color:var(--muted); }}
button.action {{ background:var(--accent); color:#062033; border:0; border-radius:6px; padding:.5rem .9rem; font-weight:600; cursor:pointer; margin:.25rem .35rem .25rem 0; }}
button.action.secondary {{ background:#243044; color:var(--text); }}
button.action:disabled {{ opacity:.5; cursor:wait; }}
.flash {{ min-height:1.2rem; font-size:.85rem; margin:.5rem 0; }}
.flash.ok {{ color:var(--ok); }}
.flash.err {{ color:var(--bad); }}
input,select {{ background:#0f1419; border:1px solid var(--line); color:var(--text); border-radius:6px; padding:.4rem .55rem; margin:.2rem .35rem .2rem 0; }}
pre.log {{ background:#0b1016; border:1px solid var(--line); border-radius:6px; padding:.75rem; max-height:420px; overflow:auto; font-size:.75rem; white-space:pre-wrap; }}
.mono {{ font-family:ui-monospace,Consolas,monospace; font-size:.8rem; }}
.hint {{ color:var(--muted); font-size:.8rem; }}
</style></head><body>
<header>
  <h1>Remote Analysis — Commissioning &amp; Diagnostics Toolkit</h1>
  <span class="hint" id="clock"></span>
  <a href="/api/v1/analysis/operations/commissioning/?view=html">Commissioning console</a>
  <a href="/api/v1/analysis/operations/toolkit/live/?view=html">Live Commissioning</a>
  <a href="/api/v1/analysis/operations/toolkit/faults/?view=html">Fault injection</a>
  <a href="/api/v1/analysis/operations/diagnostics/?view=html">Legacy diagnostics</a>
</header>
<nav class="tabs" id="tabs">
  <button class="tab active" data-tab="overview">Overview</button>
  <button class="tab" data-tab="agent">Agent</button>
  <button class="tab" data-tab="connect">Connectivity</button>
  <button class="tab" data-tab="guac">Guacamole</button>
  <button class="tab" data-tab="tunnel">Reverse Tunnel</button>
  <button class="tab" data-tab="logs">Logs</button>
  <button class="tab" data-tab="health">Health report</button>
  <button class="tab" data-tab="selftest">Self-test</button>
  <button class="tab" data-tab="report">Commissioning report</button>
  <button class="tab" data-tab="monitor">Monitoring</button>
</nav>
<main>
  <div class="flash" id="flash"></div>

  <section class="panel active" id="panel-overview">
    <div class="grid" id="overviewStats"></div>
    <div class="card"><h2>Workstations</h2>
      <div style="overflow:auto"><table><thead><tr>
        <th>Host</th><th>Online</th><th>Status</th><th>HB age</th><th>Health</th><th>Command</th><th>Workspace</th>
      </tr></thead><tbody id="wsRows"></tbody></table></div>
    </div>
    <div class="card"><h2>Infra</h2><pre class="log mono" id="infraBox"></pre></div>
  </section>

  <section class="panel" id="panel-agent">
    <div class="card">
      <h2>Agent diagnostics</h2>
      <select id="agentSelect"></select>
      <button class="action" onclick="loadAgent()">Refresh agent</button>
      <pre class="log mono" id="agentBox">Select a workstation…</pre>
    </div>
  </section>

  <section class="panel" id="panel-connect">
    <div class="card">
      <h2>Connectivity tests</h2>
      <p class="hint">Portal API · Auth · DB · Redis · Storage · Guacamole · Heartbeat · Workspace · Upload · Download · Cleanup</p>
      <select id="connectWs"></select>
      <button class="action" id="btnConnect" onclick="runConnect()">Run connectivity suite</button>
      <p class="hint mono" id="connectRunMeta"></p>
      <pre class="log mono" id="connectBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-guac">
    <div class="card">
      <h2>Guacamole diagnostics</h2>
      <p class="hint">Connectivity, active sessions, tunnel health, API latency (does not alter production workflows).</p>
      <button class="action" onclick="loadGuac()">Refresh Guacamole status</button>
      <pre class="log mono" id="guacBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-tunnel">
    <div class="card">
      <h2>Reverse Tunnel Gateway</h2>
      <p class="hint">Transport mode, gateway health, active tunnels, bandwidth counters.</p>
      <button class="action" onclick="loadTunnel()">Refresh tunnel dashboard</button>
      <pre class="log mono" id="tunnelBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-logs">
    <div class="card">
      <h2>Log viewer (portal)</h2>
      <input id="logWs" placeholder="workstation_id"/>
      <input id="logWorkspace" placeholder="workspace_id"/>
      <input id="logBooking" placeholder="booking_id"/>
      <input id="logQ" placeholder="search text"/>
      <select id="logSev"><option value="">any severity</option><option value="error">error</option><option value="info">info</option></select>
      <button class="action" onclick="loadLogs()">Search</button>
      <button class="action secondary" onclick="downloadLogs()">Download JSON</button>
      <pre class="log mono" id="logBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-health">
    <div class="card">
      <h2>Health report</h2>
      <button class="action" onclick="loadHealth()">Generate health report</button>
      <div class="grid" id="healthGrid"></div>
      <pre class="log mono" id="healthBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-selftest">
    <div class="card">
      <h2>Full Remote Analysis self-test</h2>
      <p class="hint">Creates a disposable workspace, uploads/downloads a probe file, verifies checksum, writes dummy Processed output, cleans up. Does not change production booking workflows.</p>
      <select id="selfWs"></select>
      <button class="action" id="btnSelf" onclick="runSelfTest()">Run Full Self Test</button>
      <p class="hint mono" id="selfRunMeta"></p>
      <pre class="log mono" id="selfBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-report">
    <div class="card">
      <h2>Commissioning report</h2>
      <button class="action" onclick="downloadReport(false)">JSON report</button>
      <button class="action" onclick="downloadReport(true)">PDF report (+ self-test)</button>
      <pre class="log mono" id="reportBox"></pre>
    </div>
  </section>

  <section class="panel" id="panel-monitor">
    <div class="card">
      <h2>Production monitoring recommendations</h2>
      <pre class="log mono" id="monBox"></pre>
    </div>
  </section>
</main>
<script>
const INITIAL = {data_json};
const BASE = "/api/v1/analysis/operations/toolkit";
const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
let lastLogs = null;

function flash(msg, ok) {{
  const el = document.getElementById("flash");
  el.textContent = msg || "";
  el.className = "flash " + (ok === false ? "err" : ok ? "ok" : "");
}}

function pill(text, kind) {{
  return `<span class="pill ${{kind || "neu"}}">${{escapeHtml(text)}}</span>`;
}}

function escapeHtml(s) {{
  return String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
}}

document.querySelectorAll(".tab").forEach(btn => {{
  btn.onclick = () => {{
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("panel-" + btn.dataset.tab).classList.add("active");
  }};
}});

function fillWsSelects(list) {{
  const opts = (list || []).map(w =>
    `<option value="${{w.id}}">${{escapeHtml(w.hostname)}} · ${{escapeHtml(w.status)}} · health ${{w.health_score}}</option>`
  ).join("") || `<option value="">No workstations</option>`;
  ["agentSelect","connectWs","selfWs"].forEach(id => {{
    const el = document.getElementById(id);
    const prev = el.value;
    el.innerHTML = opts;
    if (prev) el.value = prev;
  }});
}}

function renderOverview(data) {{
  const o = data.overview || {{}};
  document.getElementById("clock").textContent = "Updated " + (data.generated_at || "");
  document.getElementById("overviewStats").innerHTML = [
    ["Online", o.workstations_online, "ok"],
    ["Offline", o.workstations_offline, o.workstations_offline ? "warn" : "ok"],
    ["Pending cmds", o.pending_commands, "neu"],
    ["Running WS", o.running_workspaces, "neu"],
    ["Failed WS", o.failed_workspaces, o.failed_workspaces ? "bad" : "ok"],
    ["Retries Σ", o.retry_count_sum, "neu"],
    ["Uploads", o.active_uploads, "neu"],
    ["Downloads", o.active_downloads, "neu"],
    ["DB ms", o.database_latency_ms, "neu"],
  ].map(([l,v,k]) => `<div class="stat"><div class="v">${{pill(String(v ?? "—"), k)}}</div><div class="l">${{l}}</div></div>`).join("");

  document.getElementById("wsRows").innerHTML = (data.workstations || []).map(w => `
    <tr>
      <td>${{escapeHtml(w.hostname)}}<div class="mono">${{escapeHtml(w.agent_id)}}</div></td>
      <td>${{w.online ? pill("ONLINE","ok") : pill("OFFLINE","bad")}}</td>
      <td>${{escapeHtml(w.status)}}</td>
      <td class="mono">${{w.heartbeat_age_seconds ?? "—"}}</td>
      <td>${{w.health_score}}</td>
      <td class="mono">${{w.current_command ? escapeHtml(w.current_command.command_type + " / " + w.current_command.status) : "—"}}</td>
      <td class="mono">${{w.current_workspace ? escapeHtml((w.current_workspace.sync_phase || "") + " " + String(w.current_workspace.id).slice(0,8)) : "—"}}</td>
    </tr>`).join("") || `<tr><td colspan="7">No workstations</td></tr>`;

  document.getElementById("infraBox").textContent = JSON.stringify({{
    database: o.database,
    redis: o.redis,
    storage: o.storage,
    guacamole: o.guacamole || data.guacamole,
  }}, null, 2);
  fillWsSelects(data.workstations);
  if (data.guacamole) {{
    document.getElementById("guacBox").textContent = JSON.stringify(data.guacamole, null, 2);
  }}
}}

async function loadGuac() {{
  try {{
    const data = await api("/dashboard/");
    document.getElementById("guacBox").textContent = JSON.stringify(data.guacamole || data.overview?.guacamole || {{}}, null, 2);
    flash("Guacamole status refreshed", true);
  }} catch (e) {{ flash(String(e), false); }}
}}

async function loadTunnel() {{
  try {{
    const data = await api("/dashboard/");
    document.getElementById("tunnelBox").textContent = JSON.stringify(data.reverse_tunnel || data.overview?.reverse_tunnel || {{}}, null, 2);
    flash("Tunnel dashboard refreshed", true);
  }} catch (e) {{ flash(String(e), false); }}
}}

async function api(path, opts={{}}) {{
  const res = await fetch(BASE + path, {{
    credentials: "same-origin",
    headers: {{
      "Accept": "application/json",
      "Content-Type": "application/json",
      "X-CSRFToken": csrftoken,
      ...(opts.headers || {{}}),
    }},
    ...opts,
  }});
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
  return data;
}}

async function loadAgent() {{
  const id = document.getElementById("agentSelect").value;
  try {{
    const data = await api("/agent/" + (id ? ("?workstation_id=" + encodeURIComponent(id)) : ""));
    document.getElementById("agentBox").textContent = JSON.stringify(data, null, 2);
  }} catch (e) {{ flash(String(e), false); }}
}}

function showRunMeta(elId, data) {{
  const el = document.getElementById(elId);
  if (!el) return;
  if (!data || !data.commissioning_run_id) {{ el.textContent = ""; return; }}
  const url = data.evidence_url || (BASE + "/runs/" + data.commissioning_run_id + "/evidence/");
  el.innerHTML = "Run ID: <code>" + escapeHtml(data.commissioning_run_id) + "</code> · "
    + "<a href=\\"" + escapeHtml(url) + "\\">Download evidence ZIP</a>";
}}

async function runConnect() {{
  const btn = document.getElementById("btnConnect");
  btn.disabled = true;
  try {{
    const id = document.getElementById("connectWs").value;
    const data = await api("/connectivity/", {{
      method: "POST",
      body: JSON.stringify({{ workstation_id: id || null }}),
    }});
    document.getElementById("connectBox").textContent = JSON.stringify(data, null, 2);
    showRunMeta("connectRunMeta", data);
    flash("Connectivity: " + data.overall, data.overall === "PASS");
  }} catch (e) {{ flash(String(e), false); }}
  finally {{ btn.disabled = false; }}
}}

async function loadLogs() {{
  const q = new URLSearchParams();
  const ws = document.getElementById("logWs").value.trim();
  const workspace = document.getElementById("logWorkspace").value.trim();
  const booking = document.getElementById("logBooking").value.trim();
  const search = document.getElementById("logQ").value.trim();
  const sev = document.getElementById("logSev").value;
  if (ws) q.set("workstation_id", ws);
  if (workspace) q.set("workspace_id", workspace);
  if (booking) q.set("booking_id", booking);
  if (search) q.set("q", search);
  if (sev) q.set("severity", sev);
  try {{
    lastLogs = await api("/logs/?" + q.toString());
    document.getElementById("logBox").textContent = (lastLogs.entries || []).map(e =>
      `${{e.created_at}}  ${{(e.severity||"").toUpperCase().padEnd(5)}}  ${{e.source}}  ${{e.action || ""}}  ${{e.details || ""}}`
    ).join("\\n") || "(no entries)";
  }} catch (e) {{ flash(String(e), false); }}
}}

function downloadLogs() {{
  if (!lastLogs) return;
  const blob = new Blob([JSON.stringify(lastLogs, null, 2)], {{ type: "application/json" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "ra-ops-logs.json";
  a.click();
}}

async function loadHealth() {{
  try {{
    const data = await api("/health-report/");
    const comps = data.components || {{}};
    document.getElementById("healthGrid").innerHTML = Object.entries(comps).map(([k,v]) => {{
      const kind = v === "GREEN" ? "ok" : v === "RED" ? "bad" : "warn";
      return `<div class="stat"><div class="v">${{pill(v, kind)}}</div><div class="l">${{escapeHtml(k)}}</div></div>`;
    }}).join("");
    document.getElementById("healthBox").textContent = JSON.stringify(data, null, 2);
    flash("Overall: " + data.overall, data.overall === "GREEN");
  }} catch (e) {{ flash(String(e), false); }}
}}

async function runSelfTest() {{
  const btn = document.getElementById("btnSelf");
  btn.disabled = true;
  flash("Running self-test…", true);
  try {{
    const id = document.getElementById("selfWs").value;
    const data = await api("/self-test/", {{
      method: "POST",
      body: JSON.stringify({{ workstation_id: id || null }}),
    }});
    document.getElementById("selfBox").textContent = JSON.stringify(data, null, 2);
    showRunMeta("selfRunMeta", data);
    flash("Self-test: " + data.overall, data.overall === "PASS");
  }} catch (e) {{ flash(String(e), false); }}
  finally {{ btn.disabled = false; }}
}}

async function downloadReport(withSelfTest) {{
  try {{
    if (withSelfTest) {{
      const id = document.getElementById("selfWs").value;
      const url = BASE + "/report/?format=pdf&self_test=1" + (id ? ("&workstation_id=" + encodeURIComponent(id)) : "");
      // PDF via POST to include CSRF + body
      const res = await fetch(BASE + "/report/?export=pdf", {{
        method: "POST",
        credentials: "same-origin",
        headers: {{ "Content-Type": "application/json", "X-CSRFToken": csrftoken }},
        body: JSON.stringify({{ workstation_id: id || null, export: "pdf" }}),
      }});
      if (!res.ok) throw new Error("PDF failed " + res.status);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "ra-commissioning-report.pdf";
      a.click();
      flash("PDF downloaded", true);
    }} else {{
      const data = await api("/report/");
      document.getElementById("reportBox").textContent = JSON.stringify(data, null, 2);
      const blob = new Blob([JSON.stringify(data, null, 2)], {{ type: "application/json" }});
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "ra-commissioning-report.json";
      a.click();
    }}
  }} catch (e) {{ flash(String(e), false); }}
}}

async function loadMonitoring() {{
  try {{
    const data = await api("/monitoring/");
    document.getElementById("monBox").textContent = JSON.stringify(data.recommendations || data, null, 2);
  }} catch (e) {{ flash(String(e), false); }}
}}

renderOverview(INITIAL);
loadMonitoring();
</script>
</body></html>"""
