"""HTTP views — booking analysis integration APIs."""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings as django_settings
from django.contrib.auth import login as django_login
from django.http import HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token as get_csrf_token
from django.shortcuts import get_object_or_404
from django.urls import reverse
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework import status
from rest_framework.views import APIView

from iic_booking.equipment.models import Booking
from iic_booking.equipment.remote_analysis_integration.dashboard import BookingAnalysisDashboardService
from iic_booking.equipment.remote_analysis_integration.desktop_html import render_desktop_launcher_html
from iic_booking.equipment.remote_analysis_integration.reports import BookingReportBridge
from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService
from iic_booking.remote_analysis.guacamole.session import SessionError
from iic_booking.remote_analysis.operations.commissioning import QueryParamTokenAuthentication
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

_AUTH = [TokenAuthenticationWithInactivity, SessionAuthentication, QueryParamTokenAuthentication]

# Elevated roles that may assist with analysis files/archives (not faculty-by-department alone).
_ANALYSIS_FILE_STAFF_TYPES = frozenset(
    {"admin", "dept_admin", "manager", "officer_in_charge", "operator"}
)


def _is_analysis_admin(user) -> bool:
    """Staff who may see infrastructure details (hostname) on booking analysis APIs."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    return user_type in _ANALYSIS_FILE_STAFF_TYPES


def _can_access_booking(user, booking: Booking) -> bool:
    """Read access to analysis summary / workflows / job status."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type in _ANALYSIS_FILE_STAFF_TYPES:
        return True
    if booking.user_id == user.pk:
        return True
    if booking.created_by_id == user.pk:
        return True
    # Faculty supervision: same department — summary only (not files; see _can_access_analysis_files)
    if getattr(user, "department_id", None) and getattr(booking.user, "department_id", None):
        if user.department_id == booking.user.department_id and user_type in {"faculty", "staff"}:
            return True
    return False


def _can_access_analysis_files(user, booking: Booking) -> bool:
    """
    File list / download / archive — owner or elevated analysis staff only.
    Faculty/staff same-department summary access does NOT grant file enumeration.
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    if booking.user_id == user.pk:
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    return user_type in _ANALYSIS_FILE_STAFF_TYPES


def _can_launch(user, booking: Booking) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    return booking.user_id == user.pk


def _client_ip(request) -> str | None:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _absolute_builder(request):
    def build(path: str) -> str:
        return request.build_absolute_uri(path)

    return build


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


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_detail(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/"""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    payload = BookingRemoteAnalysisService().get_summary(
        booking,
        user=request.user,
        request=request,
        include_files=_can_access_analysis_files(request.user, booking),
        expose_infrastructure=_is_analysis_admin(request.user),
    )
    payload["report"] = BookingReportBridge().enrich_booking(booking)
    payload["launcher_url"] = f"/api/v1/bookings/{booking.booking_id}/analysis/desktop/?view=html"
    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_software(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/software/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    options = SoftwareMappingService().serialize_options(
        booking.equipment, settings_obj=RemoteAnalysisSettings.get_solo()
    )
    return Response({"software_options": options})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_analyze(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/analyze/ — one-shot Analyze Data."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response(
            {"detail": "Only the booking owner may start Analyze Data.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    body = request.data if hasattr(request, "data") else {}
    try:
        payload = BookingRemoteAnalysisService().analyze_data(
            booking,
            user=request.user,
            mapping_id=(body.get("mapping_id") or body.get("software_mapping_id") or None),
            catalog_id=(body.get("catalog_id") or None),
            software_slug=(body.get("software_slug") or body.get("slug") or None),
            workflow_id=(body.get("workflow_id") or None),
            variables=(body.get("variables") if isinstance(body.get("variables"), dict) else None),
            client_ip=_client_ip(request),
            request_absolute_uri_builder=_absolute_builder(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            request=request,
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden", "booking_ineligible"} else status.HTTP_400_BAD_REQUEST
        return Response(
            {"detail": str(exc), "code": exc.code, "eligible": False},
            status=http,
        )
    except ValueError as exc:
        return Response({"detail": str(exc), "eligible": False}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as exc:  # noqa: BLE001
        return Response({"detail": str(exc), "eligible": False}, status=status.HTTP_400_BAD_REQUEST)
    code = status.HTTP_202_ACCEPTED if payload.get("queued") else status.HTTP_201_CREATED
    return Response(payload, status=code)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_create(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/create/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {"reservation_id": str(reservation.id), "status": reservation.status},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_launch(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/launch/ — additive launch_url / launcher_url."""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Only the booking owner may launch the desktop."}, status=status.HTTP_403_FORBIDDEN)
    try:
        payload = BookingRemoteAnalysisService().launch_session(
            booking,
            user=request.user,
            client_ip=_client_ip(request),
            request_absolute_uri_builder=_absolute_builder(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden", "booking_ineligible"} else status.HTTP_400_BAD_REQUEST
        detail = str(exc)
        return Response(
            {
                "detail": detail,
                "code": exc.code,
                "eligible": False,
                "failure": {
                    "user_message": detail,
                    "failure_category": "credentials"
                    if exc.code == "rdp_credentials_missing" or "credentials" in detail.lower()
                    else "launch",
                    "failed_stage": "preflight" if exc.code == "rdp_credentials_missing" else "launch",
                },
            },
            status=http,
        )
    except Exception as exc:
        return Response({"detail": str(exc), "eligible": False}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload, status=status.HTTP_201_CREATED)


class BookingAnalysisDesktopView(APIView):
    """GET /api/v1/bookings/{id}/analysis/desktop/?view=html — Launch Remote Analysis page."""

    authentication_classes = _AUTH
    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        if isinstance(exc, NotAuthenticated) and _wants_html(self.request):
            return _login_redirect(self.request)
        if isinstance(exc, PermissionDenied) and _wants_html(self.request):
            return HttpResponse("<h1>Forbidden</h1>", status=403, content_type="text/html; charset=utf-8")
        return super().handle_exception(exc)

    def get(self, request, booking_id: int, *args, **kwargs):
        if _wants_html(request) and request.query_params.get("token") and request.user.is_authenticated:
            django_login(request, request.user, backend="django.contrib.auth.backends.ModelBackend")
            parts = urlsplit(request.build_absolute_uri())
            query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "token"]
            if not any(k == "view" for k, _ in query):
                query.append(("view", "html"))
            target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
            return HttpResponseRedirect(target)

        booking = get_object_or_404(
            Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
            booking_id=booking_id,
        )
        if not _can_access_booking(request.user, booking):
            if _wants_html(request):
                return HttpResponse("<h1>Forbidden</h1>", status=403, content_type="text/html; charset=utf-8")
            return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

        payload = BookingRemoteAnalysisService().desktop_launcher_payload(booking, user=request.user)
        if _wants_html(request):
            get_csrf_token(request)
            return HttpResponse(render_desktop_launcher_html(payload), content_type="text/html; charset=utf-8")
        return Response(payload)


booking_analysis_desktop = BookingAnalysisDesktopView.as_view()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_files(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/files/ — owner or analysis staff only."""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_analysis_files(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "files_forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    files = BookingRemoteAnalysisService().workspace.list_files(booking)
    return Response(files)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_end(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/end/ — finish early; free environment for next user."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    body = request.data if hasattr(request, "data") else {}
    reason = (body.get("reason") if isinstance(body, dict) else None) or "Finished early by user"
    try:
        payload = BookingRemoteAnalysisService().end_analysis(
            booking, user=request.user, reason=str(reason)
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    except Exception:
        logger = __import__("logging").getLogger(__name__)
        logger.exception("booking_analysis_end failed booking_id=%s", booking_id)
        return Response(
            {
                "detail": "Could not end analysis due to an unexpected server error.",
                "code": "end_failed",
            },
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_start(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/start/ — explicit check-in Start Analysis Session."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    try:
        payload = BookingRemoteAnalysisService().start_checked_in_session(
            booking,
            user=request.user,
            client_ip=_client_ip(request),
            request_absolute_uri_builder=_absolute_builder(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_release(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/release/ — release reserved PC without starting."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    body = request.data if hasattr(request, "data") else {}
    reason = (body.get("reason") if isinstance(body, dict) else None) or "Released by user"
    try:
        payload = BookingRemoteAnalysisService().release_checkin(
            booking, user=request.user, reason=str(reason)
        )
    except SessionError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_extend(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/extend/ — extend session when queue is empty."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        payload = BookingRemoteAnalysisService().extend_analysis(booking, user=request.user)
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    return Response(payload)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_data_browser(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/data-browser/ — authorized current/previous files."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user"),
        booking_id=booking_id,
    )
    if not _can_access_analysis_files(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.equipment.remote_analysis_integration.data_browser import AnalysisDataBrowserService

    params = request.query_params
    source_raw = params.get("source_booking_id")
    source_booking_id = int(source_raw) if str(source_raw or "").isdigit() else None
    payload = AnalysisDataBrowserService().browse(
        booking,
        user=request.user,
        q=params.get("q") or "",
        scope=params.get("scope") or "current",
        page=int(params.get("page") or 1),
        page_size=int(params.get("page_size") or 20),
        source_booking_id=source_booking_id,
        prefix=params.get("prefix") or "",
        file_offset=int(params.get("file_offset") or 0),
        file_limit=int(params.get("file_limit") or 40),
        file_type=params.get("file_type") or "",
        request=request,
    )
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_data_selection(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/data-selection/ — persist selection before allocation."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response(
            {"detail": "Only the booking owner may select analysis data.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    body = request.data if hasattr(request, "data") else {}
    from iic_booking.equipment.remote_analysis_integration.data_browser import AnalysisDataBrowserService

    source_kind = str(body.get("source") or "").strip().lower()
    svc = AnalysisDataBrowserService()
    try:
        if source_kind == "upload":
            payload = svc.save_upload_selection(
                booking,
                user=request.user,
                file_names=body.get("file_names") if isinstance(body.get("file_names"), list) else None,
            )
        else:
            source_booking_id = body.get("source_booking_id")
            if source_booking_id in (None, ""):
                return Response(
                    {"detail": "source_booking_id is required.", "code": "missing_source"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            payload = svc.save_selection(
                booking,
                user=request.user,
                source_booking_id=int(source_booking_id),
                folder_path=str(body.get("folder_path") or ""),
                file_names=body.get("file_names") if isinstance(body.get("file_names"), list) else None,
                source_kind=source_kind,
            )
    except PermissionError as exc:
        return Response({"detail": str(exc), "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    except ValueError as exc:
        return Response({"detail": str(exc), "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@authentication_classes(_AUTH)
def booking_analysis_files_upload(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/files/upload/ — past/extra data into RawData → agent Input."""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_launch(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    uploaded = request.FILES.get("file") or request.FILES.get("upload")
    if not uploaded:
        return Response({"detail": "Missing file", "code": "missing_file"}, status=status.HTTP_400_BAD_REQUEST)
    folder = (request.data.get("folder") if hasattr(request, "data") else None) or "RawData"
    try:
        payload = BookingRemoteAnalysisService().upload_past_data(
            booking,
            user=request.user,
            uploaded_file=uploaded,
            folder=str(folder),
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_archive(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/archive/ — owner or analysis staff only."""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_analysis_files(request.user, booking):
        return Response(
            {"detail": "Permission denied.", "code": "archive_forbidden"},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        archive = BookingRemoteAnalysisService().archive_workspace(booking, actor=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"archive_id": str(getattr(archive, "id", "")), "ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_dashboard(request):
    """GET /api/v1/bookings/analysis/dashboard/?scope=user|faculty|lab"""
    scope = (request.query_params.get("scope") or "user").lower()
    svc = BookingAnalysisDashboardService()
    if scope == "lab":
        return Response(svc.for_lab(request.user))
    if scope == "faculty":
        return Response(svc.for_faculty(request.user))
    return Response(svc.for_user(request.user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_workflows(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/workflows/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine

    return Response({"workflows": WorkflowEngine().list_workflows_for_equipment(booking.equipment)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_job(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/job/"""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    job = BookingRemoteAnalysisService().get_analysis_job(booking, user=request.user)
    return Response({"job": job})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_job_complete_step(request, booking_id: int, step_number: int):
    """POST /api/v1/bookings/{id}/analysis/job/steps/{n}/complete/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    body = request.data if hasattr(request, "data") else {}
    try:
        payload = BookingRemoteAnalysisService().complete_analysis_step(
            booking,
            user=request.user,
            step_number=step_number,
            force=bool(body.get("force")),
        )
    except SessionError as exc:
        http = status.HTTP_403_FORBIDDEN if exc.code in {"forbidden"} else status.HTTP_400_BAD_REQUEST
        return Response({"detail": str(exc), "code": exc.code}, status=http)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_job_pause(request, booking_id: int):
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        payload = BookingRemoteAnalysisService().pause_analysis_job(booking, user=request.user)
    except SessionError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_job_resume(request, booking_id: int):
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        payload = BookingRemoteAnalysisService().resume_analysis_job(
            booking,
            user=request.user,
            client_ip=_client_ip(request),
            request_absolute_uri_builder=_absolute_builder(request),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
        )
    except SessionError as exc:
        return Response({"detail": str(exc), "code": exc.code}, status=status.HTTP_400_BAD_REQUEST)
    return Response(payload)
