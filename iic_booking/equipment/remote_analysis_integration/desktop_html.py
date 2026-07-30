"""HTML launcher for browser remote desktop (Phase 3)."""

from __future__ import annotations

import json

from django.utils.html import escape


def render_desktop_launcher_html(payload: dict) -> str:
    data_json = json.dumps(payload, default=str).replace("<", "\\u003c")
    booking_id = escape(str(payload.get("booking_id") or ""))
    can = bool(payload.get("show_launch_button"))
    elig = payload.get("eligibility") or {}
    guac = payload.get("guacamole") or {}
    reason = escape(str(elig.get("reason") or ""))
    guac_status = escape(str(guac.get("status") or "unknown"))

    launch_btn = (
        '<button class="action" id="btnLaunch" onclick="doLaunch()">Launch Remote Analysis</button>'
        if can
        else '<button class="action" id="btnLaunch" disabled title="Not eligible">Launch Remote Analysis</button>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Remote Analysis Desktop — Booking {booking_id}</title>
<style>
:root {{ --bg:#0f1419; --card:#1a2332; --text:#e7ecf3; --muted:#9aa7b8; --accent:#3d8bfd; --ok:#3dd68c; --bad:#f07178; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Segoe UI, system-ui, sans-serif; background:var(--bg); color:var(--text); }}
header {{ padding:1.25rem 1.5rem; border-bottom:1px solid #2a3544; }}
h1 {{ margin:0; font-size:1.25rem; }}
main {{ max-width:720px; margin:1.5rem auto; padding:0 1rem; }}
.card {{ background:var(--card); border-radius:10px; padding:1.25rem; }}
.hint {{ color:var(--muted); font-size:.9rem; }}
.action {{ background:var(--accent); color:#fff; border:0; border-radius:8px; padding:.65rem 1.1rem; font-size:1rem; cursor:pointer; }}
.action:disabled {{ opacity:.45; cursor:not-allowed; }}
.pill {{ display:inline-block; padding:.15rem .5rem; border-radius:999px; font-size:.75rem; background:#2a3544; }}
.pill.ok {{ background:#1e3d32; color:var(--ok); }}
.pill.bad {{ background:#3d2226; color:var(--bad); }}
.flash {{ margin:1rem 0; min-height:1.2rem; }}
.flash.ok {{ color:var(--ok); }}
.flash.err {{ color:var(--bad); }}
pre {{ background:#0b1016; padding:1rem; border-radius:8px; overflow:auto; font-size:.8rem; }}
a {{ color:var(--accent); }}
</style>
</head>
<body>
<header>
  <h1>Remote Analysis Desktop</h1>
  <p class="hint">Booking {booking_id} · Guacamole redirect launcher (Portal-authorized)</p>
</header>
<main>
  <div class="card">
    <p>Eligibility: <span class="pill {'ok' if elig.get('eligible') else 'bad'}">{escape(str(elig.get('eligible')))}</span> {reason}</p>
    <p>Guacamole: <span class="pill {'ok' if guac.get('ok') else 'bad'}">{guac_status}</span></p>
    <p class="hint">The launch button appears only when the booking is eligible, a workspace and workstation are assigned, and Guacamole is ready (or mock mode is on).</p>
    {launch_btn}
    <button class="action" style="background:#2a3544;margin-left:.5rem" onclick="location.reload()">Refresh</button>
    <div class="flash" id="flash"></div>
    <pre id="box"></pre>
  </div>
</main>
<script>
const INITIAL = {data_json};
const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";
const LAUNCH_API = INITIAL.launch_api || "";

function flash(msg, ok) {{
  const el = document.getElementById("flash");
  el.textContent = msg || "";
  el.className = "flash " + (ok === false ? "err" : ok ? "ok" : "");
}}

async function doLaunch() {{
  const btn = document.getElementById("btnLaunch");
  if (btn) btn.disabled = true;
  flash("Starting session…", true);
  try {{
    const res = await fetch(LAUNCH_API, {{
      method: "POST",
      credentials: "same-origin",
      headers: {{
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CSRFToken": csrftoken,
      }},
      body: JSON.stringify({{}}),
    }});
    const data = await res.json().catch(() => ({{}}));
    document.getElementById("box").textContent = JSON.stringify(data, null, 2);
    if (!res.ok) throw new Error(data.detail || ("HTTP " + res.status));
    const url = data.launch_url;
    if (!url) throw new Error(data.detail || "Session not ready for launch yet — refresh and retry");
    flash("Redirecting to remote desktop…", true);
    // launch_url hits Portal connect (consumes one-time token) which redirects to Guacamole
    window.location.href = url + (url.includes("?") ? "&" : "?") + "redirect=1";
  }} catch (e) {{
    flash(String(e), false);
    if (btn && INITIAL.show_launch_button) btn.disabled = false;
  }}
}}

document.getElementById("box").textContent = JSON.stringify({{
  eligibility: INITIAL.eligibility,
  reservation: INITIAL.reservation,
  workspace: INITIAL.workspace,
  session: INITIAL.session,
  guacamole: INITIAL.guacamole,
  can_launch: INITIAL.can_launch,
}}, null, 2);
</script>
</body>
</html>
"""
