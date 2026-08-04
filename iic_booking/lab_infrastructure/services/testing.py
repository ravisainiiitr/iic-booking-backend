"""Seed and aggregate SAT / UAT catalog for the Phase 2.5 Test Dashboard."""

from __future__ import annotations

from django.db.models import Count, Q
from django.utils import timezone

from iic_booking.lab_infrastructure.models import SatTestCase, SatTestResult, SatTestRun

# Master catalog — mirrors docs/phase-2.5/SAT-Master-Test-Plan.md (keep in sync).
SAT_CATALOG: list[dict] = [
    # Deployment
    {"test_id": "SAT-DEP-001", "module": "Deployment", "feature": "Clean Windows DSA install from Deployment Center", "severity": "critical",
     "preconditions": "Clean Win10/11 VM; Main Admin; published DSA release with SHA-256",
     "steps": "1. Download DSA via ticket\n2. Verify SHA-256\n3. Install silently/wizard\n4. Service starts",
     "expected_result": "DSA service running; LocalApi listens; no manual config beyond enrollment secret"},
    {"test_id": "SAT-DEP-002", "module": "Deployment", "feature": "Equipment PC Wizard install + integrity", "severity": "critical",
     "preconditions": "Published Wizard release", "steps": "Download, SHA-256, install, launch", "expected_result": "Wizard launches; discovers DSA"},
    {"test_id": "SAT-DEP-003", "module": "Deployment", "feature": "RAA install + enrollment", "severity": "critical",
     "preconditions": "Published RAA release; enrollment key", "steps": "Install, enroll, heartbeat", "expected_result": "Workstation registered; heartbeat Online"},
    {"test_id": "SAT-DEP-004", "module": "Deployment Center", "feature": "SHA-256 / signature / compatibility matrix display", "severity": "high",
     "preconditions": "Release with checksum + min versions", "steps": "Open Deployment Center; inspect release card", "expected_result": "Checksum, notes, matrix visible"},
    {"test_id": "SAT-DEP-005", "module": "Deployment Center", "feature": "Repair package download", "severity": "high",
     "preconditions": "Repair package attached", "steps": "Download repair package; run on broken agent", "expected_result": "Agent recovers without full reinstall"},
    {"test_id": "SAT-DEP-006", "module": "Deployment Center", "feature": "Upgrade package preserves config", "severity": "high",
     "preconditions": "Prior agent version installed", "steps": "Upgrade; verify ProgramData retained", "expected_result": "Identity/token retained; version bumped"},
    # Commissioning
    {"test_id": "SAT-COM-001", "module": "Commissioning", "feature": "Full lab chain Portal→DSA→EqPC→RAA→Analysis PC", "severity": "critical",
     "preconditions": "Fresh department; equipment RA-enabled", "steps": "Enroll DSA; pair Wizard; link RAA; run diagnostics",
     "expected_result": "All nodes Online on Lab Infrastructure; config version set"},
    {"test_id": "SAT-COM-002", "module": "DSA", "feature": "Automatic discovery UDP/HTTP", "severity": "critical",
     "preconditions": "DSA on LAN", "steps": "Wizard discover", "expected_result": "DSA listed with correct IP/port"},
    {"test_id": "SAT-COM-003", "module": "DSA", "feature": "Pairing + announce + config pack", "severity": "critical",
     "preconditions": "ManagementApiKey set", "steps": "Issue pairing; announce; get config-pack; validate",
     "expected_result": "Registration validated; OTP not persisted in ConfigJson"},
    {"test_id": "SAT-COM-004", "module": "RAA", "feature": "Equipment binding + software inventory", "severity": "high",
     "preconditions": "Enrolled RAA", "steps": "Link equipment; inventory scan", "expected_result": "Equipment linked; inventory on portal"},
    {"test_id": "SAT-COM-005", "module": "Heartbeat", "feature": "DSA equipment_pcs rollup in Portal heartbeat", "severity": "critical",
     "preconditions": "EqPC reported status to DSA", "steps": "Wait heartbeat; GET lab infrastructure",
     "expected_result": "EqPC appears under DSA with live status"},
    {"test_id": "SAT-COM-006", "module": "Diagnostics", "feature": "Node diagnostics PASS/WARN/FAIL", "severity": "high",
     "preconditions": "Online node", "steps": "Run diagnostics from Lab UI", "expected_result": "Professional report JSON; no fleet-wide side effects"},
    # Booking E2E
    {"test_id": "SAT-BKG-001", "module": "Booking Workflow", "feature": "Internal user E2E booking→RA→complete", "severity": "critical",
     "preconditions": "Commissioned lab; internal user", "steps": "Create→approve→sample→raw→sync→RA→Guac→results→S3→email",
     "expected_result": "Booking Complete; emails sent; cleanup done"},
    {"test_id": "SAT-BKG-002", "module": "Booking Workflow", "feature": "Faculty E2E", "severity": "high",
     "preconditions": "Faculty account", "steps": "Same as BKG-001", "expected_result": "Passes with faculty RBAC"},
    {"test_id": "SAT-BKG-003", "module": "Booking Workflow", "feature": "External user E2E", "severity": "high",
     "preconditions": "Verified external org", "steps": "Same as BKG-001", "expected_result": "Passes charge/approval path"},
    {"test_id": "SAT-BKG-004", "module": "Booking Workflow", "feature": "Project user E2E", "severity": "medium",
     "preconditions": "Project wallet", "steps": "Same as BKG-001", "expected_result": "Passes project billing"},
    {"test_id": "SAT-BKG-005", "module": "Booking Workflow", "feature": "Startup user E2E", "severity": "medium",
     "preconditions": "Startup account", "steps": "Same as BKG-001", "expected_result": "Passes startup path"},
    {"test_id": "SAT-BKG-006", "module": "Synchronization", "feature": "DSA raw data sync after sample accept", "severity": "critical",
     "preconditions": "EqPC folders + share", "steps": "Drop raw file; observe DSA sync", "expected_result": "Portal sees files; SyncLog success"},
    # Remote Analysis
    {"test_id": "SAT-RA-001", "module": "Remote Analysis", "feature": "Session create + reverse tunnel + Guacamole", "severity": "critical",
     "preconditions": "Analysis PC Online", "steps": "Start session; open Guac", "expected_result": "Desktop reachable < SLA"},
    {"test_id": "SAT-RA-002", "module": "Remote Analysis", "feature": "Clipboard + file transfer", "severity": "high",
     "preconditions": "Active session", "steps": "Copy text; transfer file", "expected_result": "Both succeed per policy"},
    {"test_id": "SAT-RA-003", "module": "Remote Analysis", "feature": "Timeout / extension / maintenance / queue", "severity": "high",
     "preconditions": "Policies configured", "steps": "Exercise each mode", "expected_result": "Expected UX + audit events"},
    {"test_id": "SAT-RA-004", "module": "Remote Analysis", "feature": "Workspace cleanup + archive", "severity": "high",
     "preconditions": "Completed session", "steps": "End analysis; wait cleanup", "expected_result": "Workspace cleaned/archived"},
    {"test_id": "SAT-RA-005", "module": "Remote Analysis", "feature": "Concurrent sessions multi-PC", "severity": "high",
     "preconditions": "≥2 Analysis PCs", "steps": "Two sessions parallel", "expected_result": "No cross-talk; both healthy"},
    {"test_id": "SAT-RA-006", "module": "Remote Analysis", "feature": "Session recovery + no-show", "severity": "medium",
     "preconditions": "Policies set", "steps": "Kill agent mid-session; no-show booking", "expected_result": "Recovery/no-show handlers fire"},
    # DSA deep
    {"test_id": "SAT-DSA-001", "module": "DSA", "feature": "IP allocation + reservation", "severity": "high",
     "preconditions": "Soft IP pool", "steps": "Announce twice", "expected_result": "Preferred IP reused"},
    {"test_id": "SAT-DSA-002", "module": "Configuration Push", "feature": "Config push + ack + Applied status", "severity": "critical",
     "preconditions": "Profile bound", "steps": "Bump config; bootstrap; ack", "expected_result": "Ack Applied; dashboard shows Applied"},
    {"test_id": "SAT-DSA-003", "module": "Configuration Push", "feature": "Configuration rollback", "severity": "high",
     "preconditions": "≥2 versions", "steps": "Rollback; re-bootstrap", "expected_result": "Previous snapshot restored; version bumped"},
    {"test_id": "SAT-DSA-004", "module": "DSA", "feature": "Folder monitoring + repair + reconfigure", "severity": "high",
     "preconditions": "Validated EqPC", "steps": "Delete folder; Repair; RefreshConfiguration", "expected_result": "Folders restored; audit logged"},
    {"test_id": "SAT-DSA-005", "module": "Software Inventory", "feature": "Required vs installed compliance matrix", "severity": "medium",
     "preconditions": "Template required_software", "steps": "Open compliance API/UI", "expected_result": "Missing/Outdated flagged"},
    # Failure & recovery
    {"test_id": "SAT-FAIL-001", "module": "Failure Recovery", "feature": "Stop DSA / disconnect LAN", "severity": "critical",
     "preconditions": "Healthy lab", "steps": "Stop DSA 5m; restore", "expected_result": "Alerts; auto reconnect; no data corruption"},
    {"test_id": "SAT-FAIL-002", "module": "Failure Recovery", "feature": "Stop RAA / delete ProgramData subset", "severity": "high",
     "preconditions": "Healthy RAA", "steps": "Stop; delete cache; restart/reinstall", "expected_result": "Re-enroll or recover per design"},
    {"test_id": "SAT-FAIL-003", "module": "Failure Recovery", "feature": "Stop reverse tunnel / Guacamole", "severity": "critical",
     "preconditions": "Active path", "steps": "Stop gateway; restart", "expected_result": "Sessions fail safely; recover after restart"},
    {"test_id": "SAT-FAIL-004", "module": "Failure Recovery", "feature": "Disk full / missing result folder", "severity": "high",
     "preconditions": "Writable volumes", "steps": "Fill disk; delete results", "expected_result": "Alerts; diagnostics FAIL; repair path"},
    {"test_id": "SAT-FAIL-005", "module": "Failure Recovery", "feature": "Restart Portal + Database", "severity": "critical",
     "preconditions": "Staging", "steps": "Restart services mid-sync", "expected_result": "Agents reconnect; no orphan critical state"},
    # Fleet / Lab UI
    {"test_id": "SAT-FLT-001", "module": "Fleet Dashboard", "feature": "Lab Infrastructure tree statuses", "severity": "critical",
     "preconditions": "Mixed online/offline nodes", "steps": "Open /laboratory-infrastructure; poll 30s", "expected_result": "Correct enums; auto-refresh"},
    {"test_id": "SAT-FLT-002", "module": "Fleet Dashboard", "feature": "Health score + node detail fields", "severity": "high",
     "preconditions": "Heartbeat enriched", "steps": "Open node detail", "expected_result": "CPU/RAM/disk/versions present"},
    {"test_id": "SAT-FLT-003", "module": "Repair", "feature": "Repair / RestartAgent / RescanSoftware actions", "severity": "high",
     "preconditions": "Manage permission", "steps": "Invoke each action", "expected_result": "Queued/Sent; audit; restart applies"},
    {"test_id": "SAT-FLT-004", "module": "Reporting", "feature": "Utilization CSV export", "severity": "medium",
     "preconditions": "Usage data", "steps": "GET utilization report", "expected_result": "CSV/JSON downloadable"},
    {"test_id": "SAT-FLT-005", "module": "Notifications", "feature": "Critical alert email", "severity": "medium",
     "preconditions": "Email stack configured", "steps": "Trigger offline detector", "expected_result": "Alert + email"},
    # Security / RBAC / API / DB / FE
    {"test_id": "SAT-SEC-001", "module": "Security", "feature": "Pairing fail-closed without ManagementApiKey", "severity": "critical",
     "preconditions": "Unset key", "steps": "POST pairing token", "expected_result": "403"},
    {"test_id": "SAT-SEC-002", "module": "Role Based Access", "feature": "Lab + Test Dashboard Main Admin only", "severity": "critical",
     "preconditions": "Non-admin user", "steps": "Navigate protected routes", "expected_result": "Denied / redirected"},
    {"test_id": "SAT-SEC-003", "module": "Security", "feature": "Agent auth + config integrity + credential storage", "severity": "critical",
     "preconditions": "Agents enrolled", "steps": "Replay token; inspect secrets on disk", "expected_result": "No plaintext secrets; invalid auth rejected"},
    {"test_id": "SAT-SEC-004", "module": "Security", "feature": "Status ingest not forgeable without pairing/mgmt key", "severity": "high",
     "preconditions": "DSA up", "steps": "POST status without token (non-loopback)", "expected_result": "401/403"},
    {"test_id": "SAT-API-001", "module": "API", "feature": "Lab aggregate APIs authz + validation", "severity": "high",
     "preconditions": "Swagger/OpenAPI", "steps": "Call with/without auth; bad payloads", "expected_result": "401/403/400 as expected"},
    {"test_id": "SAT-API-002", "module": "API", "feature": "Config ack idempotent upsert", "severity": "medium",
     "preconditions": "DSA agent token", "steps": "POST ack twice", "expected_result": "Single logical ack row; 201"},
    {"test_id": "SAT-DB-001", "module": "Database", "feature": "Phase1/2 migrations apply + rollback smoke", "severity": "high",
     "preconditions": "Staging DB backup", "steps": "migrate; migrate reverse where safe", "expected_result": "No integrity errors"},
    {"test_id": "SAT-DB-002", "module": "Database", "feature": "Indexes/FKs on lab_infrastructure tables", "severity": "medium",
     "preconditions": "Migrations applied", "steps": "Inspect schema", "expected_result": "Indexes present; cascades correct"},
    {"test_id": "SAT-FE-001", "module": "Frontend", "feature": "Lab Infrastructure UX states", "severity": "medium",
     "preconditions": "Admin browser", "steps": "Loading/empty/error/filter", "expected_result": "Indicators and empty states correct"},
    {"test_id": "SAT-FE-002", "module": "Frontend", "feature": "Deployment Center + Test Dashboard responsive", "severity": "low",
     "preconditions": "Desktop + mobile widths", "steps": "Resize", "expected_result": "Usable layout"},
    # Performance placeholders (executed under PERF plan)
    {"test_id": "SAT-PERF-001", "module": "Performance", "feature": "Lab infrastructure API p95", "severity": "medium",
     "preconditions": "≥50 nodes", "steps": "Load test GET /lab/infrastructure/", "expected_result": "p95 < 2s staging baseline"},
    {"test_id": "SAT-PERF-002", "module": "Performance", "feature": "Heartbeat + concurrent sync", "severity": "medium",
     "preconditions": "Multi-agent", "steps": "Burst heartbeats + syncs", "expected_result": "No dropped critical updates"},
]


def ensure_catalog(*, suite: str = SatTestCase.Suite.SAT) -> int:
    created = 0
    for row in SAT_CATALOG:
        _, was_created = SatTestCase.objects.update_or_create(
            test_id=row["test_id"],
            defaults={
                "suite": suite if row["test_id"].startswith("SAT-") else suite,
                "module": row["module"],
                "feature": row["feature"],
                "preconditions": row.get("preconditions", ""),
                "steps": row.get("steps", ""),
                "expected_result": row.get("expected_result", ""),
                "severity": row.get("severity", SatTestCase.Severity.HIGH),
                "is_active": True,
            },
        )
        if was_created:
            created += 1
    try:
        from iic_booking.lab_infrastructure.services.sat_execution import apply_execution_plan

        apply_execution_plan()
    except Exception:
        pass
    return created


def module_summary(run: SatTestRun | None = None) -> list[dict]:
    ensure_catalog()
    cases = SatTestCase.objects.filter(is_active=True)
    modules = (
        cases.values("module")
        .annotate(total=Count("id"))
        .order_by("module")
    )
    latest = run or SatTestRun.objects.order_by("-started_at").first()
    out = []
    for m in modules:
        name = m["module"]
        total = m["total"]
        passed = failed = skipped = not_run = 0
        last_exec = None
        if latest:
            qs = SatTestResult.objects.filter(run=latest, test_case__module=name)
            agg = qs.aggregate(
                passed=Count("id", filter=Q(status=SatTestResult.Status.PASSED)),
                failed=Count("id", filter=Q(status=SatTestResult.Status.FAILED)),
                skipped=Count("id", filter=Q(status__in=[SatTestResult.Status.SKIPPED, SatTestResult.Status.BLOCKED])),
                not_run=Count("id", filter=Q(status=SatTestResult.Status.NOT_RUN)),
            )
            passed = agg["passed"] or 0
            failed = agg["failed"] or 0
            skipped = agg["skipped"] or 0
            not_run = agg["not_run"] or 0
            last = qs.exclude(executed_at=None).order_by("-executed_at").first()
            last_exec = last.executed_at.isoformat() if last and last.executed_at else None
            # Cases without a result row count as not_run
            covered = qs.count()
            if covered < total:
                not_run += total - covered
        else:
            not_run = total
        coverage = round(100.0 * (passed + failed + skipped) / total, 1) if total else 0.0
        health = "healthy" if failed == 0 and passed > 0 else ("degraded" if failed else "unknown")
        out.append(
            {
                "module": name,
                "total_tests": total,
                "passed": passed,
                "failed": failed,
                "skipped": skipped,
                "not_run": not_run,
                "last_execution": last_exec,
                "coverage_pct": coverage,
                "health": health,
            }
        )
    return out


def overall_health(modules: list[dict]) -> dict:
    total = sum(m["total_tests"] for m in modules)
    passed = sum(m["passed"] for m in modules)
    failed = sum(m["failed"] for m in modules)
    skipped = sum(m["skipped"] for m in modules)
    not_run = sum(m["not_run"] for m in modules)
    if failed:
        status = "failing"
    elif not_run == total:
        status = "not_started"
    elif passed + skipped >= total and failed == 0:
        status = "passing"
    else:
        status = "in_progress"
    return {
        "status": status,
        "total_tests": total,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "not_run": not_run,
        "coverage_pct": round(100.0 * (passed + failed + skipped) / total, 1) if total else 0.0,
        "as_of": timezone.now().isoformat(),
    }


def start_run(*, user, name: str = "", suite: str = "", lab_context: dict | None = None) -> SatTestRun:
    ensure_catalog()
    run = SatTestRun.objects.create(
        name=name or f"Lab SAT {timezone.now().strftime('%Y-%m-%d %H:%M')}",
        suite=suite or SatTestCase.Suite.SAT,
        executed_by=user if getattr(user, "pk", None) else None,
        status=SatTestRun.Status.RUNNING,
        lab_context=lab_context or {},
    )
    bulk = [
        SatTestResult(run=run, test_case=tc, status=SatTestResult.Status.NOT_RUN)
        for tc in SatTestCase.objects.filter(is_active=True).order_by("stage", "execution_order", "test_id")
    ]
    SatTestResult.objects.bulk_create(bulk, batch_size=200)
    first = (
        SatTestResult.objects.filter(run=run, status=SatTestResult.Status.NOT_RUN)
        .order_by("test_case__stage", "test_case__execution_order")
        .first()
    )
    if first:
        run.current_result = first
        run.save(update_fields=["current_result"])
    return run
