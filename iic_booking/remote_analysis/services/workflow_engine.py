"""Analysis Workflow engine — jobs, steps, checkpoints, resume, handoff."""

from __future__ import annotations

import fnmatch
import logging
import shutil
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    UX_STATUS_LABELS,
    WorkflowJobStatus,
    WorkflowJobStepStatus,
)
from iic_booking.remote_analysis.services.workspace_metadata import AnalysisWorkspaceMetadata
from iic_booking.remote_analysis.workflow_models import (
    AnalysisJob,
    AnalysisJobStep,
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)
from iic_booking.remote_analysis.workspace.storage import StorageManager

logger = logging.getLogger(__name__)

ACTIVE_JOB_STATUSES = {
    WorkflowJobStatus.PENDING,
    WorkflowJobStatus.PREPARING,
    WorkflowJobStatus.ACTIVE,
    WorkflowJobStatus.PAUSED,
    WorkflowJobStatus.NEEDS_REVIEW,
}


class WorkflowEngineError(Exception):
    def __init__(self, message: str, *, code: str = "workflow_error"):
        super().__init__(message)
        self.code = code


class WorkflowEngine:
    """Orchestrates Analysis Jobs without duplicating the reservation scheduler."""

    def __init__(self):
        self.storage = StorageManager()
        self.metadata = AnalysisWorkspaceMetadata(self.storage)

    # ------------------------------------------------------------------ resolve
    def resolve_workflow(
        self,
        equipment,
        *,
        workflow_id: str | None = None,
        prefer_workflow: bool = True,
        require_equipment_mapping: bool = True,
    ) -> tuple[AnalysisWorkflow | None, AnalysisWorkflowVersion | None, EquipmentAnalysisWorkflow | None]:
        """Return (workflow, published_version, equipment_mapping).

        When ``require_equipment_mapping`` is True (default), ``workflow_id`` must
        resolve via an enabled EquipmentAnalysisWorkflow for this equipment.
        """
        if workflow_id:
            mapping = (
                EquipmentAnalysisWorkflow.objects.select_related("workflow")
                .filter(equipment=equipment, workflow_id=workflow_id, workflow__is_active=True)
                .first()
            )
            if mapping is None:
                if require_equipment_mapping:
                    # Distinguish not-found vs unmapped for authorization responses
                    exists = AnalysisWorkflow.objects.filter(id=workflow_id).exists()
                    if not exists:
                        raise WorkflowEngineError("Workflow not found.", code="workflow_not_found")
                    raise WorkflowEngineError(
                        "This workflow is not assigned to the booking equipment.",
                        code="workflow_not_mapped",
                    )
                wf = AnalysisWorkflow.objects.filter(id=workflow_id, is_active=True).first()
                if wf is None:
                    raise WorkflowEngineError("Workflow not found.", code="workflow_not_found")
                version = wf.published_version()
                if version is None:
                    raise WorkflowEngineError("Workflow has no published version.", code="workflow_unpublished")
                return wf, version, None
            if not mapping.workflow.is_active:
                raise WorkflowEngineError("Workflow is disabled.", code="workflow_disabled")
            version = mapping.workflow.published_version()
            if version is None:
                raise WorkflowEngineError("Workflow has no published version.", code="workflow_unpublished")
            return mapping.workflow, version, mapping

        if prefer_workflow:
            mapping = (
                EquipmentAnalysisWorkflow.objects.select_related("workflow")
                .filter(equipment=equipment, workflow__is_active=True)
                .order_by("-is_default", "sort_order", "workflow__name")
                .first()
            )
            if mapping is not None:
                version = mapping.workflow.published_version()
                if version is not None:
                    return mapping.workflow, version, mapping

        return None, None, None

    def list_workflows_for_equipment(self, equipment) -> list[dict[str, Any]]:
        rows = (
            EquipmentAnalysisWorkflow.objects.select_related("workflow")
            .filter(equipment=equipment, workflow__is_active=True)
            .order_by("-is_default", "sort_order", "workflow__name")
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            wf = row.workflow
            version = wf.published_version()
            if version is None:
                continue
            steps = list(version.steps.select_related("software", "capability").order_by("step_number"))
            softwares = []
            for step in steps:
                if step.software_id:
                    softwares.append(step.software.name)
                elif step.capability_id:
                    softwares.append(f"Capability: {step.capability.name}")
            out.append(
                {
                    "id": str(wf.id),
                    "mapping_id": str(row.id),
                    "name": wf.name,
                    "slug": wf.slug,
                    "description": wf.description,
                    "estimated_duration_minutes": wf.estimated_duration_minutes
                    or sum(s.estimated_duration_minutes for s in steps),
                    "is_default": row.is_default,
                    "button_label_override": row.button_label_override,
                    "version": version.label or f"v{version.version_number}",
                    "version_id": str(version.id),
                    "required_software": softwares,
                    "steps": [self._serialize_step_def(s) for s in steps],
                    "input_requirements": {
                        "raw_data": wf.require_raw_data,
                        "calibration": wf.require_calibration,
                        "reference_files": wf.require_reference_files,
                        "optional": wf.optional_input_types or [],
                        "extra": wf.input_requirements or {},
                    },
                    "variables_schema": wf.variables_schema or [],
                }
            )
        return out

    def _serialize_step_def(self, step: AnalysisWorkflowStep) -> dict[str, Any]:
        return {
            "step_number": step.step_number,
            "title": step.display_title,
            "software": step.software.name if step.software_id else None,
            "software_id": str(step.software_id) if step.software_id else None,
            "capability": step.capability.name if step.capability_id else None,
            "mandatory": step.mandatory,
            "estimated_duration_minutes": step.estimated_duration_minutes,
            "expected_output_folder": step.folder_name,
            "expected_outputs": step.expected_outputs or [],
            "allowed_file_types": step.allowed_file_types or [],
            "description": step.description,
            "operator_instructions": step.operator_instructions,
            "help_url": step.help_url,
            "reference_manual_url": step.reference_manual_url,
            "environment_label": step.analysis_environment_label,
        }

    # ------------------------------------------------------------------ job lifecycle
    def get_active_job(self, booking) -> AnalysisJob | None:
        return (
            AnalysisJob.objects.select_related(
                "workflow_version",
                "workflow_version__workflow",
                "workspace",
                "reservation",
                "preferred_workstation",
                "owner",
            )
            .prefetch_related("steps", "steps__workflow_step", "steps__workflow_step__software")
            .filter(booking=booking, status__in=ACTIVE_JOB_STATUSES)
            .order_by("-created_at")
            .first()
        )

    def serialize_job(self, job: AnalysisJob | None, *, admin: bool = False) -> dict[str, Any] | None:
        if job is None:
            return None
        steps = list(job.steps.select_related("workflow_step", "workflow_step__software").order_by("step_number"))
        completed = sum(1 for s in steps if s.status == WorkflowJobStepStatus.COMPLETED)
        pending = sum(
            1
            for s in steps
            if s.status in {WorkflowJobStepStatus.PENDING, WorkflowJobStepStatus.READY}
        )
        current = next((s for s in steps if s.step_number == job.current_step_number), None)
        remaining = [s for s in steps if s.step_number > job.current_step_number and s.status != WorkflowJobStepStatus.SKIPPED]
        wf = job.workflow_version.workflow
        payload: dict[str, Any] = {
            "id": str(job.id),
            "status": job.status,
            "ux_status": job.ux_status or UX_STATUS_LABELS.get(job.status, job.status),
            "status_detail": job.status_detail,
            "current_step": job.current_step_number,
            "completed_steps": completed,
            "pending_steps": pending,
            "total_steps": len(steps),
            "progress_percent": int(round(100.0 * completed / len(steps))) if steps else 0,
            "same_environment": job.same_environment,
            "workflow": {
                "id": str(wf.id),
                "name": wf.name,
                "description": wf.description,
                "version": job.workflow_version.label or f"v{job.workflow_version.version_number}",
                "estimated_duration_minutes": wf.estimated_duration_minutes,
            },
            "current_environment": (current.environment_label if current else ""),
            "current_step_detail": self._serialize_job_step(current, admin=admin) if current else None,
            "remaining_steps": [
                {"step_number": s.step_number, "title": s.workflow_step.display_title, "status": s.status}
                for s in remaining
            ],
            "steps": [self._serialize_job_step(s, admin=admin) for s in steps],
            "variables": job.variables or {},
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "paused_at": job.paused_at.isoformat() if job.paused_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            "results_available": job.status == WorkflowJobStatus.COMPLETED,
            "results_label": "Processed Results Available" if job.status == WorkflowJobStatus.COMPLETED else "",
        }
        if admin and job.preferred_workstation_id:
            payload["preferred_workstation"] = job.preferred_workstation.hostname
        return payload

    def _serialize_job_step(self, step: AnalysisJobStep | None, *, admin: bool = False) -> dict[str, Any] | None:
        if step is None:
            return None
        ws = step.workflow_step
        data = {
            "id": str(step.id),
            "step_number": step.step_number,
            "title": ws.display_title,
            "status": step.status,
            "input_folder": step.input_folder,
            "output_folder": step.output_folder,
            "environment_label": step.environment_label or ws.analysis_environment_label,
            "operator_instructions": ws.operator_instructions,
            "estimated_duration_minutes": ws.estimated_duration_minutes,
            "help_url": ws.help_url,
            "reference_manual_url": ws.reference_manual_url,
            "expected_outputs": ws.expected_outputs or [],
            "mandatory": ws.mandatory,
            "verification_result": step.verification_result or {},
            "started_at": step.started_at.isoformat() if step.started_at else None,
            "completed_at": step.completed_at.isoformat() if step.completed_at else None,
            "checkpoint_at": step.checkpoint_at.isoformat() if step.checkpoint_at else None,
        }
        if admin and step.workstation_id:
            data["workstation"] = step.workstation.hostname
        return data

    @transaction.atomic
    def start_job(
        self,
        booking,
        *,
        user,
        workflow_id: str | None = None,
        variables: dict | None = None,
        workspace=None,
        reservation=None,
        prefer_workflow: bool = True,
    ) -> AnalysisJob:
        existing = self.get_active_job(booking)
        if existing is not None:
            return existing

        wf, version, _mapping = self.resolve_workflow(
            booking.equipment, workflow_id=workflow_id, prefer_workflow=prefer_workflow
        )
        if wf is None or version is None:
            raise WorkflowEngineError(
                "No analysis workflow is configured for this equipment.",
                code="no_workflow",
            )

        steps_def = list(version.steps.select_related("software", "capability").order_by("step_number"))
        if not steps_def:
            raise WorkflowEngineError("Workflow has no steps.", code="empty_workflow")

        job = AnalysisJob.objects.create(
            booking=booking,
            workflow_version=version,
            workspace=workspace,
            reservation=reservation,
            owner=user,
            status=WorkflowJobStatus.PREPARING,
            current_step_number=steps_def[0].step_number,
            variables=variables or {},
            ux_status=UX_STATUS_LABELS[WorkflowJobStatus.PREPARING],
            started_at=timezone.now(),
        )

        for step_def in steps_def:
            prior = next((s for s in steps_def if s.step_number == step_def.step_number - 1), None)
            input_folder = prior.folder_name if prior is not None else "RawData"
            AnalysisJobStep.objects.create(
                job=job,
                workflow_step=step_def,
                step_number=step_def.step_number,
                status=WorkflowJobStepStatus.READY
                if step_def.step_number == steps_def[0].step_number
                else WorkflowJobStepStatus.PENDING,
                input_folder=input_folder,
                output_folder=step_def.folder_name,
                environment_label=step_def.analysis_environment_label,
            )

        if workspace is not None:
            self.ensure_job_folders(job)
            self.metadata.write(job)

        return job

    def ensure_job_folders(self, job: AnalysisJob) -> None:
        if not job.workspace_id:
            return
        names = ["RawData", "FinalOutput", "Scratch", "Logs", "Metadata", "Processed"]
        for step in job.steps.all():
            if step.output_folder:
                names.append(step.output_folder)
            if step.input_folder and step.input_folder not in names:
                names.append(step.input_folder)
        self.storage.ensure_folders(job.workspace, names)

    def mandatory_software_names(self, job: AnalysisJob) -> list[str]:
        return self.mandatory_software_names_for_version(job.workflow_version)

    def mandatory_software_names_for_version(self, version: AnalysisWorkflowVersion) -> list[str]:
        """Software names required by mandatory steps (for same-environment allocation)."""
        names: list[str] = []
        for step_def in version.steps.select_related("software", "capability").order_by("step_number"):
            if not step_def.mandatory:
                continue
            if step_def.software_id:
                names.append(step_def.software.name)
            elif step_def.capability_id:
                from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog

                soft = (
                    AnalysisSoftwareCatalog.objects.filter(
                        is_active=True, capabilities=step_def.capability
                    )
                    .order_by("name")
                    .first()
                )
                if soft:
                    names.append(soft.name)
        return names

    def resolve_step_software(self, step_def: AnalysisWorkflowStep):
        """Return (catalog_or_None, software_requirement_or_None, software_name)."""
        from iic_booking.remote_analysis.catalog_models import AnalysisSoftwareCatalog

        if step_def.software_id:
            catalog = step_def.software
            req = catalog.ensure_software_requirement()
            return catalog, req, catalog.name
        if step_def.capability_id:
            catalog = (
                AnalysisSoftwareCatalog.objects.filter(
                    is_active=True, capabilities=step_def.capability
                )
                .order_by("name")
                .first()
            )
            if catalog is None:
                raise WorkflowEngineError(
                    f"No software advertises capability '{step_def.capability.name}'.",
                    code="capability_unresolved",
                )
            return catalog, catalog.ensure_software_requirement(), catalog.name
        raise WorkflowEngineError(
            f"Step {step_def.step_number} has neither software nor capability.",
            code="step_unconfigured",
        )

    def plan_same_environment(self, job: AnalysisJob) -> bool:
        """True if one ONLINE workstation has all mandatory softwares."""
        from iic_booking.remote_analysis.services.allocation import AllocationService

        names = self.mandatory_software_names(job)
        if len(names) <= 1:
            return True
        allocation = AllocationService()
        candidate = allocation.find_workstation_with_all_software(names)
        if candidate is not None:
            job.preferred_workstation = candidate
            job.same_environment = True
            job.save(update_fields=["preferred_workstation", "same_environment", "updated_at"])
            return True
        job.same_environment = False
        job.save(update_fields=["same_environment", "updated_at"])
        return False

    def verify_step_outputs(self, job: AnalysisJob, job_step: AnalysisJobStep) -> dict[str, Any]:
        """Check expected_outputs globs under the step output folder."""
        patterns = list(job_step.workflow_step.expected_outputs or [])
        result: dict[str, Any] = {
            "checked": bool(patterns),
            "ok": True,
            "matched": [],
            "missing": [],
        }
        if not patterns or not job.workspace_id:
            return result
        folder = self.storage.absolute_path(job.workspace, job_step.output_folder)
        if not folder.exists():
            result["ok"] = False
            result["missing"] = patterns
            return result
        files = [p.name for p in folder.rglob("*") if p.is_file()]
        for pattern in patterns:
            hits = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(f.lower(), pattern.lower())]
            if hits:
                result["matched"].append({"pattern": pattern, "files": hits[:20]})
            else:
                result["missing"].append(pattern)
        result["ok"] = len(result["missing"]) == 0
        return result

    @transaction.atomic
    def activate_step(self, job: AnalysisJob, step_number: int | None = None) -> AnalysisJobStep:
        """Mark step active and prepare folders. Allocation/launch remain with booking service."""
        n = step_number or job.current_step_number
        job_step = job.steps.select_related("workflow_step").filter(step_number=n).first()
        if job_step is None:
            raise WorkflowEngineError(f"Step {n} not found.", code="step_not_found")

        # Resume-anywhere: do not rewind completed steps
        if job_step.status == WorkflowJobStepStatus.COMPLETED:
            raise WorkflowEngineError("Step already completed; resume at the next step.", code="step_done")

        self.ensure_job_folders(job)
        job_step.status = WorkflowJobStepStatus.ACTIVE
        job_step.started_at = job_step.started_at or timezone.now()
        job_step.save(update_fields=["status", "started_at", "updated_at"])

        job.status = WorkflowJobStatus.ACTIVE
        job.current_step_number = n
        job.ux_status = UX_STATUS_LABELS[WorkflowJobStatus.ACTIVE]
        job.status_detail = f"Current step {n}: {job_step.workflow_step.display_title}"
        job.save(
            update_fields=[
                "status",
                "current_step_number",
                "ux_status",
                "status_detail",
                "updated_at",
            ]
        )
        self.metadata.write(job)
        return job_step

    @transaction.atomic
    def complete_step(
        self,
        job: AnalysisJob,
        step_number: int | None = None,
        *,
        force: bool = False,
        actor=None,
    ) -> dict[str, Any]:
        n = step_number or job.current_step_number
        job_step = job.steps.select_related("workflow_step").filter(step_number=n).first()
        if job_step is None:
            raise WorkflowEngineError(f"Step {n} not found.", code="step_not_found")

        verification = self.verify_step_outputs(job, job_step)
        job_step.verification_result = verification
        if verification.get("checked") and not verification.get("ok") and not force:
            job_step.status = WorkflowJobStepStatus.NEEDS_REVIEW
            job_step.save(update_fields=["status", "verification_result", "updated_at"])
            job.status = WorkflowJobStatus.NEEDS_REVIEW
            job.ux_status = UX_STATUS_LABELS[WorkflowJobStatus.NEEDS_REVIEW]
            job.status_detail = f"Step {n} missing expected outputs: {verification.get('missing')}"
            job.save(update_fields=["status", "ux_status", "status_detail", "updated_at"])
            self.metadata.write(job)
            return {
                "needs_review": True,
                "job": self.serialize_job(job),
                "verification": verification,
            }

        now = timezone.now()
        job_step.status = WorkflowJobStepStatus.COMPLETED
        job_step.completed_at = now
        job_step.checkpoint_at = now
        job_step.save(
            update_fields=["status", "completed_at", "checkpoint_at", "verification_result", "updated_at"]
        )
        self.metadata.write(job, extra={"checkpoint": {"step": n, "at": now.isoformat()}})

        next_step = (
            job.steps.filter(step_number__gt=n)
            .exclude(status=WorkflowJobStepStatus.SKIPPED)
            .order_by("step_number")
            .first()
        )
        if next_step is None:
            self.finalize(job)
            return {"completed": True, "job": self.serialize_job(job), "verification": verification}

        next_step.status = WorkflowJobStepStatus.READY
        next_step.save(update_fields=["status", "updated_at"])
        job.current_step_number = next_step.step_number
        job.status = WorkflowJobStatus.ACTIVE
        job.ux_status = UX_STATUS_LABELS[WorkflowJobStatus.ACTIVE]
        job.status_detail = f"Advancing to step {next_step.step_number}"
        job.save(
            update_fields=[
                "current_step_number",
                "status",
                "ux_status",
                "status_detail",
                "updated_at",
            ]
        )
        self.metadata.write(job)
        return {
            "completed": False,
            "advanced": True,
            "next_step": next_step.step_number,
            "handoff_required": not job.same_environment,
            "job": self.serialize_job(job),
            "verification": verification,
        }

    @transaction.atomic
    def skip_optional_step(self, job: AnalysisJob, step_number: int) -> AnalysisJob:
        job_step = job.steps.select_related("workflow_step").filter(step_number=step_number).first()
        if job_step is None:
            raise WorkflowEngineError("Step not found.", code="step_not_found")
        if job_step.workflow_step.mandatory:
            raise WorkflowEngineError("Cannot skip a mandatory step.", code="mandatory_step")
        job_step.status = WorkflowJobStepStatus.SKIPPED
        job_step.checkpoint_at = timezone.now()
        job_step.save(update_fields=["status", "checkpoint_at", "updated_at"])
        result = self.complete_step(job, step_number, force=True)
        return job

    @transaction.atomic
    def pause(self, job: AnalysisJob) -> AnalysisJob:
        if job.status not in ACTIVE_JOB_STATUSES:
            raise WorkflowEngineError("Job cannot be paused.", code="invalid_state")
        job.status = WorkflowJobStatus.PAUSED
        job.paused_at = timezone.now()
        job.ux_status = UX_STATUS_LABELS[WorkflowJobStatus.PAUSED]
        job.save(update_fields=["status", "paused_at", "ux_status", "updated_at"])
        self.metadata.write(job)
        return job

    @transaction.atomic
    def resume(self, job: AnalysisJob) -> AnalysisJob:
        """Resume at current (incomplete) step — never restart the workflow."""
        if job.status not in {WorkflowJobStatus.PAUSED, WorkflowJobStatus.NEEDS_REVIEW, WorkflowJobStatus.FAILED}:
            if job.status == WorkflowJobStatus.ACTIVE:
                return job
            raise WorkflowEngineError("Job cannot be resumed.", code="invalid_state")

        # Find first incomplete non-skipped step (resume-anywhere)
        target = (
            job.steps.exclude(
                status__in={WorkflowJobStepStatus.COMPLETED, WorkflowJobStepStatus.SKIPPED}
            )
            .order_by("step_number")
            .first()
        )
        if target is None:
            self.finalize(job)
            return job

        job.current_step_number = target.step_number
        job.status = WorkflowJobStatus.ACTIVE
        job.resumed_at = timezone.now()
        job.ux_status = UX_STATUS_LABELS[WorkflowJobStatus.ACTIVE]
        job.status_detail = f"Resumed at step {target.step_number}"
        job.save(
            update_fields=[
                "current_step_number",
                "status",
                "resumed_at",
                "ux_status",
                "status_detail",
                "updated_at",
            ]
        )
        target.status = WorkflowJobStepStatus.READY
        target.save(update_fields=["status", "updated_at"])
        self.activate_step(job, target.step_number)
        return job

    @transaction.atomic
    def finalize(self, job: AnalysisJob) -> AnalysisJob:
        """Promote last outputs to FinalOutput + Processed; mark job completed."""
        if job.workspace_id:
            self.ensure_job_folders(job)
            last = (
                job.steps.filter(status=WorkflowJobStepStatus.COMPLETED)
                .order_by("-step_number")
                .first()
            )
            if last and last.output_folder:
                src = self.storage.absolute_path(job.workspace, last.output_folder)
                for dest_name in ("FinalOutput", "Processed"):
                    dest = self.storage.absolute_path(job.workspace, dest_name)
                    dest.mkdir(parents=True, exist_ok=True)
                    if src.exists():
                        for item in src.iterdir():
                            target = dest / item.name
                            if item.is_dir():
                                if target.exists():
                                    shutil.rmtree(target, ignore_errors=True)
                                shutil.copytree(item, target)
                            else:
                                shutil.copy2(item, target)

        job.status = WorkflowJobStatus.COMPLETED
        job.completed_at = timezone.now()
        job.ux_status = "Processed Results Available"
        job.status_detail = "Analysis Completed"
        job.save(
            update_fields=["status", "completed_at", "ux_status", "status_detail", "updated_at"]
        )
        self.metadata.write(job)
        return job

    def copy_step_output_to_next_input(self, job: AnalysisJob, from_step: int, to_step: int) -> None:
        """Handoff helper: ensure next step input folder has prior outputs."""
        if not job.workspace_id:
            return
        src_step = job.steps.filter(step_number=from_step).first()
        dst_step = job.steps.filter(step_number=to_step).first()
        if not src_step or not dst_step:
            return
        src = self.storage.absolute_path(job.workspace, src_step.output_folder)
        # Next step input is prior output folder by design; nothing to copy if same path
        if src_step.output_folder == dst_step.input_folder:
            return
        dst = self.storage.absolute_path(job.workspace, dst_step.input_folder)
        dst.mkdir(parents=True, exist_ok=True)
        if not src.exists():
            return
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(item, target)
            else:
                shutil.copy2(item, target)

    # ------------------------------------------------------------------ templates
    @transaction.atomic
    def clone_workflow(
        self,
        source: AnalysisWorkflow,
        *,
        name: str,
        actor=None,
        as_template: bool = False,
    ) -> AnalysisWorkflow:
        clone = AnalysisWorkflow.objects.create(
            name=name,
            description=source.description,
            is_active=True,
            is_template=as_template,
            cloned_from=source,
            estimated_duration_minutes=source.estimated_duration_minutes,
            require_raw_data=source.require_raw_data,
            require_calibration=source.require_calibration,
            require_reference_files=source.require_reference_files,
            optional_input_types=list(source.optional_input_types or []),
            input_requirements=dict(source.input_requirements or {}),
            variables_schema=list(source.variables_schema or []),
            ai_assistance_enabled=source.ai_assistance_enabled,
            ai_suggested_parameters=dict(source.ai_suggested_parameters or {}),
            ai_auto_classification=dict(source.ai_auto_classification or {}),
            ai_quality_score_schema=dict(source.ai_quality_score_schema or {}),
            ai_analysis_notes_prompt=source.ai_analysis_notes_prompt,
            collaboration_enabled=False,
            created_by=actor,
        )
        src_version = source.published_version() or source.versions.order_by("-version_number").first()
        if src_version is None:
            AnalysisWorkflowVersion.objects.create(
                workflow=clone, version_number=1, label="v1", is_published=False
            )
            return clone

        new_version = AnalysisWorkflowVersion.objects.create(
            workflow=clone,
            version_number=1,
            label="v1",
            changelog=f"Cloned from {source.name} {src_version.label or src_version.version_number}",
            is_published=True,
            published_at=timezone.now(),
        )
        for step in src_version.steps.all():
            AnalysisWorkflowStep.objects.create(
                version=new_version,
                step_number=step.step_number,
                title=step.title,
                software=step.software,
                capability=step.capability,
                version_constraint=step.version_constraint,
                mandatory=step.mandatory,
                estimated_duration_minutes=step.estimated_duration_minutes,
                expected_output_folder=step.expected_output_folder,
                expected_outputs=list(step.expected_outputs or []),
                allowed_file_types=list(step.allowed_file_types or []),
                validation_rules=dict(step.validation_rules or {}),
                description=step.description,
                operator_instructions=step.operator_instructions,
                help_url=step.help_url,
                reference_manual_url=step.reference_manual_url,
                environment_label=step.environment_label,
                ai_suggested_parameters=dict(step.ai_suggested_parameters or {}),
            )
        return clone
