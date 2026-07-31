"""HTTP views for the Commissioning & Diagnostics Toolkit (admin-only)."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings as django_settings
from django.contrib.auth import login as django_login
from django.http import HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token as get_csrf_token
from django.urls import reverse
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from iic_booking.remote_analysis.operations.commissioning import QueryParamTokenAuthentication
from iic_booking.remote_analysis.operations.toolkit import (
    MONITORING_RECOMMENDATIONS,
    build_agent_diagnostics,
    build_commissioning_report_payload,
    build_health_report,
    build_toolkit_dashboard,
    query_ops_logs,
    render_commissioning_report_pdf,
    run_connectivity_tests,
    run_full_self_test,
)
from iic_booking.remote_analysis.operations.toolkit_html import render_toolkit_html
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]
_AUTH = [TokenAuthenticationWithInactivity, SessionAuthentication, QueryParamTokenAuthentication]


def _wants_html(request) -> bool:
    if (request.query_params.get("view") or "").lower() == "html":
        return True
    accept = (request.META.get("HTTP_ACCEPT") or "").lower()
    return "text/html" in accept and "application/json" not in accept.split(",")[0]


def _login_redirect(request) -> HttpResponseRedirect:
    next_url = request.build_absolute_uri()
    parts = urlsplit(next_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "token"]
    next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    frontend = (getattr(django_settings, "FRONTEND_URL", "") or "").rstrip("/")
    if frontend:
        return HttpResponseRedirect(f"{frontend}/login?{urlencode({'next': next_url})}")
    try:
        login_path = reverse(django_settings.LOGIN_URL)
    except Exception:  # noqa: BLE001
        login_path = "/accounts/login/"
    return HttpResponseRedirect(f"{login_path}?{urlencode({'next': request.get_full_path()})}")


class ToolkitConsoleView(APIView):
    """Unified commissioning & diagnostics toolkit HTML/JSON shell."""

    authentication_classes = _AUTH
    permission_classes = _MANAGE

    def handle_exception(self, exc):
        if isinstance(exc, NotAuthenticated) and _wants_html(self.request):
            return _login_redirect(self.request)
        if isinstance(exc, PermissionDenied) and _wants_html(self.request):
            return HttpResponse("<h1>Forbidden</h1>", status=403, content_type="text/html; charset=utf-8")
        return super().handle_exception(exc)

    def get(self, request, *args, **kwargs):
        if _wants_html(request) and request.query_params.get("token") and request.user.is_authenticated:
            django_login(request, request.user, backend="django.contrib.auth.backends.ModelBackend")
            parts = urlsplit(request.build_absolute_uri())
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "token"]
            if not any(k == "view" for k, _ in query):
                query.append(("view", "html"))
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            return HttpResponseRedirect(target)

        payload = build_toolkit_dashboard()
        if _wants_html(request):
            get_csrf_token(request)
            return HttpResponse(render_toolkit_html(payload), content_type="text/html; charset=utf-8")
        return Response(payload)


toolkit_console = ToolkitConsoleView.as_view()


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_dashboard(request):
    return Response(build_toolkit_dashboard())


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_agent(request):
    return Response(
        build_agent_diagnostics(
            workstation_id=request.query_params.get("workstation_id"),
            agent_id=request.query_params.get("agent_id"),
        )
    )


@api_view(["POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_connectivity(request):
    return Response(
        run_connectivity_tests(
            actor=request.user,
            workstation_id=request.data.get("workstation_id") or request.query_params.get("workstation_id"),
            commissioning_run_id=request.data.get("commissioning_run_id")
            or request.query_params.get("commissioning_run_id"),
        )
    )


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_logs(request):
    return Response(
        query_ops_logs(
            workspace_id=request.query_params.get("workspace_id"),
            booking_id=request.query_params.get("booking_id"),
            workstation_id=request.query_params.get("workstation_id"),
            severity=request.query_params.get("severity"),
            search=request.query_params.get("q") or request.query_params.get("search"),
            since_hours=int(request.query_params.get("since_hours") or 24),
            limit=int(request.query_params.get("limit") or 200),
        )
    )


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_health_report(request):
    return Response(build_health_report())


@api_view(["POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_self_test(request):
    result = run_full_self_test(
        actor=request.user,
        workstation_id=request.data.get("workstation_id") or request.query_params.get("workstation_id"),
        commissioning_run_id=request.data.get("commissioning_run_id")
        or request.query_params.get("commissioning_run_id"),
    )
    return Response(result)


@api_view(["GET", "POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_commissioning_report(request):
    workstation_id = None
    if request.method == "POST":
        workstation_id = request.data.get("workstation_id")
    workstation_id = workstation_id or request.query_params.get("workstation_id")

    run_tests = request.method == "POST" or (request.query_params.get("self_test") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    self_test = None
    if run_tests:
        self_test = run_full_self_test(actor=request.user, workstation_id=workstation_id)

    payload = build_commissioning_report_payload(actor=request.user, self_test=self_test)
    # Avoid ?format= — DRF content negotiation reserves it.
    fmt = request.query_params.get("export") or request.query_params.get("render")
    if request.method == "POST":
        fmt = request.data.get("export") or request.data.get("format") or fmt
    if str(fmt or "json").lower() == "pdf":
        pdf = render_commissioning_report_pdf(payload)
        resp = HttpResponse(pdf, content_type="application/pdf")
        resp["Content-Disposition"] = 'attachment; filename="ra-commissioning-report.pdf"'
        return resp
    return Response(payload)


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_monitoring_recommendations(request):
    return Response(
        {
            "recommendations": MONITORING_RECOMMENDATIONS,
            "docs": "/docs/RemoteAnalysisCommissioningToolkit.md",
        }
    )


@api_view(["GET", "POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_runs(request):
    """List recent commissioning runs, or POST to start a new run (observability only)."""
    from iic_booking.remote_analysis.operations.commissioning_observability import (
        start_commissioning_run,
        summary_payload,
    )
    from iic_booking.remote_analysis.operations_models import CommissioningRun

    if request.method == "POST":
        run = start_commissioning_run(
            actor=request.user,
            workstation_id=request.data.get("workstation_id"),
            notes=str(request.data.get("notes") or ""),
        )
        return Response(summary_payload(run), status=201)

    limit = max(1, min(int(request.query_params.get("limit") or 50), 200))
    qs = CommissioningRun.objects.order_by("-started_at")[:limit]
    return Response([summary_payload(r) for r in qs])


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_run_detail(request, run_id):
    from django.shortcuts import get_object_or_404

    from iic_booking.remote_analysis.operations.commissioning_observability import summary_payload, timeline_payload
    from iic_booking.remote_analysis.operations_models import CommissioningRun

    run = get_object_or_404(CommissioningRun, pk=run_id)
    return Response({**summary_payload(run), "timeline": timeline_payload(run)})


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_run_timeline(request, run_id):
    from django.shortcuts import get_object_or_404

    from iic_booking.remote_analysis.operations.commissioning_observability import timeline_payload
    from iic_booking.remote_analysis.operations_models import CommissioningRun

    run = get_object_or_404(CommissioningRun, pk=run_id)
    return Response(timeline_payload(run))


@api_view(["GET", "POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_run_evidence(request, run_id):
    """Download (or rebuild) the evidence ZIP for a commissioning run. Admin/manage only."""
    from django.core.files.storage import default_storage
    from django.shortcuts import get_object_or_404

    from iic_booking.remote_analysis.operations.commissioning_observability import (
        build_evidence_bundle_bytes,
        persist_evidence_bundle,
    )
    from iic_booking.remote_analysis.operations_models import CommissioningRun

    run = get_object_or_404(CommissioningRun, pk=run_id)
    rebuild = request.method == "POST" or (request.query_params.get("rebuild") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    if rebuild or not run.evidence_path or not default_storage.exists(run.evidence_path):
        persist_evidence_bundle(run)
        run.refresh_from_db()

    if run.evidence_path and default_storage.exists(run.evidence_path):
        with default_storage.open(run.evidence_path, "rb") as fh:
            data = fh.read()
    else:
        data = build_evidence_bundle_bytes(run)

    resp = HttpResponse(data, content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="commissioning-run-{run.id}-evidence.zip"'
    return resp


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_run_failure_snapshots(request, run_id):
    from django.shortcuts import get_object_or_404

    from iic_booking.remote_analysis.operations_models import CommissioningRun

    run = get_object_or_404(CommissioningRun, pk=run_id)
    return Response(
        [
            {
                "id": str(s.id),
                "step_name": s.step_name,
                "captured_at": s.captured_at.isoformat(),
                "payload": s.payload,
            }
            for s in run.failure_snapshots.all()
        ]
    )


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_live(request):
    """Phase 4 Live Commissioning color dashboard (HTML or JSON)."""
    from iic_booking.remote_analysis.operations.live_commissioning import build_live_commissioning_dashboard
    from iic_booking.remote_analysis.operations.live_commissioning_html import render_live_commissioning_html

    ws_id = request.query_params.get("workstation_id") or None
    payload = build_live_commissioning_dashboard(workstation_id=ws_id)
    if _wants_html(request):
        get_csrf_token(request)
        return HttpResponse(render_live_commissioning_html(payload), content_type="text/html; charset=utf-8")
    return Response(payload)


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_live_timeline(request):
    from iic_booking.remote_analysis.operations.live_commissioning import build_live_session_timeline

    booking_id = request.query_params.get("booking_id")
    booking_id_int = int(booking_id) if booking_id and str(booking_id).isdigit() else None
    return Response(
        build_live_session_timeline(
            booking_id=booking_id_int,
            run_id=request.query_params.get("run_id") or None,
        )
    )


@api_view(["GET"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_faults(request):
    from iic_booking.remote_analysis.operations.fault_injection import list_faults, recovery_checklist
    from iic_booking.remote_analysis.operations.live_commissioning_html import render_fault_injection_html

    payload = {"faults": list_faults(), "recovery": recovery_checklist()}
    if _wants_html(request):
        get_csrf_token(request)
        return HttpResponse(render_fault_injection_html(payload), content_type="text/html; charset=utf-8")
    return Response(payload)


@api_view(["POST"])
@authentication_classes(_AUTH)
@permission_classes(_MANAGE)
def toolkit_fault_inject(request):
    from iic_booking.remote_analysis.operations.fault_injection import inject_fault

    booking_raw = request.data.get("booking_id")
    booking_id = int(booking_raw) if booking_raw not in (None, "") else None
    result = inject_fault(
        fault_id=str(request.data.get("fault_id") or ""),
        actor=request.user,
        workstation_id=request.data.get("workstation_id") or None,
        booking_id=booking_id,
        run_id=request.data.get("run_id") or None,
        dry_run=bool(request.data.get("dry_run")),
    )
    status = 200 if result.get("ok") else 400
    return Response(result, status=status)
