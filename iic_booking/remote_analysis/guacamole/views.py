"""API views for browser remote desktop sessions."""

from __future__ import annotations

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.constants import SessionStatus
from iic_booking.remote_analysis.guacamole.permissions import can_observe_session
from iic_booking.remote_analysis.guacamole.serializers import (
    CreateSessionSerializer,
    RemoteDesktopSessionSerializer,
    SessionActivitySerializer,
    SessionAuditSerializer,
    SessionStateHistorySerializer,
)
from iic_booking.remote_analysis.guacamole.services import GuacamoleIntegrationService
from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionAudit

_AUTH = [IsAuthenticated]
_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _absolute_builder(request):
    def build(path: str) -> str:
        return request.build_absolute_uri(path)

    return build


@api_view(["POST"])
@permission_classes(_AUTH)
def session_create(request):
    """POST /api/v1/analysis/session/create/"""
    ser = CreateSessionSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data
    svc = GuacamoleIntegrationService()
    try:
        session = svc.create_session_for_reservation(
            data["reservation_id"],
            request.user,
            client_ip=_client_ip(request),
            browser=data.get("browser") or request.META.get("HTTP_USER_AGENT", "")[:255],
            client_platform=data.get("client_platform") or "",
            wait_for_prepare=bool(data.get("wait_for_prepare")),
        )
    except AnalysisReservation.DoesNotExist:
        return Response({"detail": "Reservation not found"}, status=status.HTTP_404_NOT_FOUND)
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code == "forbidden" else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    return Response(RemoteDesktopSessionSerializer(session).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_AUTH)
def session_launch(request, session_id):
    """GET /api/v1/analysis/session/{id}/launch/"""
    session = get_object_or_404(
        RemoteDesktopSession.objects.select_related("workstation", "user", "reservation"),
        pk=session_id,
    )
    redirect = request.query_params.get("redirect", "").lower() in {"1", "true", "yes"}
    svc = GuacamoleIntegrationService()
    try:
        payload = svc.launch(
            session,
            request.user,
            request_absolute_uri_builder=_absolute_builder(request),
            client_ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            redirect=redirect,
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code == "forbidden" else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)

    if redirect:
        from django.shortcuts import redirect as django_redirect

        return django_redirect(payload["launch_url"])
    return Response(payload)


@api_view(["GET", "POST"])
@permission_classes(_AUTH)
def session_connect(request, session_id):
    """
    GET/POST /api/v1/analysis/session/{id}/connect/?t=<one-time-token>
    Consumes the launch token and returns connection payload (no secrets to user beyond ephemeral client token).
    With ?redirect=1 or Accept: text/html, redirects to Guacamole client_url (live) or shows mock HTML.
    """
    session = get_object_or_404(RemoteDesktopSession, pk=session_id)
    token = request.query_params.get("t") or request.data.get("token") or ""
    if not token:
        return Response({"detail": "Missing token", "code": "missing_token"}, status=status.HTTP_400_BAD_REQUEST)
    svc = GuacamoleIntegrationService()
    try:
        payload = svc.connect(
            session,
            token,
            request.user,
            client_ip=_client_ip(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden", "token_user_mismatch", "token_ip_mismatch"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)

    wants_redirect = (request.query_params.get("redirect") or "").lower() in {"1", "true", "yes"}
    accept = (request.META.get("HTTP_ACCEPT") or "").lower()
    wants_html = wants_redirect or ("text/html" in accept and "application/json" not in accept.split(",")[0])

    if wants_html:
        client_url = (payload.get("client") or {}).get("client_url") or payload.get("redirect_url") or ""
        if client_url:
            from django.shortcuts import redirect as django_redirect

            return django_redirect(client_url)
        if payload.get("mock"):
            html = (
                "<!DOCTYPE html><html><head><title>Mock Remote Desktop</title></head>"
                "<body style='font-family:sans-serif;padding:2rem'>"
                "<h1>Mock Guacamole Session</h1>"
                f"<p>Session {session_id} connected (mock mode — no remote host contacted).</p>"
                "<p>Disable mock_guacamole and configure Guacamole for live RDP.</p>"
                "</body></html>"
            )
            return HttpResponse(html, content_type="text/html; charset=utf-8")
        return Response(
            {"detail": "No Guacamole client_url configured (set guacamole_base_url)", "code": "guac_url_missing"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(payload)


@api_view(["POST"])
@permission_classes(_AUTH)
def session_terminate(request, session_id):
    """POST /api/v1/analysis/session/{id}/terminate/"""
    session = get_object_or_404(RemoteDesktopSession, pk=session_id)
    reason = (request.data.get("reason") or "Terminated by user").strip()
    svc = GuacamoleIntegrationService()
    try:
        session = svc.terminate(session, user=request.user, reason=reason)
    except SessionError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_403_FORBIDDEN)
    return Response(RemoteDesktopSessionSerializer(session).data)


@api_view(["GET"])
@permission_classes(_AUTH)
def session_status(request, session_id):
    """GET /api/v1/analysis/session/{id}/status/"""
    session = get_object_or_404(
        RemoteDesktopSession.objects.select_related("workstation", "user", "statistics", "health"),
        pk=session_id,
    )
    if not can_observe_session(request.user, session):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    # Opportunistically advance prepare → ready
    if session.status == SessionStatus.PREPARING:
        GuacamoleIntegrationService().retry_prepare(session)
        session.refresh_from_db()

    GuacamoleIntegrationService().health(session)
    session.refresh_from_db()
    data = RemoteDesktopSessionSerializer(session).data
    data["state_history"] = SessionStateHistorySerializer(session.state_history.all()[:30], many=True).data
    return Response(data)


@api_view(["GET"])
@permission_classes(_VIEW)
def sessions_list(request):
    """GET /api/v1/analysis/sessions/"""
    qs = RemoteDesktopSession.objects.select_related("workstation", "user", "statistics", "health").order_by("-created_at")
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())
    mine = request.query_params.get("mine", "").lower() in {"1", "true", "yes"}
    if mine or not CanManageRemoteAnalysis().has_permission(request, None):
        qs = qs.filter(user=request.user)
    from iic_booking.remote_analysis.production_hardening import parse_pagination

    offset, limit = parse_pagination(request)
    return Response(RemoteDesktopSessionSerializer(qs[offset : offset + limit], many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def session_history(request):
    """GET /api/v1/analysis/session/history/"""
    qs = RemoteDesktopSession.objects.filter(
        status__in=[
            SessionStatus.COMPLETED,
            SessionStatus.TERMINATED,
            SessionStatus.EXPIRED,
            SessionStatus.FAILED,
        ]
    ).select_related("workstation", "user", "statistics").order_by("-disconnected_at", "-updated_at")
    if not CanManageRemoteAnalysis().has_permission(request, None):
        qs = qs.filter(user=request.user)
    from iic_booking.remote_analysis.production_hardening import parse_pagination

    offset, limit = parse_pagination(request)
    return Response(RemoteDesktopSessionSerializer(qs[offset : offset + limit], many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def session_dashboard(request):
    """GET /api/v1/analysis/session/dashboard/"""
    return Response(GuacamoleIntegrationService().dashboard_metrics())


@api_view(["POST"])
@permission_classes(_AUTH)
def session_activity(request, session_id):
    """POST /api/v1/analysis/session/{id}/activity/ — browser keepalive / bandwidth."""
    session = get_object_or_404(RemoteDesktopSession, pk=session_id)
    if not can_observe_session(request.user, session):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    ser = SessionActivitySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    SessionOrchestrator().heartbeat_activity(
        session,
        bytes_in=ser.validated_data.get("bytes_in") or 0,
        bytes_out=ser.validated_data.get("bytes_out") or 0,
    )
    return Response({"ok": True, "last_activity_at": timezone.now().isoformat()})


@api_view(["GET"])
@permission_classes(_VIEW)
def session_audits(request, session_id):
    session = get_object_or_404(RemoteDesktopSession, pk=session_id)
    if not can_observe_session(request.user, session):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    rows = SessionAudit.objects.filter(session=session).order_by("-created_at")[:100]
    return Response(SessionAuditSerializer(rows, many=True).data)
