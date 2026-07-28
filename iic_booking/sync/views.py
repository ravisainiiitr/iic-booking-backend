"""
Thin DRF views for Department Sync control-plane and data-plane APIs.

Business logic lives in service classes.
"""

from __future__ import annotations

import logging
import uuid

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication
from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.permissions import IsDepartmentSyncAgent
from iic_booking.sync.throttles import SyncEnrollRateThrottle
from iic_booking.sync.serializers import (
    BootstrapRequestSerializer,
    CommandCompleteSerializer,
    CommandFailSerializer,
    EnrollRequestSerializer,
    HeartbeatRequestSerializer,
    UploadChunkSerializer,
    UploadCompleteSerializer,
    UploadStartSerializer,
    ResultFinalizeSerializer,
    ResultImportSerializer,
    WorkspaceCreateSerializer,
)
from iic_booking.sync.services import BootstrapService, EnrollmentService, HeartbeatService
from iic_booking.sync.services.dataplane import (
    BookingSyncService,
    CommandService,
    EquipmentSyncService,
    WorkspaceService,
)
from iic_booking.sync.services.upload import UploadTransportService
from iic_booking.sync.services.result_completion import ResultCompletionService
from iic_booking.sync.services.result_processing import ResultProcessingService

logger = logging.getLogger(__name__)

_AGENT_AUTH = [DepartmentSyncAgentAuthentication]
_AGENT_PERM = [IsDepartmentSyncAgent]


def _correlation_id(request) -> uuid.UUID:
    raw = request.headers.get("X-Correlation-ID") or request.META.get("HTTP_X_CORRELATION_ID")
    if raw:
        try:
            return uuid.UUID(str(raw))
        except (TypeError, ValueError):
            pass
    return uuid.uuid4()


def _error_response(exc: SyncControlPlaneError) -> Response:
    return Response(exc.to_dict(), status=exc.status_code)


def _truthy(value) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
@throttle_classes([SyncEnrollRateThrottle])
def enroll(request):
    """POST /api/v1/sync/enroll/ — first-time agent enrollment."""
    serializer = EnrollRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = EnrollmentService().enroll(serializer.validated_data, correlation_id=correlation_id)
    except SyncControlPlaneError as exc:
        # Uniform failure surface — never leak which check failed.
        return Response(
            {"error": {"code": "ENROLLMENT_FAILED", "message": "Enrollment failed."}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception:
        logger.exception("Unexpected enrollment failure")
        return Response(
            {"error": {"code": "ENROLLMENT_FAILED", "message": "Enrollment failed."}},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def heartbeat(request):
    """POST /api/v1/sync/heartbeat/ — telemetry + operational command only."""
    serializer = HeartbeatRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = HeartbeatService().process(
            request.sync_agent,
            serializer.validated_data,
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def bootstrap(request):
    """POST /api/v1/sync/bootstrap/ — full operational configuration document."""
    serializer = BootstrapRequestSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = BootstrapService().build(request.sync_agent, correlation_id=correlation_id)
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def equipment_list(request):
    """
    GET /api/v1/sync/equipment/

    Equipment assigned to this agent (no booking payload).
    Query: modified_after
    """
    correlation_id = _correlation_id(request)
    try:
        result = EquipmentSyncService().list_for_agent(
            request.sync_agent,
            modified_after=request.query_params.get("modified_after"),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def bookings_list(request):
    """
    GET /api/v1/sync/bookings/

    Bookings for equipment assigned to this agent.
    Query: today, active, future, booking_status, last_modified_after, modified_after
    """
    correlation_id = _correlation_id(request)
    try:
        result = BookingSyncService().list_for_agent(
            request.sync_agent,
            today=_truthy(request.query_params.get("today")),
            active=_truthy(request.query_params.get("active")),
            future=_truthy(request.query_params.get("future")),
            booking_status=request.query_params.get("booking_status"),
            last_modified_after=request.query_params.get("last_modified_after"),
            modified_after=request.query_params.get("modified_after"),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def workspaces_create(request):
    """
    POST /api/v1/sync/workspaces/

    Idempotent create/validate booking workspace.
    """
    serializer = WorkspaceCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = WorkspaceService().create_or_get(
            request.sync_agent,
            booking_id=serializer.validated_data["booking_id"],
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    status_code = status.HTTP_201_CREATED if result.get("created") else status.HTTP_200_OK
    return Response(result, status=status_code)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def commands_list(request):
    """
    GET /api/v1/sync/commands/

    Pending (or filtered) commands for this agent.
    Query: status, priority, created_after, command_type, modified_after, pending_only
    """
    correlation_id = _correlation_id(request)
    pending_only = request.query_params.get("pending_only")
    try:
        result = CommandService().list_for_agent(
            request.sync_agent,
            status_filter=request.query_params.get("status"),
            priority=request.query_params.get("priority"),
            created_after=request.query_params.get("created_after"),
            command_type=request.query_params.get("command_type"),
            modified_after=request.query_params.get("modified_after"),
            pending_only=True if pending_only is None else _truthy(pending_only),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def command_acknowledge(request, command_id):
    """POST /api/v1/sync/commands/{id}/acknowledge/"""
    correlation_id = _correlation_id(request)
    try:
        result = CommandService().acknowledge(
            request.sync_agent,
            command_id,
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def command_complete(request, command_id):
    """POST /api/v1/sync/commands/{id}/complete/"""
    serializer = CommandCompleteSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = CommandService().complete(
            request.sync_agent,
            command_id,
            result_payload=serializer.validated_data.get("result_payload") or {},
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def command_fail(request, command_id):
    """POST /api/v1/sync/commands/{id}/fail/"""
    serializer = CommandFailSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    data = serializer.validated_data
    try:
        result = CommandService().fail(
            request.sync_agent,
            command_id,
            failure_reason=data["failure_reason"],
            error_details=data.get("error_details") or {},
            retry_recommended=bool(data.get("retry_recommended")),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def upload_start(request):
    """POST /api/v1/sync/uploads/start/ — create or resume upload session."""
    serializer = UploadStartSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    correlation_id = _correlation_id(request)
    try:
        result = UploadTransportService().start(
            request.sync_agent,
            agent_upload_id=data["agent_upload_id"],
            file_name=data["file_name"],
            relative_path=data.get("relative_path") or "",
            expected_size=data.get("expected_size") or 0,
            equipment_id=data.get("equipment_id"),
            booking_id=data.get("booking_id"),
            workspace_id=data.get("workspace_id"),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def upload_chunk(request):
    """POST /api/v1/sync/uploads/chunk/ — upload one chunk (multipart preferred)."""
    import base64

    payload = request.data
    serializer = UploadChunkSerializer(data=payload)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    correlation_id = _correlation_id(request)

    chunk_file = request.FILES.get("chunk") or request.FILES.get("file")
    if chunk_file is not None:
        chunk_bytes = chunk_file.read()
    elif "chunk_base64" in request.data:
        chunk_bytes = base64.b64decode(request.data.get("chunk_base64") or "")
    elif request.content_type and "octet-stream" in request.content_type:
        chunk_bytes = request.body
    else:
        chunk_bytes = b""

    try:
        result = UploadTransportService().receive_chunk(
            request.sync_agent,
            upload_id=data["upload_id"],
            resume_token=data["resume_token"],
            chunk_index=data["chunk_index"],
            total_chunks=data.get("total_chunks") or 0,
            data=chunk_bytes,
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def upload_complete(request):
    """POST /api/v1/sync/uploads/complete/ — finalize upload session."""
    serializer = UploadCompleteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    correlation_id = _correlation_id(request)
    try:
        result = UploadTransportService().complete(
            request.sync_agent,
            upload_id=data["upload_id"],
            resume_token=data["resume_token"],
            expected_size=data.get("expected_size"),
            chunk_count=data.get("chunk_count"),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def results_import(request):
    """POST /api/v1/sync/results/import/ — create EquipmentResult + measurements."""
    serializer = ResultImportSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    correlation_id = _correlation_id(request)
    try:
        result = ResultProcessingService().import_results(
            request.sync_agent,
            serializer.validated_data,
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def results_finalize(request):
    """POST /api/v1/sync/results/finalize/ — booking PROCESSING → COMPLETED."""
    serializer = ResultFinalizeSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    correlation_id = _correlation_id(request)
    try:
        result = ResultCompletionService().finalize(
            request.sync_agent,
            agent_upload_id=data["agent_upload_id"],
            booking_id=data["booking_id"],
            processing_duration_ms=data.get("processing_duration_ms"),
            correlation_id=correlation_id,
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


# Milestone 11 — Agent Management Dashboard (staff session)
from iic_booking.sync.services.agent_management import (  # noqa: E402
    admin_agent_create_command,
    admin_agent_detail,
    admin_agents_list,
)

# Milestone 12 — Security endpoints
from iic_booking.sync.serializers import (  # noqa: E402
    ApiKeyRotateSerializer,
    CertificateIssueSerializer,
    DeviceIdentityRegisterSerializer,
)
from iic_booking.sync.services.security import SecurityService  # noqa: E402


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_device_register(request):
    """POST /api/v1/sync/security/device/register/"""
    serializer = DeviceIdentityRegisterSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    try:
        result = SecurityService().device_identity.register_or_update(
            request.sync_agent,
            serializer.validated_data,
            correlation_id=_correlation_id(request),
            ip_address=request.META.get("REMOTE_ADDR"),
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_device_identity(request):
    """GET /api/v1/sync/security/device/"""
    return Response(SecurityService().device_identity.get_identity(request.sync_agent))


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_certificate_issue(request):
    """POST /api/v1/sync/security/certificates/issue/"""
    serializer = CertificateIssueSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        result = SecurityService().certificates.issue(
            request.sync_agent,
            public_key=data.get("public_key") or "",
            validity_days=data.get("validity_days") or 365,
            renew=bool(data.get("renew")),
            correlation_id=_correlation_id(request),
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_certificate_renew(request):
    """POST /api/v1/sync/security/certificates/renew/"""
    from iic_booking.sync.constants import certificate_renewal_days

    try:
        result = SecurityService().certificates.renew_if_needed(
            request.sync_agent,
            renewal_days=certificate_renewal_days(),
            correlation_id=_correlation_id(request),
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_certificate_status(request):
    """GET /api/v1/sync/security/certificates/status/"""
    return Response(SecurityService().certificates.validate(request.sync_agent))


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def security_api_key_rotate(request):
    """POST /api/v1/sync/security/api-keys/rotate/"""
    serializer = ApiKeyRotateSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    try:
        result = SecurityService().api_keys.rotate(
            request.sync_agent,
            lifetime_days=data.get("lifetime_days") or 90,
            grace_days=data.get("grace_days") or 7,
            correlation_id=_correlation_id(request),
        )
    except SyncControlPlaneError as exc:
        return _error_response(exc)
    return Response(result, status=status.HTTP_200_OK)


# Milestone 13 — Offline sync / disaster recovery
from iic_booking.sync.serializers import (  # noqa: E402
    ConflictResolveSerializer,
    IntegrityReportSerializer,
    RecoveryEventSerializer,
    RecoveryReconcileSerializer,
)
from iic_booking.sync.services.recovery import RecoveryService  # noqa: E402


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def recovery_reconcile(request):
    """POST /api/v1/sync/recovery/reconcile/"""
    serializer = RecoveryReconcileSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = RecoveryService().reconcile(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def recovery_status(request):
    """GET /api/v1/sync/recovery/status/"""
    return Response(RecoveryService().status(request.sync_agent))


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def recovery_event(request):
    """POST /api/v1/sync/recovery/events/"""
    serializer = RecoveryEventSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    result = RecoveryService().record_event(
        request.sync_agent,
        event_code=data["event_code"],
        message=data["message"],
        component=data.get("component") or "",
        from_state=data.get("from_state") or "",
        to_state=data.get("to_state") or "",
        correlation_id=_correlation_id(request),
        device_id=data.get("device_id"),
        details=data.get("details") or {},
    )
    return Response(result, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def recovery_integrity_report(request):
    """POST /api/v1/sync/recovery/integrity/"""
    serializer = IntegrityReportSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = RecoveryService().integrity.accept_report(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def recovery_conflict(request):
    """POST /api/v1/sync/recovery/conflicts/"""
    serializer = ConflictResolveSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    result = RecoveryService().conflicts.resolve(
        request.sync_agent,
        conflict_type=data["conflict_type"],
        resolution=data.get("resolution") or None,
        upload_id=data.get("upload_id"),
        processing_id=data.get("processing_id"),
        correlation_id=_correlation_id(request),
        details=data.get("details") or {},
    )
    return Response(result, status=status.HTTP_200_OK)


# Milestone 14 — Enterprise multi-agent / multi-department
from rest_framework.authentication import SessionAuthentication  # noqa: E402
from rest_framework.permissions import IsAdminUser  # noqa: E402

from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent  # noqa: E402
from iic_booking.sync.serializers import (  # noqa: E402
    EnterpriseAssignSerializer,
    EnterpriseCapabilitySerializer,
    EnterpriseLifecycleSerializer,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService  # noqa: E402
from iic_booking.sync.services.assignment import AssignmentService  # noqa: E402
from iic_booking.sync.services.enterprise_dashboard import EnterpriseDashboardService  # noqa: E402
from iic_booking.sync.services.topology import TopologyService  # noqa: E402
from iic_booking.users.models import Department  # noqa: E402


def _department_scope(request):
    raw = request.query_params.get("department_id")
    return raw or None


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_departments(request):
    """GET /api/v1/sync/enterprise/departments/"""
    return Response(
        {"departments": TopologyService().list_departments(department_id=_department_scope(request))}
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_buildings(request):
    """GET /api/v1/sync/enterprise/buildings/"""
    return Response(
        {"buildings": TopologyService().list_buildings(department_id=_department_scope(request))}
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_agents(request):
    """GET /api/v1/sync/enterprise/agents/"""
    agents = AgentRegistryService().list_agents(
        department_id=_department_scope(request),
        building_id=request.query_params.get("building_id"),
        status=request.query_params.get("status"),
    )
    return Response({"agents": agents, "count": len(agents)})


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_topology(request):
    """GET /api/v1/sync/enterprise/topology/"""
    dept_id = _department_scope(request)
    if not dept_id:
        return Response({"departments": TopologyService().list_departments()})
    dept = Department.objects.filter(pk=dept_id).first()
    if dept is None:
        return Response({"detail": "Department not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(
        TopologyService().build_topology(
            dept,
            user_name=getattr(request.user, "username", "") or "",
            correlation_id=_correlation_id(request),
        )
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_dashboard(request):
    """GET /api/v1/sync/enterprise/dashboard/"""
    return Response(
        EnterpriseDashboardService().summary(department_id=_department_scope(request))
    )


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_assign(request):
    """POST /api/v1/sync/enterprise/assign/"""
    serializer = EnterpriseAssignSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    agent = DepartmentSyncAgent.objects.filter(pk=data["agent_id"]).first()
    if agent is None:
        return Response({"detail": "Agent not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        result = AssignmentService().assign(
            sync_agent=agent,
            assignment_type=data.get("assignment_type") or "MANUAL",
            building_id=data.get("building_id"),
            laboratory_id=data.get("laboratory_id"),
            equipment_id=data.get("equipment_id"),
            group_id=data.get("group_id"),
            priority=data.get("priority") or 100,
            notes=data.get("notes") or "",
            user_name=getattr(request.user, "username", "") or "",
            correlation_id=_correlation_id(request),
            make_primary=bool(data.get("make_primary", True)),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


def _lifecycle_action(request, new_status: str):
    serializer = EnterpriseLifecycleSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    agent = DepartmentSyncAgent.objects.filter(pk=data["agent_id"]).first()
    if agent is None:
        return Response({"detail": "Agent not found."}, status=status.HTTP_404_NOT_FOUND)
    result = AgentRegistryService().set_lifecycle(
        agent,
        new_status=new_status,
        user_name=getattr(request.user, "username", "") or "",
        correlation_id=_correlation_id(request),
        reason=data.get("reason") or "",
    )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_maintenance(request):
    """POST /api/v1/sync/enterprise/maintenance/"""
    return _lifecycle_action(request, AgentLifecycleStatus.MAINTENANCE)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_drain(request):
    """POST /api/v1/sync/enterprise/drain/"""
    return _lifecycle_action(request, AgentLifecycleStatus.DRAINING)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def enterprise_retire(request):
    """POST /api/v1/sync/enterprise/retire/"""
    return _lifecycle_action(request, AgentLifecycleStatus.RETIRED)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def enterprise_capabilities_report(request):
    """POST /api/v1/sync/enterprise/capabilities/"""
    serializer = EnterpriseCapabilitySerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = AgentRegistryService().record_capabilities(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


# Milestone 15 — Enterprise monitoring / health / alerts
from iic_booking.sync.serializers import (  # noqa: E402
    AlertResolveSerializer,
    MonitoringTelemetrySerializer,
)
from iic_booking.sync.services.alerts import AlertService  # noqa: E402
from iic_booking.sync.services.capacity import CapacityService  # noqa: E402
from iic_booking.sync.services.history import HistoryService  # noqa: E402
from iic_booking.sync.services.monitoring import MonitoringService  # noqa: E402


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_overview(request):
    """GET /api/v1/sync/monitoring/overview/"""
    return Response(MonitoringService().overview(department_id=_department_scope(request)))


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_agents(request):
    """GET /api/v1/sync/monitoring/agents/"""
    agents = MonitoringService().agents(
        department_id=_department_scope(request),
        building_id=request.query_params.get("building_id"),
    )
    return Response({"agents": agents, "count": len(agents)})


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_history(request):
    """GET /api/v1/sync/monitoring/history/"""
    days = int(request.query_params.get("days") or 7)
    limit = int(request.query_params.get("limit") or 200)
    rows = HistoryService().query(
        department_id=_department_scope(request),
        agent_id=request.query_params.get("agent_id"),
        period=request.query_params.get("period"),
        metric_name=request.query_params.get("metric_name"),
        days=days,
        limit=limit,
    )
    return Response({"history": rows, "count": len(rows)})


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_alerts(request):
    """GET /api/v1/sync/monitoring/alerts/"""
    rows = AlertService().list_alerts(
        department_id=_department_scope(request),
        status=request.query_params.get("status"),
        severity=request.query_params.get("severity"),
        agent_id=request.query_params.get("agent_id"),
        limit=int(request.query_params.get("limit") or 100),
    )
    return Response(
        {
            "alerts": rows,
            "count": len(rows),
            "summary": AlertService().summary(department_id=_department_scope(request)),
        }
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_capacity(request):
    """GET /api/v1/sync/monitoring/capacity/"""
    days = int(request.query_params.get("days") or 30)
    return Response(
        {
            "summary": CapacityService().summary(department_id=_department_scope(request)),
            "trends": CapacityService().trends(
                department_id=_department_scope(request),
                days=days,
                limit=int(request.query_params.get("limit") or 100),
            ),
        }
    )


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_alert_acknowledge(request, alert_id):
    """POST /api/v1/sync/monitoring/alerts/{id}/acknowledge/"""
    try:
        result = AlertService().acknowledge(
            alert_id,
            user_name=getattr(request.user, "username", "") or "",
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def monitoring_alert_resolve(request, alert_id):
    """POST /api/v1/sync/monitoring/alerts/{id}/resolve/"""
    serializer = AlertResolveSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    try:
        result = AlertService().resolve(
            alert_id,
            user_name=getattr(request.user, "username", "") or "",
            resolution=serializer.validated_data.get("resolution") or "",
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def monitoring_telemetry(request):
    """POST /api/v1/sync/monitoring/telemetry/ — agent health/metrics ingest."""
    serializer = MonitoringTelemetrySerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = MonitoringService().ingest_telemetry(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


# Milestone 16 — Automatic updates / release orchestration
from iic_booking.sync.serializers import (  # noqa: E402
    ReleaseCreateSerializer,
    ReleaseDeploySerializer,
    ReleasePublishSerializer,
    ReleaseRollbackSerializer,
    UpdateStatusReportSerializer,
)
from iic_booking.sync.services.releases import ReleaseService  # noqa: E402
from iic_booking.sync.services.rollout import RolloutService  # noqa: E402
from iic_booking.sync.services.update_manager import UpdateManagerService  # noqa: E402


@api_view(["GET", "POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def releases_list(request):
    """GET/POST /api/v1/sync/releases/"""
    if request.method == "GET":
        rows = ReleaseService().list_releases(
            department_id=_department_scope(request),
            channel=request.query_params.get("channel"),
            package_type=request.query_params.get("package_type"),
            status=request.query_params.get("status"),
            limit=int(request.query_params.get("limit") or 100),
        )
        return Response({"releases": rows, "count": len(rows)})
    serializer = ReleaseCreateSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = ReleaseService().create(
        serializer.validated_data,
        created_by=getattr(request.user, "username", "") or "",
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def releases_detail(request, release_id):
    """GET /api/v1/sync/releases/{id}/"""
    data = ReleaseService().get(release_id)
    if data is None:
        return Response({"detail": "Release not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def releases_publish(request):
    """POST /api/v1/sync/releases/publish/"""
    serializer = ReleasePublishSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    try:
        result = ReleaseService().publish(
            serializer.validated_data["package_id"],
            user_name=getattr(request.user, "username", "") or "",
            correlation_id=_correlation_id(request),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def releases_deploy(request):
    """POST /api/v1/sync/releases/deploy/"""
    serializer = ReleaseDeploySerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    from iic_booking.sync.models import ReleasePackage, UpdateDeployment

    package = ReleasePackage.objects.filter(pk=data["package_id"]).first()
    if package is None:
        return Response({"detail": "Release not found."}, status=status.HTTP_404_NOT_FOUND)
    deployment = RolloutService().create_deployment(
        package=package,
        strategy=data.get("strategy") or "MANUAL",
        channel=data.get("channel") or None,
        percentage=data.get("percentage") or 100,
        department_id=data.get("department_id") or _department_scope(request),
        building_id=data.get("building_id"),
        agent_group_id=data.get("agent_group_id"),
        scheduled_at=data.get("scheduled_at"),
        maintenance_window_start=data.get("maintenance_window_start"),
        maintenance_window_end=data.get("maintenance_window_end"),
        requires_approval=bool(data.get("requires_approval")),
        target_agent_ids=[str(x) for x in (data.get("target_agent_ids") or [])],
        created_by=getattr(request.user, "username", "") or "",
        correlation_id=_correlation_id(request),
    )
    if data.get("start_immediately", True) and not data.get("requires_approval"):
        dep = UpdateDeployment.objects.filter(pk=deployment["id"]).first()
        if dep:
            try:
                deployment = RolloutService().start(
                    dep, approved_by=getattr(request.user, "username", "") or ""
                )
            except ValueError as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(deployment, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def releases_rollback(request):
    """POST /api/v1/sync/releases/rollback/"""
    serializer = ReleaseRollbackSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    result = UpdateManagerService().rollback(
        package_id=data.get("package_id"),
        agent_id=data.get("agent_id"),
        to_version=data.get("to_version") or "",
        reason=data.get("reason") or "",
        user_name=getattr(request.user, "username", "") or "",
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def updates_history(request):
    """GET /api/v1/sync/updates/history/"""
    rows = UpdateManagerService().history(
        department_id=_department_scope(request),
        agent_id=request.query_params.get("agent_id"),
        limit=int(request.query_params.get("limit") or 100),
    )
    return Response({"history": rows, "count": len(rows)})


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def updates_status(request):
    """GET /api/v1/sync/updates/status/ — release dashboard."""
    return Response(UpdateManagerService().status_dashboard(department_id=_department_scope(request)))


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def updates_discover(request):
    """GET /api/v1/sync/updates/discover/ — agent release discovery."""
    current = request.query_params.get("current_version") or ""
    return Response(
        UpdateManagerService().discover_for_agent(request.sync_agent, current_version=current)
    )


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def updates_status_report(request):
    """POST /api/v1/sync/updates/report/ — agent update lifecycle report."""
    serializer = UpdateStatusReportSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = UpdateManagerService().report_status(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


# Milestone 18 — Instrument integration / experiment workflows
from iic_booking.sync.serializers import (  # noqa: E402
    ExperimentReportSerializer,
    ExperimentTelemetrySerializer,
)
from iic_booking.sync.services.experiments import ExperimentService, InstrumentCatalogService  # noqa: E402


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def experiments_list(request):
    """GET /api/v1/sync/experiments/"""
    rows = ExperimentService().list_sessions(
        department_id=_department_scope(request),
        agent_id=request.query_params.get("agent_id"),
        status=request.query_params.get("status"),
        plugin_id=request.query_params.get("plugin_id"),
        limit=int(request.query_params.get("limit") or 100),
    )
    return Response(
        {
            "experiments": rows,
            "count": len(rows),
            "summary": ExperimentService().summary(department_id=_department_scope(request)),
        }
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def experiments_detail(request, experiment_id):
    """GET /api/v1/sync/experiments/{id}/"""
    data = ExperimentService().get(experiment_id)
    if data is None:
        return Response({"detail": "Experiment not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def experiments_report(request):
    """POST /api/v1/sync/experiments/report/ — agent experiment status upsert."""
    serializer = ExperimentReportSerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = ExperimentService().upsert_from_agent(
        request.sync_agent,
        serializer.validated_data,
        correlation_id=_correlation_id(request),
    )
    return Response(result, status=status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def experiments_telemetry(request):
    """POST /api/v1/sync/experiments/telemetry/"""
    serializer = ExperimentTelemetrySerializer(data=request.data or {})
    serializer.is_valid(raise_exception=True)
    result = ExperimentService().record_telemetry(request.sync_agent, serializer.validated_data)
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def instruments_list(request):
    """GET /api/v1/sync/instruments/"""
    rows = InstrumentCatalogService().list_instruments(department_id=_department_scope(request))
    return Response({"instruments": rows, "count": len(rows)})


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def plugins_list(request):
    """GET /api/v1/sync/plugins/"""
    active_only = (request.query_params.get("active") or "1") not in ("0", "false", "False")
    rows = InstrumentCatalogService().list_plugins(active_only=active_only)
    return Response({"plugins": rows, "count": len(rows)})


# Production hardening / operational APIs (Release Candidate)
from iic_booking.sync.services.diagnostics import PortalDiagnosticsService  # noqa: E402
from iic_booking.sync.services.maintenance import MaintenanceService  # noqa: E402


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def operations_diagnostics(request):
    """GET /api/v1/sync/operations/diagnostics/"""
    return Response(PortalDiagnosticsService().summary(department_id=_department_scope(request)))


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def operations_table_sizes(request):
    """GET /api/v1/sync/operations/table-sizes/"""
    return Response(PortalDiagnosticsService().table_sizes())


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def operations_top_events(request):
    """GET /api/v1/sync/operations/top-events/"""
    rows = PortalDiagnosticsService().top_event_codes(
        department_id=_department_scope(request),
        limit=int(request.query_params.get("limit") or 20),
    )
    return Response({"events": rows, "count": len(rows)})


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def operations_maintenance(request):
    """POST /api/v1/sync/operations/maintenance/ — prune retention windows."""
    body = request.data or {}
    result = MaintenanceService().run(
        department_id=_department_scope(request) or body.get("department_id"),
        dry_run=bool(body.get("dry_run", False)),
        sync_log_days=body.get("sync_log_days"),
        heartbeat_days=body.get("heartbeat_days"),
        update_history_days=body.get("update_history_days"),
        monitoring_days=body.get("monitoring_days"),
    )
    return Response(result, status=status.HTTP_200_OK)
