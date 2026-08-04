"""Deployment Center APIs — aggregate catalog + Equipment PC Wizard releases."""

from __future__ import annotations

import os

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
)
from iic_booking.installer_download_tickets import absolute_ticket_url, issue_ticket, parse_ticket
from iic_booking.deployment.models import EquipmentPcWizardRelease
from iic_booking.remote_analysis.installer.models import AgentInstallerRelease
from iic_booking.sync.installer.models import DsaInstallerRelease
from iic_booking.sync.installer.services import sha256_filefield
from iic_booking.sync.permissions import CanManageDepartmentSync

_MANAGE = [IsAuthenticated, CanManageDepartmentSync]


def _serialize_dsa(rel: DsaInstallerRelease) -> dict:
    return {
        "product": "dsa",
        "product_label": "Department Sync Agent",
        "id": str(rel.id),
        "version": rel.version,
        "channel": rel.channel,
        "release_date": rel.release_date.isoformat() if rel.release_date else None,
        "release_notes": rel.release_notes,
        "supported_windows": rel.supported_windows,
        "download_size_bytes": rel.download_size_bytes
        or (rel.file.size if rel.file else 0),
        "sha256": rel.sha256,
        "signature_status": rel.signature_status,
        "signature_status_display": rel.get_signature_status_display(),
        "has_file": bool(rel.file),
        "has_offline_file": bool(rel.offline_file),
        "original_name": rel.original_name
        or (os.path.basename(rel.file.name) if rel.file else ""),
        "documentation_url": rel.documentation_url,
        "installation_guide_url": rel.installation_guide_url,
        "troubleshooting_guide_url": rel.troubleshooting_guide_url,
        "is_latest": rel.is_latest,
        "is_active": rel.is_active,
        "download_count": getattr(rel, "download_count", None),
    }


def _serialize_ra(rel: AgentInstallerRelease) -> dict:
    return {
        "product": "ra",
        "product_label": "Remote Analysis Agent",
        "id": str(rel.id),
        "version": rel.version,
        "channel": rel.channel,
        "release_date": rel.release_date.isoformat() if rel.release_date else None,
        "release_notes": rel.release_notes,
        "supported_windows": rel.supported_windows,
        "download_size_bytes": rel.download_size_bytes
        or (rel.file.size if rel.file else 0),
        "sha256": rel.sha256,
        "signature_status": rel.signature_status,
        "signature_status_display": rel.get_signature_status_display(),
        "has_file": bool(rel.file),
        "has_offline_file": bool(rel.offline_file),
        "original_name": rel.original_name
        or (os.path.basename(rel.file.name) if rel.file else ""),
        "documentation_url": rel.documentation_url,
        "installation_guide_url": rel.installation_guide_url,
        "troubleshooting_guide_url": rel.troubleshooting_guide_url,
        "is_latest": rel.is_latest,
        "is_active": rel.is_active,
        "download_count": getattr(rel, "download_count", None),
    }


def _serialize_wizard(rel: EquipmentPcWizardRelease) -> dict:
    return {
        "product": "eq_wizard",
        "product_label": rel.product_name or "Equipment PC Configuration Wizard",
        "id": str(rel.id),
        "version": rel.version,
        "build_number": rel.build_number,
        "channel": rel.channel,
        "release_date": rel.release_date.isoformat() if rel.release_date else None,
        "release_notes": rel.release_notes,
        "supported_windows": rel.supported_windows,
        "download_size_bytes": rel.download_size_bytes
        or (rel.file.size if rel.file else 0),
        "sha256": rel.sha256,
        "signature_status": rel.signature_status,
        "signature_status_display": rel.get_signature_status_display(),
        "has_file": bool(rel.file),
        "has_offline_file": False,
        "has_repair_file": bool(getattr(rel, "repair_file", None)),
        "has_emergency_file": bool(getattr(rel, "emergency_file", None)),
        "compatibility": getattr(rel, "compatibility", None) or {},
        "rollback_of": str(rel.rollback_of_id) if getattr(rel, "rollback_of_id", None) else None,
        "original_name": rel.original_name
        or (os.path.basename(rel.file.name) if rel.file else ""),
        "documentation_url": rel.documentation_url,
        "installation_guide_url": rel.installation_guide_url,
        "troubleshooting_guide_url": rel.troubleshooting_guide_url,
        "is_latest": rel.is_latest,
        "is_active": rel.is_active,
        "download_count": rel.download_count,
    }


def _latest_dsa():
    return (
        DsaInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or DsaInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )


def _latest_ra():
    return (
        AgentInstallerRelease.objects.filter(is_active=True, is_latest=True).first()
        or AgentInstallerRelease.objects.filter(is_active=True).order_by("-release_date").first()
    )


def _latest_wizard():
    return (
        EquipmentPcWizardRelease.objects.filter(is_active=True, is_latest=True).first()
        or EquipmentPcWizardRelease.objects.filter(is_active=True)
        .order_by("-release_date")
        .first()
    )


@api_view(["GET"])
@permission_classes(_MANAGE)
def deployment_center(request):
    """Aggregate installer catalog for the Deployment Center UI."""
    dsa = _latest_dsa()
    ra = _latest_ra()
    wizard = _latest_wizard()

    previous_dsa = list(
        DsaInstallerRelease.objects.filter(is_active=True)
        .exclude(pk=dsa.pk if dsa else None)
        .order_by("-release_date", "-created_at")[:5]
    )
    previous_ra = list(
        AgentInstallerRelease.objects.filter(is_active=True)
        .exclude(pk=ra.pk if ra else None)
        .order_by("-release_date", "-created_at")[:5]
    )
    previous_wizard = list(
        EquipmentPcWizardRelease.objects.filter(is_active=True)
        .exclude(pk=wizard.pk if wizard else None)
        .order_by("-release_date", "-created_at")[:5]
    )

    return Response(
        {
            "products": [
                {
                    "key": "dsa",
                    "label": "Department Sync Agent",
                    "guide_path": "/department-sync/agent-installer",
                    "ticket_product": "dsa",
                    "latest": _serialize_dsa(dsa) if dsa else None,
                    "previous": [_serialize_dsa(r) for r in previous_dsa],
                },
                {
                    "key": "ra",
                    "label": "Remote Analysis Agent",
                    "guide_path": "/remote-analysis/agent-installer",
                    "ticket_product": "ra",
                    "latest": _serialize_ra(ra) if ra else None,
                    "previous": [_serialize_ra(r) for r in previous_ra],
                },
                {
                    "key": "eq_wizard",
                    "label": "Equipment PC Configuration Wizard",
                    "guide_path": "/deployment-center",
                    "ticket_product": "eq_wizard",
                    "latest": _serialize_wizard(wizard) if wizard else None,
                    "previous": [_serialize_wizard(r) for r in previous_wizard],
                },
            ],
            "links": {
                "dsa_guide": "/department-sync/agent-installer",
                "ra_guide": "/remote-analysis/agent-installer",
                "deployment_center": "/deployment-center",
            },
        }
    )


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
@parser_classes([JSONParser, MultiPartParser, FormParser])
def wizard_releases_collection(request):
    if request.method == "GET":
        qs = EquipmentPcWizardRelease.objects.filter(is_active=True).order_by(
            "-is_latest", "-release_date", "-created_at"
        )
        include_inactive = str(request.query_params.get("all") or "") in {"1", "true", "yes"}
        if include_inactive:
            qs = EquipmentPcWizardRelease.objects.all().order_by(
                "-is_latest", "-release_date", "-created_at"
            )
        return Response(
            {"count": qs.count(), "results": [_serialize_wizard(r) for r in qs[:50]]}
        )

    version = (request.data.get("version") or "").strip()
    if not version:
        return Response({"detail": "version is required"}, status=status.HTTP_400_BAD_REQUEST)
    uploaded = request.FILES.get("file") or request.FILES.get("installer")

    rel = EquipmentPcWizardRelease(
        product_name=(request.data.get("product_name") or "Equipment PC Configuration Wizard").strip(),
        version=version,
        build_number=(request.data.get("build_number") or "").strip(),
        channel=(request.data.get("channel") or EquipmentPcWizardRelease.Channel.STABLE).strip(),
        release_date=request.data.get("release_date") or timezone.now().date(),
        release_notes=request.data.get("release_notes") or "",
        supported_windows=request.data.get("supported_windows")
        or "Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
        signature_status=(
            request.data.get("signature_status")
            or EquipmentPcWizardRelease.SignatureStatus.UNSIGNED
        ),
        documentation_url=request.data.get("documentation_url") or "",
        installation_guide_url=request.data.get("installation_guide_url") or "/deployment-center",
        troubleshooting_guide_url=request.data.get("troubleshooting_guide_url") or "",
        is_active=True,
    )
    if uploaded:
        rel.file = uploaded
        rel.original_name = getattr(uploaded, "name", "") or ""
        rel.download_size_bytes = getattr(uploaded, "size", 0) or 0
    rel.save()
    if rel.file:
        rel.sha256 = sha256_filefield(rel.file)
        rel.save(update_fields=["sha256", "updated_at"])
    mark_latest = str(request.data.get("is_latest") or "1") in {"1", "true", "yes"}
    if mark_latest:
        rel.mark_latest()
    return Response(_serialize_wizard(rel), status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_MANAGE)
def wizard_release_latest(request):
    rel = _latest_wizard()
    if not rel:
        return Response(
            {"detail": "No Equipment PC Wizard release published."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(_serialize_wizard(rel))


def _resolve_wizard(release_id=None):
    if release_id:
        return get_object_or_404(EquipmentPcWizardRelease, pk=release_id, is_active=True)
    return _latest_wizard()


@api_view(["POST"])
@permission_classes(_MANAGE)
def wizard_download_ticket(request, release_id=None):
    rel = _resolve_wizard(release_id)
    if not rel:
        return Response(
            {"detail": "No Equipment PC Wizard release published."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not rel.file:
        return Response({"detail": "Installer file not available."}, status=status.HTTP_404_NOT_FOUND)

    token = issue_ticket(
        product="eq_wizard",
        release_id=str(rel.id),
        offline=False,
        user_id=getattr(request.user, "pk", None),
    )
    name = rel.original_name or os.path.basename(rel.file.name)
    try:
        size = int(getattr(rel.file, "size", 0) or 0) or int(rel.download_size_bytes or 0)
    except Exception:
        size = int(rel.download_size_bytes or 0)

    direct = build_direct_download_url(rel.file, download_name=name, expires_in=900)
    url = direct or absolute_ticket_url(request, "eq_wizard", token)

    return Response(
        {
            "token": token,
            "url": url,
            "direct": bool(direct),
            "expires_in": 900,
            "filename": name,
            "size_bytes": size,
            "sha256": rel.sha256 or "",
            "version": rel.version,
            "offline": False,
        }
    )


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def wizard_download_by_ticket(request, token: str):
    from django.core import signing

    try:
        data = parse_ticket(token)
    except signing.SignatureExpired:
        return Response(
            {"detail": "Download link expired. Please try again."},
            status=status.HTTP_403_FORBIDDEN,
        )
    except signing.BadSignature:
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    if data.get("p") != "eq_wizard":
        return Response({"detail": "Invalid download link."}, status=status.HTTP_403_FORBIDDEN)

    rel = EquipmentPcWizardRelease.objects.filter(pk=data["r"], is_active=True).first()
    if not rel or not rel.file:
        return Response({"detail": "Installer release not found."}, status=status.HTTP_404_NOT_FOUND)

    EquipmentPcWizardRelease.objects.filter(pk=rel.pk).update(
        download_count=(rel.download_count or 0) + 1
    )
    name = rel.original_name or os.path.basename(rel.file.name)
    hdrs = {
        "sha256": rel.sha256 or "",
        "version": rel.version,
        "size_bytes": int(getattr(rel.file, "size", 0) or 0) or int(rel.download_size_bytes or 0),
    }
    return build_installer_file_response(
        rel.file,
        download_name=name,
        default_name="EquipmentPcConfigurationWizard.exe",
        prefer_redirect=True,
        **hdrs,
    )
