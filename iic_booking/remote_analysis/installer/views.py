"""Agent Installer portal + enrollment-keyed bootstrap APIs."""

from __future__ import annotations

import os

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.common_download import (
    build_direct_download_url,
    build_installer_file_response,
    release_download_headers,
)
from iic_booking.installer_download_tickets import absolute_ticket_url, issue_ticket, parse_ticket
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.installer.models import AgentInstallerRelease
from iic_booking.remote_analysis.installer.services import (
    link_workstation_to_equipment,
    seed_workstation_software_from_selection,
    sha256_filefield,
    verify_enrollment_key,
)

_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]


def _serialize_release(rel: AgentInstallerRelease) -> dict:
    return {
        "id": str(rel.id),
        "version": rel.version,
        "channel": rel.channel,
        "release_date": rel.release_date.isoformat() if rel.release_date else None,
        "release_notes": rel.release_notes,
        "supported_windows": rel.supported_windows,
        "min_ram_gb": rel.min_ram_gb,
        "min_disk_gb": rel.min_disk_gb,
        "download_size_bytes": rel.download_size_bytes
        or (rel.file.size if rel.file else 0),
        "sha256": rel.sha256,
        "signature_status": rel.signature_status,
        "signature_status_display": rel.get_signature_status_display(),
        "has_file": bool(rel.file),
        "has_offline_file": bool(rel.offline_file),
        "original_name": rel.original_name
        or (os.path.basename(rel.file.name) if rel.file else ""),
        "offline_original_name": rel.offline_original_name
        or (os.path.basename(rel.offline_file.name) if rel.offline_file else ""),
        "documentation_url": rel.documentation_url,
        "installation_guide_url": rel.installation_guide_url,
        "troubleshooting_guide_url": rel.troubleshooting_guide_url,
        "is_latest": rel.is_latest,
        "is_active": rel.is_active,
        "created_at": rel.created_at.isoformat() if rel.created_at else None,
    }


def _file_response(file_field, download_name: str, *, prefer_redirect: bool = True, **headers):
    return build_installer_file_response(
        file_field,
        download_name=download_name,
        default_name="RemoteAnalysisAgentSetup.exe",
        prefer_redirect=prefer_redirect,
        **headers,
    )


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
@parser_classes([JSONParser, MultiPartParser, FormParser])
def releases_collection(request):
    """GET list / POST upload new installer release (Main Admin / RA manage)."""
    if request.method == "GET":
        qs = AgentInstallerRelease.objects.filter(is_active=True).order_by(
            "-is_latest", "-release_date", "-created_at"
        )
        include_inactive = str(request.query_params.get("all") or "") in {"1", "true", "yes"}
        if include_inactive:
            qs = AgentInstallerRelease.objects.all().order_by(
                "-is_latest", "-release_date", "-created_at"
            )
        return Response({"count": qs.count(), "results": [_serialize_release(r) for r in qs[:50]]})

    version = (request.data.get("version") or "").strip()
    if not version:
        return Response({"detail": "version is required"}, status=status.HTTP_400_BAD_REQUEST)
    uploaded = request.FILES.get("file") or request.FILES.get("installer")
    offline = request.FILES.get("offline_file") or request.FILES.get("offline")

    rel = AgentInstallerRelease(
        version=version,
        channel=(request.data.get("channel") or AgentInstallerRelease.Channel.STABLE).strip(),
        release_date=request.data.get("release_date") or timezone.now().date(),
        release_notes=request.data.get("release_notes") or "",
        supported_windows=request.data.get("supported_windows")
        or "Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
        min_ram_gb=int(request.data.get("min_ram_gb") or 8),
        min_disk_gb=int(request.data.get("min_disk_gb") or 20),
        signature_status=(
            request.data.get("signature_status") or AgentInstallerRelease.SignatureStatus.UNSIGNED
        ),
        documentation_url=request.data.get("documentation_url") or "",
        installation_guide_url=request.data.get("installation_guide_url") or "",
        troubleshooting_guide_url=request.data.get("troubleshooting_guide_url") or "",
        is_active=True,
    )
    if uploaded:
        rel.file = uploaded
        rel.original_name = getattr(uploaded, "name", "") or ""
        rel.download_size_bytes = getattr(uploaded, "size", 0) or 0
    if offline:
        rel.offline_file = offline
        rel.offline_original_name = getattr(offline, "name", "") or ""
    rel.save()
    if rel.file:
        rel.sha256 = sha256_filefield(rel.file)
        rel.save(update_fields=["sha256", "updated_at"])
    mark_latest = str(request.data.get("is_latest") or "1") in {"1", "true", "yes"}
    if mark_latest:
        rel.mark_latest()
    return Response(_serialize_release(rel), status=status.HTTP_201_CREATED)


def _agent_or_enrollment_or_manage(request) -> tuple[bool, str | None]:
    """Phase 2.5 H-07/H-08: agents may discover updates without admin session."""
    from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
    from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis

    if isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
        return True, None
    if request.user and getattr(request.user, "is_authenticated", False):
        if CanManageRemoteAnalysis().has_permission(request, None):
            return True, None
        if getattr(request.user, "is_superuser", False):
            return True, None
    ok, err = verify_enrollment_key(request)
    if ok:
        return True, None
    return False, err or "Authentication required."


def _try_bind_agent_auth(request) -> None:
    """Attach RemoteAnalysisAgentUser when Bearer token is valid for the agent/workstation."""
    from iic_booking.remote_analysis.authentication import (
        RemoteAnalysisAgentAuthentication,
        RemoteAnalysisAgentUser,
    )
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.remote_analysis.services.tokens import find_active_token

    if isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
        return
    auth_header = request.META.get("HTTP_AUTHORIZATION") or ""
    if not auth_header.lower().startswith("bearer "):
        return

    parts = auth_header.split()
    if len(parts) != 2:
        return
    bearer = parts[1].strip()
    if not bearer:
        return

    try:
        result = RemoteAnalysisAgentAuthentication().authenticate(request)
    except Exception:
        result = None
    if result:
        request.user, request.auth = result
        return

    # Installer post-claim: Bearer is valid but X-Agent-Id may be missing/wrong — resolve
    # workstation from JSON body (workstationId / agentId) and bind auth.
    data = request.data if isinstance(request.data, dict) else {}
    ws_id = str(data.get("workstation_id") or data.get("workstationId") or "").strip()
    agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
    ws = None
    if ws_id:
        ws = AnalysisWorkstation.objects.filter(pk=ws_id).first()
    if ws is None and agent_id:
        ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    if ws is not None and find_active_token(ws, bearer) is not None:
        request.user = RemoteAnalysisAgentUser(ws)
        request.auth = bearer


def _try_bind_portal_token(request) -> None:
    """Attach Django user when Authorization: Token … is present (zero-touch installer)."""
    from iic_booking.users.api.token_auth import OptionalTokenAuthentication

    if getattr(request.user, "is_authenticated", False):
        return
    auth_header = request.META.get("HTTP_AUTHORIZATION") or ""
    if not auth_header.lower().startswith("token "):
        return
    result = OptionalTokenAuthentication().authenticate(request)
    if result:
        request.user, request.auth = result


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def release_latest(request):
    """Latest RA agent installer — admin session, enrollment key, or agent bearer."""
    _try_bind_agent_auth(request)
    allowed, err = _agent_or_enrollment_or_manage(request)
    if not allowed:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    channel = (request.query_params.get("channel") or "").strip().lower()
    qs = AgentInstallerRelease.objects.filter(is_active=True)
    if channel in {"stable", "production", "prod"}:
        from django.db.models import Q

        qs = qs.filter(Q(channel__iexact="stable") | Q(channel__iexact="production"))
    elif channel:
        qs = qs.filter(channel__iexact=channel)
    rel = (
        qs.filter(is_latest=True).first()
        or qs.order_by("-release_date").first()
    )
    if not rel:
        return Response({"detail": "No installer release published."}, status=status.HTTP_404_NOT_FOUND)
    payload = _serialize_release(rel)
    current = (request.query_params.get("current_version") or "").strip()
    if current:
        payload["current_version"] = current
        payload["update_available"] = (rel.version or "") != current
    return Response(payload)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def agent_update_report(request):
    """RAA posts update-discovery status (enrollment key or agent bearer)."""
    from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
    from iic_booking.remote_analysis.constants import AuditCategory
    from iic_booking.remote_analysis.services.audit import record_event

    _try_bind_agent_auth(request)
    allowed, err = _agent_or_enrollment_or_manage(request)
    if not allowed:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    agent_id = (request.headers.get("X-Agent-Id") or data.get("agent_id") or "").strip()
    if not agent_id and isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
        agent_id = getattr(request.user.workstation, "agent_id", "") or ""

    detail = {
        "current_version": data.get("current_version") or "",
        "latest_version": data.get("latest_version") or "",
        "update_available": bool(data.get("update_available")),
        "channel": data.get("channel") or "production",
        "checked_at": data.get("checked_at"),
        "detail": data.get("detail") or "",
        "agent_id": agent_id,
    }
    try:
        import json

        from iic_booking.remote_analysis.models import AnalysisWorkstation

        ws = None
        if agent_id:
            ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
        if ws is None and isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
            ws = request.user.workstation
        record_event(
            category=AuditCategory.STATUS,
            action="agent_update_discovery",
            details=json.dumps(detail, default=str)[:4000],
            success=True,
            workstation=ws,
        )
    except Exception:
        pass

    return Response({"accepted": True, "report": detail}, status=status.HTTP_202_ACCEPTED)


def _download_release(request, release_id=None):
    """Shared installer download logic (must not call another @api_view)."""
    if release_id:
        rel = get_object_or_404(AgentInstallerRelease, pk=release_id, is_active=True)
    else:
        rel = (
            AgentInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
            or AgentInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
        )
        if not rel:
            return Response({"detail": "No installer release published."}, status=status.HTTP_404_NOT_FOUND)
    offline = str(request.query_params.get("offline") or "") in {"1", "true", "yes"}
    field = rel.offline_file if offline else rel.file
    if not field:
        return Response({"detail": "Installer file not available."}, status=status.HTTP_404_NOT_FOUND)
    name = (
        rel.offline_original_name
        if offline
        else rel.original_name
    ) or os.path.basename(field.name)
    hdrs = release_download_headers(rel, offline=offline)
    if offline and field.size:
        hdrs["size_bytes"] = field.size
    # Authenticated fetch fallback must stream (no S3 302) — CORS breaks blob reads on redirect.
    return _file_response(field, name, prefer_redirect=False, **hdrs)


@api_view(["GET"])
@permission_classes(_MANAGE)
def release_download(request, release_id=None):
    """Download setup EXE for a release, or latest when release_id omitted via sibling route."""
    return _download_release(request, release_id=release_id)


@api_view(["GET"])
@permission_classes(_MANAGE)
def release_latest_download(request):
    return _download_release(request, release_id=None)


def _resolve_latest_or_id(release_id=None):
    if release_id:
        return get_object_or_404(AgentInstallerRelease, pk=release_id, is_active=True)
    rel = (
        AgentInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or AgentInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )
    return rel


@api_view(["POST"])
@permission_classes(_MANAGE)
def release_download_ticket(request, release_id=None):
    """
    Issue a short-lived ticket so the browser can download natively
    (no SPA buffering of 100+ MB installers).
    """
    rel = _resolve_latest_or_id(release_id)
    if not rel:
        return Response({"detail": "No installer release published."}, status=status.HTTP_404_NOT_FOUND)
    offline = str(request.data.get("offline") or request.query_params.get("offline") or "") in {
        "1",
        "true",
        "yes",
    }
    field = rel.offline_file if offline else rel.file
    if not field:
        return Response({"detail": "Installer file not available."}, status=status.HTTP_404_NOT_FOUND)

    token = issue_ticket(
        product="ra",
        release_id=str(rel.id),
        offline=offline,
        user_id=getattr(request.user, "pk", None),
    )
    name = (
        rel.offline_original_name if offline else rel.original_name
    ) or os.path.basename(field.name)
    try:
        size = int(getattr(field, "size", 0) or 0) or (
            int(rel.download_size_bytes or 0) if not offline else 0
        )
    except Exception:
        size = int(rel.download_size_bytes or 0) if not offline else 0

    # Prefer direct S3 URL — browser downloads from object storage, not via Django.
    direct = build_direct_download_url(field, download_name=name, expires_in=900)
    url = direct or absolute_ticket_url(request, "ra", token)

    return Response(
        {
            "token": token,
            "url": url,
            "direct": bool(direct),
            "expires_in": 900,
            "filename": name,
            "size_bytes": size,
            "sha256": "" if offline else (rel.sha256 or ""),
            "version": rel.version,
            "offline": offline,
        }
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def release_download_by_ticket(request, token: str):
    from django.core import signing

    try:
        data = parse_ticket(token)
    except signing.SignatureExpired:
        return Response({"detail": "Download link expired. Please try again."}, status=status.HTTP_403_FORBIDDEN)
    except signing.BadSignature:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    if data.get("p") != "ra":
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    rel = AgentInstallerRelease.objects.filter(pk=data["r"], is_active=True).first()
    if not rel:
        return Response({"detail": "Installer release not found."}, status=status.HTTP_404_NOT_FOUND)
    offline = bool(int(data.get("o") or 0))
    field = rel.offline_file if offline else rel.file
    if not field:
        return Response({"detail": "Installer file not available."}, status=status.HTTP_404_NOT_FOUND)
    name = (
        rel.offline_original_name if offline else rel.original_name
    ) or os.path.basename(field.name)
    hdrs = release_download_headers(rel, offline=offline)
    try:
        hdrs["size_bytes"] = int(getattr(field, "size", 0) or 0) or hdrs.get("size_bytes")
    except Exception:
        pass
    return _file_response(field, name, **hdrs)


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def catalog_software(request):
    """Enrollment-keyed software catalog for installer wizard."""
    ok, err = verify_enrollment_key(request)
    if not ok:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog

    rows = AnalysisSoftwareCatalog.objects.filter(is_active=True).order_by("name")
    results = []
    for c in rows:
        results.append(
            {
                "id": str(c.id),
                "name": c.name,
                "slug": c.slug,
                "vendor": c.vendor,
                "version_constraint": c.version_constraint,
                "description": c.description,
                "category": getattr(c, "category", "") or "",
                "icon_url": getattr(c, "icon_url", "") or "",
            }
        )
    return Response({"count": len(results), "results": results})


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def equipment_tree(request):
    """Enrollment key, portal admin Token, or agent bearer → department/equipment tree."""
    _try_bind_agent_auth(request)
    _try_bind_portal_token(request)
    allowed, err = _agent_or_enrollment_or_manage(request)
    if not allowed:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models import Department

    equipment_qs = (
        Equipment.objects.filter(enable_remote_analysis=True)
        .select_related("internal_department")
        .order_by("name")
    )
    by_dept: dict[str, dict] = {}
    for eq in equipment_qs:
        dept = eq.internal_department
        dept_id = str(dept.id) if dept else "unassigned"
        dept_name = dept.name if dept else "Unassigned"
        if dept_id not in by_dept:
            by_dept[dept_id] = {
                "id": dept_id,
                "name": dept_name,
                "equipment": [],
            }
        by_dept[dept_id]["equipment"].append(
            {
                "id": eq.pk,
                "name": eq.name,
                "code": getattr(eq, "code", "") or "",
                "enable_remote_analysis": True,
            }
        )
    for d in Department.objects.all().order_by("name")[:200]:
        did = str(d.id)
        if did not in by_dept:
            continue
    return Response({"count": len(by_dept), "departments": list(by_dept.values())})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def link_equipment(request):
    """After register/claim: link workstation to equipment, store RDP secret, attach software."""
    _try_bind_agent_auth(request)
    _try_bind_portal_token(request)
    allowed, err = _agent_or_enrollment_or_manage(request)
    if not allowed:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    workstation_id = str(data.get("workstation_id") or data.get("workstationId") or "").strip()
    agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
    equipment_id = data.get("equipment_id") or data.get("equipmentId")
    if not equipment_id:
        return Response({"detail": "equipment_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    from iic_booking.equipment.models import Equipment
    from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    ws = None
    if workstation_id:
        ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    if ws is None and agent_id:
        ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    if ws is None and isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
        ws = request.user.workstation
    if ws is None:
        return Response({"detail": "Workstation not found. Register the agent first."}, status=status.HTTP_404_NOT_FOUND)

    equipment = Equipment.objects.filter(pk=equipment_id, enable_remote_analysis=True).first()
    if not equipment:
        return Response(
            {"detail": "Equipment not found or remote analysis not enabled."},
            status=status.HTTP_404_NOT_FOUND,
        )

    software = data.get("software_slugs") or data.get("softwareSlugs") or []
    if isinstance(software, str):
        software = [s.strip() for s in software.split(",") if s.strip()]
    software_items = data.get("software_items") or data.get("softwareItems") or data.get("software") or []
    if isinstance(software_items, str):
        software_items = []

    result = link_workstation_to_equipment(
        workstation=ws,
        equipment=equipment,
        rdp_username=str(data.get("rdp_username") or data.get("rdpUsername") or "").strip(),
        rdp_password=str(data.get("rdp_password") or data.get("rdpPassword") or ""),
        rdp_domain=str(data.get("rdp_domain") or data.get("rdpDomain") or "").strip(),
        rdp_port=int(data.get("rdp_port") or data.get("rdpPort") or 3389),
        software_slugs=list(software) if isinstance(software, list) else [],
        software_items=list(software_items) if isinstance(software_items, list) else [],
        priority_boost=int(data.get("priority_boost") or data.get("priorityBoost") or 10),
    )
    return Response({"accepted": True, **result})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def seed_inventory(request):
    """
    R11: after register/claim, seed Software Catalog + InstalledSoftware from
    installer selection when no equipment is linked (wizard no longer asks).
    """
    _try_bind_agent_auth(request)
    _try_bind_portal_token(request)
    allowed, err = _agent_or_enrollment_or_manage(request)
    if not allowed:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    workstation_id = str(data.get("workstation_id") or data.get("workstationId") or "").strip()
    agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()

    from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentUser
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    ws = None
    if workstation_id:
        ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    if ws is None and agent_id:
        ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    if ws is None and isinstance(getattr(request, "user", None), RemoteAnalysisAgentUser):
        ws = request.user.workstation
    if ws is None:
        return Response({"detail": "Workstation not found. Register the agent first."}, status=status.HTTP_404_NOT_FOUND)

    software = data.get("software_slugs") or data.get("softwareSlugs") or []
    if isinstance(software, str):
        software = [s.strip() for s in software.split(",") if s.strip()]
    software_items = data.get("software_items") or data.get("softwareItems") or data.get("software") or []
    if isinstance(software_items, str):
        software_items = []

    if not software and not software_items:
        return Response({"detail": "softwareSlugs or softwareItems required."}, status=status.HTTP_400_BAD_REQUEST)

    result = seed_workstation_software_from_selection(
        workstation=ws,
        software_slugs=list(software) if isinstance(software, list) else [],
        software_items=list(software_items) if isinstance(software_items, list) else [],
    )
    from iic_booking.remote_analysis.services.catalog_sync import (
        archive_infrastructure_catalog_entries,
        archive_unmanaged_auto_catalog_entries,
    )

    cleanup = {
        **archive_infrastructure_catalog_entries(),
        **archive_unmanaged_auto_catalog_entries(),
    }
    return Response({"accepted": True, **result, "cleanup": cleanup})
