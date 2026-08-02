"""Installer link API — store workstation RDP credentials from Agent installer."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.remote_analysis.installer.services import (
    link_workstation_to_equipment,
    verify_enrollment_key,
)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def link_equipment(request):
    """After register: link workstation to equipment and store RDP secret (server-side only)."""
    ok, err = verify_enrollment_key(request)
    if not ok:
        return Response({"detail": err}, status=status.HTTP_403_FORBIDDEN)

    data = request.data if isinstance(request.data, dict) else {}
    workstation_id = str(data.get("workstation_id") or data.get("workstationId") or "").strip()
    agent_id = str(data.get("agent_id") or data.get("agentId") or "").strip()
    equipment_id = data.get("equipment_id") or data.get("equipmentId")
    if not equipment_id:
        return Response({"detail": "equipment_id is required"}, status=status.HTTP_400_BAD_REQUEST)

    from iic_booking.equipment.models import Equipment
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    ws = None
    if workstation_id:
        ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    if ws is None and agent_id:
        ws = AnalysisWorkstation.objects.filter(agent_id=agent_id).first()
    if ws is None:
        return Response(
            {"detail": "Workstation not found. Register the agent first."},
            status=status.HTTP_404_NOT_FOUND,
        )

    equipment = Equipment.objects.filter(pk=equipment_id, enable_remote_analysis=True).first()
    if not equipment:
        return Response(
            {"detail": "Equipment not found or remote analysis not enabled."},
            status=status.HTTP_404_NOT_FOUND,
        )

    software = data.get("software_slugs") or data.get("softwareSlugs") or data.get("software") or []
    if isinstance(software, str):
        software = [s.strip() for s in software.split(",") if s.strip()]

    result = link_workstation_to_equipment(
        workstation=ws,
        equipment=equipment,
        rdp_username=str(data.get("rdp_username") or data.get("rdpUsername") or "").strip(),
        rdp_password=str(data.get("rdp_password") or data.get("rdpPassword") or ""),
        rdp_domain=str(data.get("rdp_domain") or data.get("rdpDomain") or "").strip(),
        rdp_port=int(data.get("rdp_port") or data.get("rdpPort") or 3389),
        software_slugs=list(software),
        priority_boost=int(data.get("priority_boost") or data.get("priorityBoost") or 10),
    )
    return Response({"accepted": True, **result})
