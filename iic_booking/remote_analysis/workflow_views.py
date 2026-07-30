"""Staff Analysis Workflow Designer APIs."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine
from iic_booking.remote_analysis.workflow_models import (
    AnalysisCapability,
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)


def _is_staff(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True
    return str(getattr(user, "user_type", "") or "").lower() in {
        "admin",
        "dept_admin",
        "manager",
        "officer_in_charge",
    }


def _serialize_workflow(wf: AnalysisWorkflow, *, include_draft: bool = True) -> dict:
    versions = list(wf.versions.order_by("-version_number"))
    published = next((v for v in versions if v.is_published), None)
    draft = next((v for v in versions if not v.is_published), None)
    version = draft if include_draft and draft else published
    steps = []
    if version:
        steps = [
            {
                "id": str(s.id),
                "step_number": s.step_number,
                "title": s.title,
                "display_title": s.display_title,
                "software_id": str(s.software_id) if s.software_id else None,
                "software": s.software.name if s.software_id else None,
                "capability_id": str(s.capability_id) if s.capability_id else None,
                "capability": s.capability.name if s.capability_id else None,
                "version_constraint": s.version_constraint,
                "mandatory": s.mandatory,
                "estimated_duration_minutes": s.estimated_duration_minutes,
                "expected_output_folder": s.expected_output_folder,
                "expected_outputs": s.expected_outputs or [],
                "allowed_file_types": s.allowed_file_types or [],
                "validation_rules": s.validation_rules or {},
                "description": s.description,
                "operator_instructions": s.operator_instructions,
                "help_url": s.help_url,
                "reference_manual_url": s.reference_manual_url,
                "environment_label": s.environment_label,
            }
            for s in version.steps.select_related("software", "capability").order_by("step_number")
        ]
    return {
        "id": str(wf.id),
        "name": wf.name,
        "slug": wf.slug,
        "description": wf.description,
        "is_active": wf.is_active,
        "is_template": wf.is_template,
        "estimated_duration_minutes": wf.estimated_duration_minutes,
        "require_raw_data": wf.require_raw_data,
        "require_calibration": wf.require_calibration,
        "require_reference_files": wf.require_reference_files,
        "optional_input_types": wf.optional_input_types or [],
        "variables_schema": wf.variables_schema or [],
        "ai_assistance_enabled": wf.ai_assistance_enabled,
        "collaboration_enabled": wf.collaboration_enabled,
        "published_version": (
            {"id": str(published.id), "version_number": published.version_number, "label": published.label}
            if published
            else None
        ),
        "draft_version_id": str(draft.id) if draft else None,
        "steps": steps,
        "equipment_mappings": [
            {
                "id": str(m.id),
                "equipment_id": m.equipment_id,
                "is_default": m.is_default,
                "sort_order": m.sort_order,
            }
            for m in wf.equipment_mappings.all()
        ],
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def workflow_collection(request):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        qs = AnalysisWorkflow.objects.all().order_by("name")
        if request.query_params.get("active") == "1":
            qs = qs.filter(is_active=True)
        return Response({"workflows": [_serialize_workflow(w) for w in qs]})

    body = request.data or {}
    name = (body.get("name") or "").strip()
    if not name:
        return Response({"detail": "name is required"}, status=status.HTTP_400_BAD_REQUEST)
    wf = AnalysisWorkflow.objects.create(
        name=name,
        description=body.get("description") or "",
        is_active=bool(body.get("is_active", True)),
        is_template=bool(body.get("is_template", False)),
        estimated_duration_minutes=int(body.get("estimated_duration_minutes") or 60),
        require_raw_data=bool(body.get("require_raw_data", True)),
        require_calibration=bool(body.get("require_calibration", False)),
        require_reference_files=bool(body.get("require_reference_files", False)),
        optional_input_types=body.get("optional_input_types") or [],
        variables_schema=body.get("variables_schema") or [],
        created_by=request.user,
    )
    AnalysisWorkflowVersion.objects.create(workflow=wf, version_number=1, label="v1", is_published=False)
    return Response(_serialize_workflow(wf), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def workflow_detail(request, workflow_id):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    wf = get_object_or_404(AnalysisWorkflow, id=workflow_id)
    if request.method == "GET":
        return Response(_serialize_workflow(wf))
    if request.method == "DELETE":
        wf.is_active = False
        wf.save(update_fields=["is_active", "updated_at"])
        return Response({"ok": True, "disabled": True})
    body = request.data or {}
    for field in (
        "name",
        "description",
        "estimated_duration_minutes",
        "require_raw_data",
        "require_calibration",
        "require_reference_files",
        "optional_input_types",
        "variables_schema",
        "is_active",
        "is_template",
    ):
        if field in body:
            setattr(wf, field, body[field])
    wf.save()
    return Response(_serialize_workflow(wf))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def workflow_clone(request, workflow_id):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    source = get_object_or_404(AnalysisWorkflow, id=workflow_id)
    name = (request.data.get("name") or f"{source.name} (copy)").strip()
    clone = WorkflowEngine().clone_workflow(
        source, name=name, actor=request.user, as_template=bool(request.data.get("as_template"))
    )
    return Response(_serialize_workflow(clone), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def workflow_publish(request, workflow_id):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    wf = get_object_or_404(AnalysisWorkflow, id=workflow_id)
    draft = wf.versions.filter(is_published=False).order_by("-version_number").first()
    if draft is None:
        return Response({"detail": "No draft version to publish."}, status=status.HTTP_400_BAD_REQUEST)
    if not draft.steps.exists():
        return Response({"detail": "Cannot publish a workflow with no steps."}, status=status.HTTP_400_BAD_REQUEST)
    wf.versions.filter(is_published=True).update(is_published=False)
    draft.is_published = True
    draft.published_at = timezone.now()
    if not draft.label:
        draft.label = f"v{draft.version_number}"
    draft.save(update_fields=["is_published", "published_at", "label"])
    return Response(_serialize_workflow(wf))


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def workflow_steps(request, workflow_id):
    """Replace ordered steps on the draft version (creates draft if only published exists)."""
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    wf = get_object_or_404(AnalysisWorkflow, id=workflow_id)
    draft = wf.versions.filter(is_published=False).order_by("-version_number").first()
    published = wf.published_version()
    if draft is None:
        next_num = (published.version_number + 1) if published else 1
        draft = AnalysisWorkflowVersion.objects.create(
            workflow=wf,
            version_number=next_num,
            label=f"v{next_num}",
            is_published=False,
            changelog="Draft from designer",
        )
        if published:
            for s in published.steps.all():
                AnalysisWorkflowStep.objects.create(
                    version=draft,
                    step_number=s.step_number,
                    title=s.title,
                    software=s.software,
                    capability=s.capability,
                    version_constraint=s.version_constraint,
                    mandatory=s.mandatory,
                    estimated_duration_minutes=s.estimated_duration_minutes,
                    expected_output_folder=s.expected_output_folder,
                    expected_outputs=list(s.expected_outputs or []),
                    allowed_file_types=list(s.allowed_file_types or []),
                    validation_rules=dict(s.validation_rules or {}),
                    description=s.description,
                    operator_instructions=s.operator_instructions,
                    help_url=s.help_url,
                    reference_manual_url=s.reference_manual_url,
                    environment_label=s.environment_label,
                )

    steps_payload = request.data.get("steps") if isinstance(request.data, dict) else None
    if not isinstance(steps_payload, list):
        return Response({"detail": "steps array required"}, status=status.HTTP_400_BAD_REQUEST)

    draft.steps.all().delete()
    for idx, row in enumerate(steps_payload, start=1):
        AnalysisWorkflowStep.objects.create(
            version=draft,
            step_number=int(row.get("step_number") or idx),
            title=row.get("title") or "",
            software_id=row.get("software_id") or None,
            capability_id=row.get("capability_id") or None,
            version_constraint=row.get("version_constraint") or "",
            mandatory=bool(row.get("mandatory", True)),
            estimated_duration_minutes=int(row.get("estimated_duration_minutes") or 30),
            expected_output_folder=row.get("expected_output_folder") or "",
            expected_outputs=row.get("expected_outputs") or [],
            allowed_file_types=row.get("allowed_file_types") or [],
            validation_rules=row.get("validation_rules") or {},
            description=row.get("description") or "",
            operator_instructions=row.get("operator_instructions") or "",
            help_url=row.get("help_url") or "",
            reference_manual_url=row.get("reference_manual_url") or "",
            environment_label=row.get("environment_label") or "",
        )
    return Response(_serialize_workflow(wf))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def workflow_map_equipment(request, workflow_id):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    wf = get_object_or_404(AnalysisWorkflow, id=workflow_id)
    equipment_id = request.data.get("equipment_id")
    if not equipment_id:
        return Response({"detail": "equipment_id required"}, status=status.HTTP_400_BAD_REQUEST)
    mapping, _ = EquipmentAnalysisWorkflow.objects.update_or_create(
        equipment_id=equipment_id,
        workflow=wf,
        defaults={
            "is_default": bool(request.data.get("is_default", False)),
            "sort_order": int(request.data.get("sort_order") or 0),
            "button_label_override": request.data.get("button_label_override") or "",
        },
    )
    if mapping.is_default:
        EquipmentAnalysisWorkflow.objects.filter(equipment_id=equipment_id).exclude(pk=mapping.pk).update(
            is_default=False
        )
    return Response({"mapping_id": str(mapping.id), "ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def capability_list(request):
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    rows = AnalysisCapability.objects.filter(is_active=True).order_by("name")
    return Response(
        {"capabilities": [{"id": str(c.id), "name": c.name, "slug": c.slug, "description": c.description} for c in rows]}
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def workflow_ops_dashboard(request):
    """Running jobs / utilization summary for ops."""
    if not _is_staff(request.user):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    from django.db.models import Avg, Count
    from iic_booking.remote_analysis.constants import WorkflowJobStatus
    from iic_booking.remote_analysis.workflow_models import AnalysisJob

    running = AnalysisJob.objects.filter(
        status__in={
            WorkflowJobStatus.PREPARING,
            WorkflowJobStatus.ACTIVE,
            WorkflowJobStatus.PAUSED,
            WorkflowJobStatus.NEEDS_REVIEW,
        }
    ).select_related("workflow_version__workflow", "booking", "owner")[:100]

    completed = AnalysisJob.objects.filter(status=WorkflowJobStatus.COMPLETED)
    failed = AnalysisJob.objects.filter(status=WorkflowJobStatus.FAILED)
    total_finished = completed.count() + failed.count()
    success_rate = (completed.count() / total_finished * 100.0) if total_finished else None

    avg_minutes = None
    durations = []
    for job in completed.exclude(started_at=None).exclude(completed_at=None)[:500]:
        durations.append((job.completed_at - job.started_at).total_seconds() / 60.0)
    if durations:
        avg_minutes = sum(durations) / len(durations)

    return Response(
        {
            "running_workflows": [
                {
                    "job_id": str(j.id),
                    "workflow": j.workflow_version.workflow.name,
                    "booking_id": j.booking_id,
                    "current_step": j.current_step_number,
                    "status": j.status,
                    "ux_status": j.ux_status,
                    "owner": getattr(j.owner, "email", str(j.owner_id)),
                }
                for j in running
            ],
            "average_workflow_duration_minutes": round(avg_minutes, 1) if avg_minutes is not None else None,
            "workflow_success_rate_percent": round(success_rate, 1) if success_rate is not None else None,
            "completed_count": completed.count(),
            "failed_count": failed.count(),
            "by_department": list(
                AnalysisJob.objects.values("booking__user__department__name")
                .annotate(count=Count("id"))
                .order_by("-count")[:20]
            ),
        }
    )
