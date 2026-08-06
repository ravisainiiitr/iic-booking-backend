"""DSA Installer portal + enrollment-keyed bootstrap APIs."""

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
from iic_booking.sync.installer.models import DsaInstallerRelease
from iic_booking.sync.installer.services import (
    link_agent_to_equipment,
    resolve_installer_agent,
    sha256_filefield,
)
from iic_booking.sync.permissions import CanManageDepartmentSync

_MANAGE = [IsAuthenticated, CanManageDepartmentSync]


def _serialize_release(rel: DsaInstallerRelease) -> dict:
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
        default_name="DepartmentSyncAgentSetup.exe",
        prefer_redirect=prefer_redirect,
        **headers,
    )


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
@parser_classes([JSONParser, MultiPartParser, FormParser])
def releases_collection(request):
    """GET list / POST upload new DSA installer release (Main Admin)."""
    if request.method == "GET":
        qs = DsaInstallerRelease.objects.filter(is_active=True).order_by(
            "-is_latest", "-release_date", "-created_at"
        )
        include_inactive = str(request.query_params.get("all") or "") in {"1", "true", "yes"}
        if include_inactive:
            qs = DsaInstallerRelease.objects.all().order_by(
                "-is_latest", "-release_date", "-created_at"
            )
        return Response({"count": qs.count(), "results": [_serialize_release(r) for r in qs[:50]]})

    version = (request.data.get("version") or "").strip()
    if not version:
        return Response({"detail": "version is required"}, status=status.HTTP_400_BAD_REQUEST)
    uploaded = request.FILES.get("file") or request.FILES.get("installer")
    offline = request.FILES.get("offline_file") or request.FILES.get("offline")

    rel = DsaInstallerRelease(
        version=version,
        channel=(request.data.get("channel") or DsaInstallerRelease.Channel.STABLE).strip(),
        release_date=request.data.get("release_date") or timezone.now().date(),
        release_notes=request.data.get("release_notes") or "",
        supported_windows=request.data.get("supported_windows")
        or "Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
        min_ram_gb=int(request.data.get("min_ram_gb") or 8),
        min_disk_gb=int(request.data.get("min_disk_gb") or 20),
        signature_status=(
            request.data.get("signature_status") or DsaInstallerRelease.SignatureStatus.UNSIGNED
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


@api_view(["GET"])
@permission_classes(_MANAGE)
def release_latest(request):
    rel = (
        DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )
    if not rel:
        return Response({"detail": "No installer release published."}, status=status.HTTP_404_NOT_FOUND)
    return Response(_serialize_release(rel))


def _download_release(request, release_id=None):
    """Shared installer download logic (must not call another @api_view)."""
    if release_id:
        rel = get_object_or_404(DsaInstallerRelease, pk=release_id, is_active=True)
    else:
        rel = (
            DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
            or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
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
    if offline and getattr(field, "size", None):
        hdrs["size_bytes"] = field.size
    # Authenticated fetch fallback must stream (no S3 302) — CORS breaks blob reads on redirect.
    return _file_response(field, name, prefer_redirect=False, **hdrs)


@api_view(["GET"])
@permission_classes(_MANAGE)
def release_download(request, release_id=None):
    return _download_release(request, release_id=release_id)


@api_view(["GET"])
@permission_classes(_MANAGE)
def release_latest_download(request):
    return _download_release(request, release_id=None)


def _resolve_latest_or_id(release_id=None):
    if release_id:
        return get_object_or_404(DsaInstallerRelease, pk=release_id, is_active=True)
    return (
        DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def release_download_ticket(request, release_id=None):
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
        product="dsa",
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

    direct = build_direct_download_url(field, download_name=name, expires_in=900)
    url = direct or absolute_ticket_url(request, "dsa", token)

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

    if data.get("p") != "dsa":
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    rel = DsaInstallerRelease.objects.filter(pk=data["r"], is_active=True).first()
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
def equipment_tree(request):
    """
    Legacy DSA installer equipment tree.

    Accepts Agent UUID + enrollment secret (break-glass) OR claim Bearer /
    X-Agent-Access-Token (zero-touch). Prefer /api/v1/provisioning/dsa/equipment-tree/
    for new installers.
    """
    from iic_booking.sync.installer.services import build_equipment_tree_for_department

    ok, err, agent = resolve_installer_agent(request, allow_access_token=True)
    if not ok:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    tree = build_equipment_tree_for_department(getattr(agent, "department_id", None))
    tree["agent_uuid"] = str(agent.agent_uuid)
    tree["department_id"] = str(agent.department_id) if agent.department_id else None
    return Response(tree)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def link_equipment(request):
    """After enroll: link agent to equipment via access token (or pre-enroll secret)."""
    ok, err, agent = resolve_installer_agent(request, allow_access_token=True)
    if not ok:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    equipment_id = data.get("equipment_id") or data.get("equipmentId")
    if not equipment_id:
        return Response({"detail": "equipment_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    from iic_booking.equipment.models import Equipment

    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"detail": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if agent.department_id and equipment.internal_department_id:
        if equipment.internal_department_id != agent.department_id:
            return Response(
                {"detail": "Equipment does not belong to the agent's department."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    result = link_agent_to_equipment(
        agent=agent,
        equipment=equipment,
        result_folder=str(data.get("result_folder") or data.get("resultFolder") or "").strip(),
        unc_path=str(data.get("unc_path") or data.get("uncPath") or "").strip(),
        watch_folder=str(data.get("watch_folder") or data.get("watchFolder") or "").strip(),
        hostname=str(data.get("hostname") or "").strip(),
        ip_address=str(data.get("ip_address") or data.get("ipAddress") or "").strip(),
        share_name=str(data.get("share_name") or data.get("shareName") or "").strip(),
    )
    return Response({"accepted": True, **result})
