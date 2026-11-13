"""Headless Department Sync Agent observability (Main Administrator fleet view).

DSAs run on department PCs with no monitor and nobody watching. Every heartbeat carries a ``details.diagnostics``
block (DSA 1.0.7+) and the Equipment PC rollup; this module

- keeps the latest copy per agent in :class:`AgentDiagnosticState` (heartbeat rows stay small),
- turns it into a health state (healthy / warning / critical / offline) with plain-language reasons,
- opens and resolves incidents (``AlertEvent`` rows, source ``headless``) that form each agent's timeline,
- tells the Main Administrators (email + bell) about incidents that persist past a grace period, at most once per
  incident plus a reminder every ``DSA_ALERT_REPEAT_HOURS`` (debounced: short blips never notify),
- trims heartbeat history, old bundles and old incidents (retention).

Thresholds are settings with safe defaults (see ``_setting``).
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from html import escape
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import AgentDiagnosticState
from iic_booking.sync.models import AgentHealthState
from iic_booking.sync.models import AgentHeartbeat
from iic_booking.sync.models import AgentLifecycleStatus
from iic_booking.sync.models import AgentSupportBundle
from iic_booking.sync.models import AlertEvent
from iic_booking.sync.models import AlertLifecycleStatus
from iic_booking.sync.models import AlertSeverity
from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.models import SyncLogCategory
from iic_booking.sync.models import SyncLogSeverity
from iic_booking.sync.services.logging import write_sync_log

logger = logging.getLogger(__name__)

SOURCE = "headless"
SKIPPED_STATUSES = [
    AgentLifecycleStatus.REVOKED,
    AgentLifecycleStatus.RETIRED,
    AgentLifecycleStatus.DELETED,
    AgentLifecycleStatus.DISABLED,
]
EMAIL_TEMPLATE = "dsa_headless_alert_admin"

DEFAULTS: dict[str, Any] = {
    "DSA_ALERT_NOTIFY": True,
    "DSA_ALERT_GRACE_MINUTES": 10,
    "DSA_ALERT_REPEAT_HOURS": 12,
    "DSA_EQPC_OFFLINE_MINUTES": 10,
    "DSA_UPLOAD_STUCK_MINUTES": 60,
    "DSA_DISK_WARN_PERCENT": 20.0,
    "DSA_DISK_CRITICAL_PERCENT": 10.0,
    "DSA_SQLITE_WARN_BYTES": 2 * 1024**3,
    "DSA_ERROR_SPIKE_PER_HOUR": 20,
    "DSA_CLOCK_SKEW_SECONDS": 120,
    "DSA_TOKEN_EXPIRY_WARN_HOURS": 72,
    "DSA_HEARTBEAT_RETENTION_DAYS": 30,
    "DSA_SUPPORT_BUNDLE_RETENTION_DAYS": 30,
    "DSA_INCIDENT_RETENTION_DAYS": 180,
    "DSA_SUPPORT_BUNDLE_MAX_BYTES": 3 * 1024 * 1024,
}

# Incidents worth an email/bell. Everything else is shown on the dashboard only.
NOTIFY_CODES = {
    "DSA_OFFLINE",
    "EQPC_OFFLINE",
    "UPLOADS_STUCK",
    "DISK_LOW",
    "ERROR_SPIKE",
    "EQUIPMENT_NETWORK_DOWN",
    "TOKEN_INVALID",
}

ACTION_HINTS = {
    "DSA_OFFLINE": "Check the department PC is on and connected; if it is, ask someone to restart it.",
    "EQPC_OFFLINE": "Check the Equipment PC is on and its cable to the Equipment Network is connected.",
    "EQPC_SHARE": "Run 'Refresh Equipment PCs' from the dashboard; re-run the Equipment PC wizard if it persists.",
    "UPLOADS_STUCK": "Run 'Retry failed uploads'; collect a support bundle if it does not clear.",
    "UPLOADS_FAILED": "Run 'Retry failed uploads'.",
    "DISK_LOW": "Free space on the department PC's data drive.",
    "SQLITE_LARGE": "Collect a support bundle and plan maintenance of the agent database.",
    "ERROR_SPIKE": "Open the agent's recent errors; collect a support bundle.",
    "EQUIPMENT_NETWORK_DOWN": "Check the Equipment Network adapter and switch on the department PC.",
    "EQUIPMENT_NETWORK_IP": "The Equipment Network adapter's IP differs from the configured address.",
    "TOKEN_INVALID": "The agent cannot authenticate; it needs re-enrollment from the portal.",
    "TOKEN_EXPIRING": "Renew the agent's enrollment before the token expires.",
    "CLOCK_SKEW": "Fix the department PC's clock (time sync); bookings and uploads depend on it.",
    "NO_DIAGNOSTICS": "Update the agent to 1.0.7 or later for full remote diagnostics.",
    "AGENT_DISABLED": "The agent is disabled on the portal.",
}


def _setting(name: str):
    return getattr(settings, name, DEFAULTS[name])


def _dt(raw):
    if raw is None:
        return None
    if hasattr(raw, "isoformat"):
        return raw if timezone.is_aware(raw) else timezone.make_aware(raw)
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def _num(value):
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _section(diag: dict, name: str) -> dict:
    value = (diag or {}).get(name)
    return value if isinstance(value, dict) and "error" not in value else {}


# --------------------------------------------------------------------------------------------------------------
# Ingest (called from HeartbeatService inside the heartbeat transaction)
# --------------------------------------------------------------------------------------------------------------


def is_probe(payload: dict) -> bool:
    """A bare heartbeat without the agent's details: the DSA's portal connectivity probe (or a minimal client)."""
    # HeartbeatRequestSerializer defaults details to {}, so test for content, not presence.
    return payload.get("status_message") == "connectivity_probe" or not payload.get("details")


def _sync_running_version(agent: DepartmentSyncAgent, diagnostics: dict) -> None:
    """The heartbeat's ``service_version`` is the version recorded at enrollment (stale after an upgrade); the
    diagnostics block carries the running build. Strip the ``+commit`` suffix."""
    service = diagnostics.get("service") if isinstance(diagnostics.get("service"), dict) else {}
    running = str(service.get("version") or "").split("+", 1)[0].strip()[:50]
    if running and running != agent.version:
        agent.version = running
        agent.save(update_fields=["version", "updated_at"])


def ingest_heartbeat(agent: DepartmentSyncAgent, payload: dict, details: dict, *, now=None) -> AgentDiagnosticState:
    """Store the latest diagnostics for the agent and re-evaluate its health. Mutates ``details``: the bulky
    ``diagnostics`` block is moved out so per-heartbeat rows stay small."""
    now = now or timezone.now()
    diagnostics = details.pop("diagnostics", None)
    state, _ = AgentDiagnosticState.objects.get_or_create(sync_agent=agent)
    state.received_at = now
    if is_probe(payload):
        # The DSA's connectivity probe posts a bare heartbeat (no details, no clock). It proves the agent is alive
        # but must not wipe the diagnostics / clock skew the full heartbeat delivered seconds earlier.
        state.save(update_fields=["received_at", "updated_at"])
        evaluate_and_sync(agent, state=state, now=now)
        return state
    sent_at = _dt(payload.get("sent_at"))
    if sent_at:
        state.agent_sent_at = sent_at
        state.clock_skew_seconds = round((sent_at - now).total_seconds(), 1)
    if isinstance(diagnostics, dict):
        state.diagnostics = diagnostics
        _sync_running_version(agent, diagnostics)
    elif diagnostics is None and state.diagnostics:
        # An agent that stopped sending diagnostics (downgrade) must not keep showing stale data as current.
        state.diagnostics = {**state.diagnostics, "stale": True}
    pcs = payload.get("equipment_pcs")
    if isinstance(pcs, list):
        state.equipment_pcs = pcs[:200]
    caps = details.get("capabilities")
    if isinstance(caps, dict):
        state.capabilities = caps
    state.save()
    evaluate_and_sync(agent, state=state, now=now)
    return state


# --------------------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------------------


def _reason(code: str, severity: str, message: str, subject: str = "") -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "subject": subject,
        "action": ACTION_HINTS.get(code, ""),
    }


def evaluate(agent: DepartmentSyncAgent, state: AgentDiagnosticState | None, *, now=None) -> tuple[str, list[dict]]:
    """Pure function of the stored state: (health, reasons)."""
    now = now or timezone.now()
    reasons: list[dict] = []
    if agent.status in (AgentLifecycleStatus.DISABLED, AgentLifecycleStatus.REVOKED) or not agent.is_active:
        reasons.append(_reason("AGENT_DISABLED", "info", f"Agent is {agent.status.lower()} on the portal."))

    last = agent.last_heartbeat_at
    if last is None:
        return AgentHealthState.UNKNOWN, reasons + [
            _reason("DSA_OFFLINE", "info", "The agent has never sent a heartbeat."),
        ]
    timeout = heartbeat_timeout_seconds()
    silent = (now - last).total_seconds()
    offline = silent > timeout
    if offline:
        mins = int(silent // 60)
        reasons.append(
            _reason("DSA_OFFLINE", "critical", f"No heartbeat for {mins} min (last {timezone.localtime(last):%d %b %H:%M})."),
        )

    diag = (state.diagnostics if state else None) or {}
    if not diag or diag.get("stale"):
        reasons.append(_reason("NO_DIAGNOSTICS", "info", "This agent version does not report remote diagnostics."))
    else:
        reasons.extend(_diagnostic_reasons(diag, state, now))

    for pc in (state.equipment_pcs if state else None) or []:
        reasons.extend(_equipment_pc_reasons(pc, now))

    if offline:
        return AgentHealthState.OFFLINE, reasons
    severities = {r["severity"] for r in reasons}
    if "critical" in severities:
        return AgentHealthState.CRITICAL, reasons
    if "warning" in severities:
        return AgentHealthState.WARNING, reasons
    return AgentHealthState.HEALTHY, reasons


def _diagnostic_reasons(diag: dict, state: AgentDiagnosticState | None, now) -> list[dict]:
    out: list[dict] = []

    enrollment = _section(diag, "enrollment")
    if enrollment:
        if enrollment.get("token_valid") is False:
            out.append(_reason("TOKEN_INVALID", "critical", "The agent's portal token is not valid."))
        else:
            hours = _num(enrollment.get("token_expires_in_hours"))
            if hours is not None and hours < _setting("DSA_TOKEN_EXPIRY_WARN_HOURS"):
                out.append(_reason("TOKEN_EXPIRING", "warning", f"Portal token expires in {hours:.0f} h."))

    storage = _section(diag, "storage")
    free_pct = _num(storage.get("data_drive_free_percent"))
    if free_pct is not None:
        drive = storage.get("data_drive") or "data drive"
        if free_pct <= _setting("DSA_DISK_CRITICAL_PERCENT"):
            out.append(_reason("DISK_LOW", "critical", f"{drive} has {free_pct:.1f}% free."))
        elif free_pct <= _setting("DSA_DISK_WARN_PERCENT"):
            out.append(_reason("DISK_LOW", "warning", f"{drive} has {free_pct:.1f}% free."))
    sqlite_bytes = _num(storage.get("sqlite_bytes"))
    if sqlite_bytes is not None and sqlite_bytes >= _setting("DSA_SQLITE_WARN_BYTES"):
        out.append(_reason("SQLITE_LARGE", "warning", f"Agent database is {sqlite_bytes / 1024**3:.1f} GB."))

    net = _section(diag, "equipment_network")
    if net.get("enabled"):
        if net.get("adapter_found") is False or net.get("adapter_up") is False:
            out.append(
                _reason("EQUIPMENT_NETWORK_DOWN", "critical", "Equipment Network adapter is missing or disconnected."),
            )
        elif net.get("ip_matches_config") is False:
            out.append(
                _reason(
                    "EQUIPMENT_NETWORK_IP",
                    "warning",
                    f"Equipment Network adapter has {net.get('adapter_ip') or 'no IP'}, "
                    f"configured {net.get('configured_ip')}.",
                ),
            )

    uploads = _section(diag, "uploads")
    age = _num(uploads.get("oldest_pending_age_minutes"))
    if age is not None and age >= _setting("DSA_UPLOAD_STUCK_MINUTES"):
        out.append(
            _reason(
                "UPLOADS_STUCK",
                "critical",
                f"{uploads.get('pending')} upload(s) waiting; oldest for {int(age)} min. "
                f"Last error: {uploads.get('last_error') or 'none'}",
            ),
        )
    failed = int(_num(uploads.get("failed")) or 0) + int(_num(uploads.get("detected_files_failed")) or 0)
    if failed > 0:
        out.append(_reason("UPLOADS_FAILED", "warning", f"{failed} upload(s) failed permanently."))

    problems = _section(diag, "problems")
    errors_1h = int(_num(problems.get("errors_1h")) or 0)
    if errors_1h >= _setting("DSA_ERROR_SPIKE_PER_HOUR"):
        out.append(_reason("ERROR_SPIKE", "warning", f"{errors_1h} errors logged in the last hour."))

    skew = state.clock_skew_seconds if state else None
    if skew is not None and abs(skew) > _setting("DSA_CLOCK_SKEW_SECONDS"):
        out.append(
            _reason("CLOCK_SKEW", "warning", f"Agent clock is {abs(skew):.0f} s {'ahead' if skew > 0 else 'behind'}."),
        )
    return out


def equipment_pc_online(pc: dict, now) -> tuple[bool, object]:
    """(online, last heartbeat) of an Equipment PC from heartbeat freshness only.

    The DSA's stored registration status can say "Online" while the PC is unplugged (live test 13 Nov 2026), so it is
    never used. DSA 1.0.7+ sends its own verdict (online, two missed 30 s heartbeats); older agents are judged
    here from the newest of last_seen / last_status_at against DSA_EQPC_OFFLINE_MINUTES.
    """
    stamps = [_dt(pc.get(k)) for k in ("last_heartbeat_at", "last_seen", "last_status_at")]
    stamps = [t for t in stamps if t is not None]
    last = max(stamps) if stamps else None
    if isinstance(pc.get("online"), bool):
        return pc["online"], last
    limit = timedelta(minutes=_setting("DSA_EQPC_OFFLINE_MINUTES"))
    return bool(last is not None and now - last <= limit), last


def _equipment_pc_reasons(pc: dict, now) -> list[dict]:
    if not isinstance(pc, dict):
        return []
    status = str(pc.get("registration_status") or "").lower()
    if status in ("unassigned", "retired", "removed"):
        return []
    name = str(pc.get("hostname") or pc.get("equipment_id") or pc.get("id") or "Equipment PC")[:80]
    out = []
    online, last = equipment_pc_online(pc, now)
    if not online:
        when = f"last heartbeat {timezone.localtime(last):%d %b %H:%M}" if last else "no heartbeat yet"
        out.append(_reason("EQPC_OFFLINE", "warning", f"Equipment PC {name} is offline ({when}).", subject=name))
    elif pc.get("share_ok") is False:
        out.append(
            _reason("EQPC_SHARE", "warning", f"Equipment PC {name}: results share not reachable.", subject=name),
        )
    return out


# --------------------------------------------------------------------------------------------------------------
# Incidents (AlertEvent rows with details.source = "headless")
# --------------------------------------------------------------------------------------------------------------


def _fingerprint(agent_id, code: str, subject: str) -> str:
    raw = f"{SOURCE}|{agent_id}|{code}|{subject}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


_SEVERITY = {"critical": AlertSeverity.CRITICAL, "warning": AlertSeverity.WARNING, "info": AlertSeverity.INFO}


_OPEN = [AlertLifecycleStatus.NEW, AlertLifecycleStatus.ACKNOWLEDGED, AlertLifecycleStatus.SUPPRESSED]


def open_incidents_all():
    return AlertEvent.objects.filter(details__source=SOURCE, status__in=_OPEN)


def open_incidents(agent: DepartmentSyncAgent):
    return open_incidents_all().filter(sync_agent=agent)


@transaction.atomic
def evaluate_and_sync(agent: DepartmentSyncAgent, *, state: AgentDiagnosticState | None = None, now=None) -> dict:
    """Evaluate the agent, store the result, open new incidents and resolve cleared ones."""
    now = now or timezone.now()
    if state is None:
        state = AgentDiagnosticState.objects.filter(sync_agent=agent).first()
    health, reasons = evaluate(agent, state, now=now)
    if state is None:
        state = AgentDiagnosticState.objects.create(sync_agent=agent)
    state.health = health
    state.reasons = reasons
    state.evaluated_at = now
    state.save(update_fields=["health", "reasons", "evaluated_at", "updated_at"])

    current = {
        _fingerprint(agent.id, r["code"], r.get("subject") or ""): r
        for r in reasons
        if r["severity"] in ("warning", "critical")
    }
    existing = {a.fingerprint: a for a in open_incidents(agent).select_for_update()}
    opened, resolved = 0, 0
    for fp, r in current.items():
        alert = existing.get(fp)
        if alert is None:
            AlertEvent.objects.create(
                correlation_id=uuid.uuid4(),
                department=agent.department,
                building=getattr(agent, "building", None),
                sync_agent=agent,
                rule_code=r["code"][:64],
                category="dsa_headless",
                severity=_SEVERITY[r["severity"]],
                status=AlertLifecycleStatus.NEW,
                title=(f"{r['code'].replace('_', ' ').title()}" + (f": {r['subject']}" if r.get("subject") else ""))[
                    :200
                ],
                message=r["message"][:1000],
                fingerprint=fp,
                details={"source": SOURCE, "subject": r.get("subject") or "", "opened_at": now.isoformat()},
                expires_at=None,
            )
            opened += 1
        else:
            changed = []
            if alert.message != r["message"][:1000]:
                alert.message = r["message"][:1000]
                changed.append("message")
            sev = _SEVERITY[r["severity"]]
            if alert.severity != sev:
                alert.severity = sev
                changed.append("severity")
            if changed:
                alert.save(update_fields=[*changed, "updated_at"])
    for fp, alert in existing.items():
        if fp not in current:
            alert.status = AlertLifecycleStatus.RESOLVED
            alert.resolved_at = now
            alert.resolved_by = "system"
            alert.resolution = "Condition cleared (reported by the agent)."
            alert.save(update_fields=["status", "resolved_at", "resolved_by", "resolution", "updated_at"])
            resolved += 1
    if opened or resolved:
        write_sync_log(
            event_code="DSA-HEALTH",
            category=SyncLogCategory.MONITORING,
            severity=SyncLogSeverity.WARNING if health in ("critical", "offline") else SyncLogSeverity.INFO,
            message=f"Agent health {health}: {opened} incident(s) opened, {resolved} resolved",
            sync_agent=agent,
            json_payload={"health": health, "opened": opened, "resolved": resolved},
        )
    return {"health": health, "opened": opened, "resolved": resolved}


# --------------------------------------------------------------------------------------------------------------
# Notifications (debounced)
# --------------------------------------------------------------------------------------------------------------


def main_admins() -> list:
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    return list(User.objects.filter(user_type=UserType.ADMIN, is_active=True))


def due_incidents(agent: DepartmentSyncAgent, now=None) -> list[AlertEvent]:
    """Open incidents that have lasted past the grace period and were not told (or told long enough ago)."""
    now = now or timezone.now()
    grace = timedelta(minutes=_setting("DSA_ALERT_GRACE_MINUTES"))
    repeat = timedelta(hours=_setting("DSA_ALERT_REPEAT_HOURS"))
    due = []
    for alert in open_incidents(agent).filter(rule_code__in=NOTIFY_CODES):
        if alert.status == AlertLifecycleStatus.SUPPRESSED:
            continue
        if now - alert.created_at < grace:
            continue
        told = _dt((alert.details or {}).get("notified_at"))
        if told is None or now - told >= repeat:
            due.append(alert)
    return due


def _link(agent) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/department-sync?tab=fleet&agent={agent.id}"


def notify_due(agent: DepartmentSyncAgent, now=None) -> int:
    """One email + bell per agent listing the incidents now due. Returns the number of incidents told."""
    now = now or timezone.now()
    if not _setting("DSA_ALERT_NOTIFY"):
        return 0
    due = due_incidents(agent, now)
    recovered = list(
        AlertEvent.objects.filter(
            sync_agent=agent,
            details__source=SOURCE,
            status=AlertLifecycleStatus.RESOLVED,
            details__notified_at__isnull=False,
            details__recovery_told__isnull=True,
            resolved_at__gte=now - timedelta(days=2),
        ),
    )
    if not due and not recovered:
        return 0
    users = main_admins()
    if not users:
        logger.warning("dsa.headless no Main Administrator to notify agent=%s", agent.id)
        return 0

    from iic_booking.communication.in_app import notify_in_app

    agent_label = agent.agent_name or agent.machine_name or str(agent.id)
    dept = agent.department.name if agent.department_id else "-"
    link_path = f"/department-sync?tab=fleet&agent={agent.id}"
    if due:
        worst = "critical" if any(a.severity == AlertSeverity.CRITICAL for a in due) else "warning"
        title = f"Sync agent {agent_label} ({dept}): {due[0].title}" + (
            f" and {len(due) - 1} more" if len(due) > 1 else ""
        )
        items_text = "\n".join(f"- {a.title}: {a.message} {ACTION_HINTS.get(a.rule_code, '')}" for a in due)
        items_html = "".join(
            f"<li><b>{escape(a.title)}</b>: {escape(a.message)} <i>{escape(ACTION_HINTS.get(a.rule_code, ''))}</i></li>"
            for a in due
        )
        _email(
            users,
            agent,
            context={
                "agent_name": agent_label,
                "department_name": dept,
                "alert_title": due[0].title if len(due) == 1 else f"{len(due)} problems",
                "level_label": "Critical" if worst == "critical" else "Warning",
                "items_text": items_text,
                "items_html": f"<ul>{items_html}</ul>",
                "time": timezone.localtime(now).strftime("%d %b %Y %H:%M"),
                "link": _link(agent),
            },
        )
        notify_in_app(
            users,
            title=title[:250],
            message="; ".join(a.message for a in due)[:500],
            link=link_path,
            notification_type="error" if worst == "critical" else "warning",
            event="dsa_headless_alert",
            extra={"agent_id": str(agent.id)},
        )
        for a in due:
            details = dict(a.details or {})
            details["notified_at"] = now.isoformat()
            details["notify_count"] = int(details.get("notify_count") or 0) + 1
            a.details = details
            a.save(update_fields=["details", "updated_at"])
    if recovered:
        notify_in_app(
            users,
            title=f"Sync agent {agent_label} ({dept}): recovered",
            message="; ".join(f"{a.title} cleared" for a in recovered)[:500],
            link=link_path,
            notification_type="success",
            event="dsa_headless_recovered",
            extra={"agent_id": str(agent.id)},
        )
        for a in recovered:
            a.details = {**(a.details or {}), "recovery_told": now.isoformat()}
            a.save(update_fields=["details", "updated_at"])
    return len(due)


def _email(users, agent, *, context: dict) -> None:
    from iic_booking.communication.service import CommunicationService

    for user in users:
        if not user.email:
            continue
        ctx = {"recipient_name": user.get_full_name() if hasattr(user, "get_full_name") else "", **context}
        try:
            CommunicationService.send_email(
                recipient=user,
                template=EMAIL_TEMPLATE,
                template_context=ctx,
                metadata={"sync_agent_id": str(agent.id), "kind": "dsa_headless_alert"},
            )
        except Exception:
            logger.exception("dsa.headless email failed agent=%s user=%s", agent.id, user.pk)


# --------------------------------------------------------------------------------------------------------------
# Periodic run (Celery beat every 5 minutes) + retention
# --------------------------------------------------------------------------------------------------------------


def run_all(now=None) -> dict:
    now = now or timezone.now()
    agents = DepartmentSyncAgent.objects.select_related("department").exclude(
        status__in=SKIPPED_STATUSES,
    ).filter(is_active=True)
    summary = {"agents": 0, "opened": 0, "resolved": 0, "notified": 0}
    for agent in agents:
        try:
            result = evaluate_and_sync(agent, now=now)
            summary["agents"] += 1
            summary["opened"] += result["opened"]
            summary["resolved"] += result["resolved"]
            if agent.status != AgentLifecycleStatus.MAINTENANCE:
                summary["notified"] += notify_due(agent, now)
        except Exception:
            logger.exception("dsa.headless evaluation failed agent=%s", agent.id)
    summary["retention"] = apply_retention(now)
    return summary


def apply_retention(now=None, *, batch: int = 5000) -> dict:
    """Bounded deletes per run so a large backlog is trimmed gradually."""
    now = now or timezone.now()
    out = {}
    hb_cutoff = now - timedelta(days=_setting("DSA_HEARTBEAT_RETENTION_DAYS"))
    ids = list(AgentHeartbeat.objects.filter(reported_at__lt=hb_cutoff).values_list("id", flat=True)[:batch])
    out["heartbeats"] = AgentHeartbeat.objects.filter(id__in=ids).delete()[0] if ids else 0

    b_cutoff = now - timedelta(days=_setting("DSA_SUPPORT_BUNDLE_RETENTION_DAYS"))
    out["bundles"] = AgentSupportBundle.objects.filter(created_at__lt=b_cutoff).delete()[0]

    i_cutoff = now - timedelta(days=_setting("DSA_INCIDENT_RETENTION_DAYS"))
    ids = list(
        AlertEvent.objects.filter(
            details__source=SOURCE, status=AlertLifecycleStatus.RESOLVED, resolved_at__lt=i_cutoff,
        ).values_list("id", flat=True)[:batch],
    )
    out["incidents"] = AlertEvent.objects.filter(id__in=ids).delete()[0] if ids else 0
    return out
