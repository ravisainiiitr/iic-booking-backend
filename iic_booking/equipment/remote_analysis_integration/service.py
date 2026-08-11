"""Primary orchestration service — delegates to Remote Analysis reservation/session APIs."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration.audit import BookingAuditBridge
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
from iic_booking.equipment.remote_analysis_integration.notifications import BookingNotificationBridge
from iic_booking.equipment.remote_analysis_integration.timeline import BookingTimelineIntegrationService
from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade
from iic_booking.remote_analysis.constants import NotificationType, ReservationStatus

logger = logging.getLogger(__name__)


TERMINAL_RESERVATION = {
    ReservationStatus.COMPLETED,
    ReservationStatus.EXPIRED,
    ReservationStatus.CANCELLED,
    ReservationStatus.FAILED,
}


class BookingRemoteAnalysisService:
    def __init__(self):
        self.eligibility = BookingAnalysisEligibilityService()
        self.workspace = BookingWorkspaceFacade()
        self.notifications = BookingNotificationBridge()
        self.audit = BookingAuditBridge()
        self.timeline = BookingTimelineIntegrationService()

    def get_summary(
        self,
        booking,
        *,
        user=None,
        request=None,
        include_files: bool = True,
        expose_infrastructure: bool = False,
    ) -> dict:
        elig = self.eligibility.evaluate(booking)
        reservation = booking.analysis_reservation
        workspace = self.workspace.get_for_booking(booking)
        session = None
        if reservation:
            from iic_booking.remote_analysis.session_models import RemoteDesktopSession

            session = (
                RemoteDesktopSession.objects.filter(reservation=reservation).order_by("-created_at").first()
            )
        payload = {
            "eligibility": elig.as_dict(),
            "analysis_available": booking.analysis_available,
            "analysis_available_from": booking.analysis_available_from.isoformat()
            if booking.analysis_available_from
            else None,
            "analysis_expiry": booking.analysis_expiry.isoformat() if booking.analysis_expiry else None,
            "analysis_session_count": booking.analysis_session_count,
            "analysis_last_session": booking.analysis_last_session.isoformat()
            if booking.analysis_last_session
            else None,
            "analysis_closed_at": booking.analysis_closed_at.isoformat()
            if getattr(booking, "analysis_closed_at", None)
            else None,
            "analysis_ended": bool(getattr(booking, "analysis_closed_at", None)),
            "reservation": self._serialize_reservation(
                reservation, expose_infrastructure=expose_infrastructure
            ),
            "workspace": self._serialize_workspace(workspace),
            "workspace_id": str(workspace.id) if workspace else None,
            "virtual_booking_id": (getattr(booking, "virtual_booking_id", None) or "")
            or str(booking.booking_id),
            "session": self._serialize_session(session),
            "timeline": self.timeline.build(booking),
            "files": self.workspace.list_files(booking, limit=50) if include_files else [],
        }
        try:
            from iic_booking.equipment.remote_analysis_integration.experience import AnalysisExperienceBuilder

            payload["experience"] = AnalysisExperienceBuilder().build(
                booking,
                summary=payload,
                files=payload.get("files") or [],
            )
        except Exception:  # noqa: BLE001
            logger.exception("AnalysisExperienceBuilder failed for booking %s", booking.pk)
            payload["experience"] = {
                "virtual_booking_id": payload["virtual_booking_id"],
                "equipment_name": getattr(booking.equipment, "name", "") or "",
                "equipment_code": getattr(booking.equipment, "code", "") or "",
            }
        try:
            payload["analyze"] = self.get_analyze_context(booking, user=user, request=request)
            payload["button_label"] = payload["analyze"].get("button_label")
            payload["software_options"] = payload["analyze"].get("software_options")
            payload["workflows"] = payload["analyze"].get("workflows")
            admin = bool(
                expose_infrastructure
                or (
                    user
                    and (
                        getattr(user, "is_superuser", False)
                        or str(getattr(user, "user_type", "")).lower()
                        in {"admin", "dept_admin", "manager", "officer_in_charge", "operator"}
                    )
                )
            )
            payload["job"] = payload["analyze"].get("job")
            if payload["job"] is None:
                from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine

                payload["job"] = WorkflowEngine().serialize_job(
                    WorkflowEngine().get_active_job(booking), admin=admin
                )
            payload["raw_ready"] = payload["analyze"].get("raw_ready")
            payload["can_analyze"] = payload["analyze"].get("can_analyze")
            payload["workspace_page_title"] = "Analysis Workspace"
        except Exception:  # noqa: BLE001
            logger.exception("get_analyze_context failed for booking %s; retrying without request", booking.pk)
            try:
                payload["analyze"] = self.get_analyze_context(booking, user=user, request=None)
                payload["button_label"] = payload["analyze"].get("button_label")
                payload["software_options"] = payload["analyze"].get("software_options")
                payload["workflows"] = payload["analyze"].get("workflows")
                payload["job"] = payload["analyze"].get("job")
                payload["raw_ready"] = payload["analyze"].get("raw_ready")
                payload["can_analyze"] = payload["analyze"].get("can_analyze")
                payload["workspace_page_title"] = "Analysis Workspace"
            except Exception:  # noqa: BLE001
                logger.exception("get_analyze_context fallback also failed for booking %s", booking.pk)
                payload["analyze"] = {}
                payload["can_analyze"] = False
        return payload

    @transaction.atomic
    def on_booking_completed(self, booking, *, actor=None) -> dict:
        """Evaluate eligibility and optionally create reservation (idempotent)."""
        elig = self.eligibility.evaluate(booking)
        self.audit.log(booking, "EligibilityEvaluation", details=elig.reason, actor=actor, success=elig.eligible)
        if not elig.eligible:
            booking.analysis_available = False
            booking.save(update_fields=["analysis_available", "updated_at"])
            return {"eligible": False, "reason": elig.reason}

        hours = int(getattr(booking.equipment, "analysis_access_duration", 72) or 72)
        now = timezone.now()
        booking.analysis_available = True
        booking.analysis_available_from = booking.analysis_available_from or now
        if not booking.analysis_expiry:
            booking.analysis_expiry = now + timedelta(hours=hours)
        booking.save(
            update_fields=[
                "analysis_available",
                "analysis_available_from",
                "analysis_expiry",
                "updated_at",
            ]
        )
        self.notifications.notify(
            booking.user,
            NotificationType.RESERVATION_CONFIRMED,
            "Analysis Available",
            f"Analyze Data is available for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id},
        )
        reservation = self.ensure_reservation(booking, actor=actor)
        return {"eligible": True, "reservation_id": str(reservation.id) if reservation else None}

    @transaction.atomic
    def ensure_reservation(
        self,
        booking,
        *,
        actor=None,
        auto_allocate: bool = True,
        software_profile=None,
        mapping_id: str | None = None,
        catalog_id: str | None = None,
        software_slug: str | None = None,
        requested_capabilities: dict | None = None,
    ):
        """Idempotent AnalysisReservation creation via ReservationService."""
        elig = self.eligibility.evaluate(booking)
        if not elig.eligible:
            raise ValueError(elig.reason)

        from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
        from iic_booking.remote_analysis.services.reservation import ReservationService
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
        from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService

        existing = (
            AnalysisReservation.objects.filter(booking=booking)
            .exclude(status__in=TERMINAL_RESERVATION)
            .order_by("-created_at")
            .first()
        )
        if existing:
            self._link_booking(booking, existing)
            return existing

        if booking.analysis_reservation_id and booking.analysis_reservation.status not in TERMINAL_RESERVATION:
            return booking.analysis_reservation

        if software_profile is None:
            _, software_profile = SoftwareMappingService().resolve(
                booking.equipment,
                mapping_id=mapping_id,
                catalog_id=catalog_id,
                slug=software_slug,
            )

        # Always hard-require every catalog software mapped to this equipment.
        caps = dict(requested_capabilities or {})
        required_names = SoftwareMappingService().required_software_names(booking.equipment)
        if required_names:
            caps["required_software_names"] = required_names
            # Prefer a PC that already has the full set when multiple are needed.
            if len(required_names) > 1 and not caps.get("prefer_workstation_id"):
                from iic_booking.remote_analysis.services.allocation import AllocationService

                preferred = AllocationService().find_workstation_with_all_software(required_names)
                if preferred is not None:
                    caps["prefer_workstation_id"] = str(preferred.id)

        duration_hours = int(getattr(booking.equipment, "analysis_access_duration", 72) or 72)
        if software_profile is not None:
            catalog = getattr(software_profile, "catalog_entry", None)
            if catalog and catalog.default_session_duration_hours:
                # Cap reservation window by catalog default when shorter than equipment access
                duration_hours = min(duration_hours, int(catalog.default_session_duration_hours) or duration_hours)

        svc = ReservationService()
        start = timezone.now()
        end = start + timedelta(hours=duration_hours)
        try:
            reservation = svc.create_reservation(
                user=booking.user,
                requested_start=start,
                requested_end=end,
                booking=booking,
                created_by=actor,
                auto_allocate=auto_allocate,
                software_profile=software_profile,
                requested_capabilities=caps,
            )
        except ValueError as exc:
            # Race: another active reservation appeared
            existing = (
                AnalysisReservation.objects.filter(booking=booking)
                .exclude(status__in=TERMINAL_RESERVATION)
                .first()
            )
            if existing:
                self._link_booking(booking, existing)
                return existing
            raise

        self._link_booking(booking, reservation)
        try:
            workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=actor)
            booking.analysis_workspace = workspace
            booking.save(update_fields=["analysis_workspace", "updated_at"])
        except Exception:
            pass

        self.audit.log(booking, "ReservationCreated", details=str(reservation.id), actor=actor)
        self.notifications.notify(
            booking.user,
            NotificationType.SESSION_STARTING,
            "Analysis Ready",
            f"Analysis is ready for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id, "reservation_id": str(reservation.id)},
        )
        return reservation

    def get_analyze_context(self, booking, *, user=None, request=None) -> dict:
        """Enrichment for Analyze Data CTA (button, workflows, software options, gates)."""
        from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
        from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

        settings_obj = RemoteAnalysisSettings.get_solo()
        mapping = SoftwareMappingService()
        staging = BookingRawStagingService()
        engine = WorkflowEngine()
        elig = self.eligibility.evaluate(booking)
        options = mapping.serialize_options(booking.equipment, settings_obj=settings_obj)
        workflows = engine.list_workflows_for_equipment(booking.equipment)
        job = engine.get_active_job(booking)
        # Existence check must not depend on building absolute download URLs from request
        # (DisallowedHost / missing Host must not zero out can_analyze).
        try:
            raw_ready = staging.has_raw_files(booking, request=request)
        except Exception:  # noqa: BLE001
            logger.exception("raw_ready check failed for booking %s; retrying without request", booking.pk)
            raw_ready = staging.has_raw_files(booking, request=None)
        require_raw = bool(settings_obj.analyze_data_require_s3_files)
        software_configured = (
            bool(workflows)
            or bool(options)
            or bool((getattr(booking.equipment, "analysis_profile", None) or "").strip())
        )
        can_launch = True
        if user is not None:
            user_type = str(getattr(user, "user_type", "") or "").lower()
            can_launch = bool(
                getattr(user, "is_superuser", False)
                or getattr(user, "is_staff", False)
                or booking.user_id == getattr(user, "pk", None)
                or user_type
                in {"admin", "dept_admin", "manager", "officer_in_charge", "operator"}
            )

        can_analyze = (
            elig.eligible
            and software_configured
            and (raw_ready or not require_raw)
            and can_launch
            and not bool(getattr(booking, "analysis_closed_at", None))
        )
        reservation = booking.analysis_reservation
        queued = bool(
            reservation
            and reservation.status in {ReservationStatus.QUEUED, ReservationStatus.REQUESTED, ReservationStatus.VALIDATING}
        )
        button = mapping.button_label(booking.equipment, settings_obj=settings_obj)
        if workflows:
            default_wf = next((w for w in workflows if w.get("is_default")), workflows[0])
            if default_wf.get("button_label_override"):
                button = default_wf["button_label_override"]
        return {
            "button_label": button,
            "workspace_page_title": "Analysis Workspace",
            "software_options": options,
            "workflows": workflows,
            "job": engine.serialize_job(job),
            "software_configured": software_configured,
            "raw_ready": raw_ready,
            "require_raw_files": require_raw,
            "can_analyze": can_analyze,
            "can_launch": can_launch,
            "analysis_ended": bool(getattr(booking, "analysis_closed_at", None)),
            "analysis_closed_at": booking.analysis_closed_at.isoformat()
            if getattr(booking, "analysis_closed_at", None)
            else None,
            "queued": queued,
            "queue_message": (
                "An Analysis Environment is busy. Your request is queued and will start automatically."
                if queued
                else ""
            ),
            "prefer_workflow": bool(getattr(settings_obj, "analyze_data_prefer_workflow", True)),
        }

    def analyze_data(
        self,
        booking,
        *,
        user,
        mapping_id: str | None = None,
        catalog_id: str | None = None,
        software_slug: str | None = None,
        workflow_id: str | None = None,
        variables: dict | None = None,
        client_ip: str | None = None,
        request_absolute_uri_builder=None,
        user_agent: str = "",
        request=None,
    ) -> dict:
        """
        One-shot Analyze Data: resolve workflow (or legacy software) → allocate → stage RAW → launch.
        Never exposes workstation identity to the caller.
        """
        from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
        from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine, WorkflowEngineError
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

        settings_obj = RemoteAnalysisSettings.get_solo()
        ctx = self.get_analyze_context(booking, user=user, request=request)
        engine = WorkflowEngine()
        self.audit.log(
            booking,
            "AnalyzeDataRequested",
            details=f"workflow={workflow_id} mapping={mapping_id} catalog={catalog_id} slug={software_slug}",
            actor=user,
        )

        if not ctx["can_launch"]:
            raise SessionError("Only the booking owner may start Analyze Data.", code="forbidden")
        if ctx.get("analysis_ended") or getattr(booking, "analysis_closed_at", None):
            raise SessionError(
                "Remote analysis session is over for this booking.",
                code="analysis_ended",
            )
        if not ctx["software_configured"] and not (workflow_id or mapping_id or catalog_id or software_slug):
            raise SessionError("No analysis workflow or software is configured for this equipment.", code="no_software")
        if settings_obj.analyze_data_require_s3_files and not ctx["raw_ready"]:
            raise SessionError(
                "RAW files are not available yet. Wait until results finish synchronizing.",
                code="raw_not_ready",
            )

        prefer_workflow = bool(getattr(settings_obj, "analyze_data_prefer_workflow", True))
        job = None
        profile = None
        catalog = None
        required_software_names: list[str] = []
        prefer_workstation_id = None
        allocation_caps: dict = {}

        # Prefer workflow path when configured — require equipment mapping (S3)
        try:
            wf, version, _emap = engine.resolve_workflow(
                booking.equipment,
                workflow_id=workflow_id,
                prefer_workflow=prefer_workflow and not (mapping_id or catalog_id or software_slug),
                require_equipment_mapping=True,
            )
        except WorkflowEngineError as exc:
            if workflow_id:
                raise SessionError(str(exc), code=exc.code) from exc
            wf, version = None, None

        if wf and version:
            existing = engine.get_active_job(booking)
            if existing:
                job = existing
            step1 = version.steps.order_by("step_number").first()
            if step1 is None:
                raise SessionError("Workflow has no steps.", code="empty_workflow")
            try:
                catalog, profile, _name = engine.resolve_step_software(step1)
            except WorkflowEngineError as exc:
                raise SessionError(str(exc), code=exc.code) from exc

            # R1: evaluate same-environment preference BEFORE first allocation
            required_software_names = engine.mandatory_software_names_for_version(version)
            from iic_booking.remote_analysis.services.allocation import AllocationService

            preferred_ws = None
            if len(required_software_names) > 1:
                preferred_ws = AllocationService().find_workstation_with_all_software(
                    required_software_names
                )
            if preferred_ws is not None:
                prefer_workstation_id = preferred_ws.id
            if required_software_names:
                allocation_caps["required_software_names"] = required_software_names
            if prefer_workstation_id is not None:
                allocation_caps["prefer_workstation_id"] = str(prefer_workstation_id)
        else:
            _, profile = SoftwareMappingService().resolve(
                booking.equipment,
                mapping_id=mapping_id,
                catalog_id=catalog_id,
                slug=software_slug,
            )
            if profile is None and (mapping_id or catalog_id or software_slug):
                raise SessionError(
                    "Selected analysis software is not available for this equipment.",
                    code="bad_software",
                )

        reservation = self.ensure_reservation(
            booking,
            actor=user,
            auto_allocate=True,
            software_profile=profile,
            mapping_id=mapping_id,
            catalog_id=catalog_id,
            software_slug=software_slug,
            requested_capabilities=allocation_caps or None,
        )

        workspace = booking.analysis_workspace
        if workspace is None:
            try:
                workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=user)
                booking.analysis_workspace = workspace
                booking.save(update_fields=["analysis_workspace", "updated_at"])
            except Exception as exc:  # noqa: BLE001
                self.audit.log(booking, "WorkspaceEnsureFailed", details=str(exc), actor=user, success=False)

        if wf and version:
            try:
                job = engine.start_job(
                    booking,
                    user=user,
                    workflow_id=str(wf.id),
                    variables=variables,
                    workspace=workspace,
                    reservation=reservation,
                    prefer_workflow=True,
                )
                if prefer_workstation_id is not None:
                    from iic_booking.remote_analysis.models import AnalysisWorkstation

                    ws_obj = AnalysisWorkstation.objects.filter(id=prefer_workstation_id).first()
                    if ws_obj:
                        job.preferred_workstation = ws_obj
                        job.same_environment = True
                        job.save(
                            update_fields=["preferred_workstation", "same_environment", "updated_at"]
                        )
                else:
                    engine.plan_same_environment(job)
                engine.ensure_job_folders(job)
                engine.activate_step(job, job.current_step_number)
                job_step = job.steps.filter(step_number=job.current_step_number).first()
                if job_step and reservation.workstation_id:
                    job_step.workstation = reservation.workstation
                    job_step.environment_label = job_step.environment_label or (
                        job_step.workflow_step.analysis_environment_label
                    )
                    job_step.save(update_fields=["workstation", "environment_label", "updated_at"])
                self.audit.log(booking, "WorkflowStarted", details=str(job.id), actor=user)
            except WorkflowEngineError as exc:
                raise SessionError(str(exc), code=exc.code) from exc

        staging_result = None
        if settings_obj.analyze_data_stage_raw_on_launch and workspace is not None:
            staging_result = BookingRawStagingService().stage_into_workspace(
                booking, workspace, actor=user, request=request
            )
            self.audit.log(
                booking,
                "RawStaged",
                details=str(staging_result),
                actor=user,
                success=bool(staging_result.get("success")),
            )

        if reservation.status in {ReservationStatus.QUEUED, ReservationStatus.REQUESTED, ReservationStatus.VALIDATING}:
            self.audit.log(booking, "AnalyzeQueued", details=str(reservation.id), actor=user)
            return {
                "eligible": True,
                "queued": True,
                "status": reservation.status,
                "reservation_id": str(reservation.id),
                "message": "An Analysis Environment is busy. Your request is queued.",
                "launcher_url": f"/api/v1/bookings/{booking.booking_id}/analysis/desktop/?view=html",
                "workspace_url": f"/analysis-workspace/{booking.booking_id}",
                "staging": staging_result,
                "button_label": ctx["button_label"],
                "job": engine.serialize_job(job) if job else None,
            }

        # Two-stage allocation: hold PC until user explicitly starts the desktop.
        if reservation.status in {ReservationStatus.AWAITING_CHECKIN, ReservationStatus.RESERVED}:
            from iic_booking.remote_analysis.services.checkin import CheckinService

            if reservation.status == ReservationStatus.RESERVED:
                CheckinService().open_checkin_window(reservation, actor=user)
                reservation.refresh_from_db()
            checkin = CheckinService().checkin_payload(reservation)
            self.audit.log(booking, "AwaitingCheckin", details=str(reservation.id), actor=user)
            return {
                "eligible": True,
                "queued": False,
                "awaiting_checkin": True,
                "checkin": checkin,
                "status": reservation.status,
                "reservation_id": str(reservation.id),
                "message": "Your Analysis Environment is ready. Start the session to begin.",
                "launcher_url": f"/api/v1/bookings/{booking.booking_id}/analysis/desktop/?view=html",
                "workspace_url": f"/analysis-workspace/{booking.booking_id}",
                "staging": staging_result,
                "button_label": "Start Analysis Session",
                "job": engine.serialize_job(job) if job else None,
            }

        payload = self.launch_session(
            booking,
            user=user,
            client_ip=client_ip,
            request_absolute_uri_builder=request_absolute_uri_builder,
            user_agent=user_agent,
        )
        if job is not None:
            job_step = job.steps.filter(step_number=job.current_step_number).first()
            session_id = payload.get("session_id") or payload.get("id")
            if job_step and session_id:
                from iic_booking.remote_analysis.session_models import RemoteDesktopSession

                sess = RemoteDesktopSession.objects.filter(id=session_id).first()
                if sess:
                    job_step.session = sess
                    job_step.save(update_fields=["session", "updated_at"])
            engine.metadata.write(job)

        payload["queued"] = False
        payload["awaiting_checkin"] = False
        payload["reservation_id"] = str(reservation.id)
        payload["staging"] = staging_result
        payload["button_label"] = ctx["button_label"]
        payload["job"] = engine.serialize_job(job) if job else None
        payload["workspace_url"] = f"/analysis-workspace/{booking.booking_id}"
        payload["ux_status"] = (job.ux_status if job else "Analysis Session Active")
        # Never leak workstation details
        payload.pop("workstation_id", None)
        payload.pop("hostname", None)
        return payload

    def launch_session(
        self,
        booking,
        *,
        user,
        client_ip: str | None = None,
        request_absolute_uri_builder=None,
        user_agent: str = "",
        wait_for_prepare: bool = False,
    ) -> dict:
        """
        Create (or reuse) a remote desktop session and issue a Portal launch URL.

        Returns a dict with session fields plus additive launch_url / launcher_url.
        """
        elig = self.eligibility.evaluate(booking)
        if not elig.eligible:
            from iic_booking.remote_analysis.guacamole.session import SessionError

            self.audit.log(booking, "LaunchRejected", details=elig.reason, actor=user, success=False)
            raise SessionError(elig.reason, code="booking_ineligible")

        reservation = self.ensure_reservation(booking, actor=user)

        # Explicit check-in required before Guacamole / tunnel allocation.
        if reservation.status == ReservationStatus.AWAITING_CHECKIN:
            from iic_booking.remote_analysis.guacamole.session import SessionError
            from iic_booking.remote_analysis.services.checkin import CheckinService
            from iic_booking.remote_analysis.services.reservation import ReservationService

            if reservation.checkin_expires_at and reservation.checkin_expires_at < timezone.now():
                CheckinService().expire_due(limit=50)
                reservation.refresh_from_db()
                raise SessionError(
                    "Check-in window expired. Please request analysis again.",
                    code="checkin_expired",
                )

            ReservationService().transition(
                reservation,
                ReservationStatus.RESERVED,
                reason="User checked in — starting desktop",
                actor=user,
            )
            self.audit.log(booking, "CheckinAccepted", details=str(reservation.id), actor=user)

        from iic_booking.remote_analysis.constants import SessionStatus
        from iic_booking.remote_analysis.guacamole.authorization import find_reusable_open_session
        from iic_booking.remote_analysis.guacamole.services import GuacamoleIntegrationService
        from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
        from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

        settings_obj = RemoteAnalysisSettings.get_solo()
        workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=user)
        if getattr(settings_obj, "analyze_data_stage_raw_on_launch", True) and workspace is not None:
            try:
                BookingRawStagingService().stage_into_workspace(booking, workspace, actor=user)
            except Exception:  # noqa: BLE001
                logger.exception("RAW staging before launch failed for booking %s", booking.pk)

        orch = SessionOrchestrator()
        was_new = find_reusable_open_session(reservation, settings_obj=orch.settings) is None
        session = orch.create_session(
            reservation=reservation,
            user=user,
            client_ip=client_ip,
            wait_for_prepare=wait_for_prepare,
        )
        if session.status == SessionStatus.PREPARING:
            orch.try_advance_after_prepare(session)
            session.refresh_from_db()

        if was_new:
            # Only consume a session slot once prepare/Guacamole provisioning succeeds.
            # Failed prepare attempts (timeouts, agent blips) must not permanently
            # disable Analyze Data via analysis_session_limit.
            if session.status not in {SessionStatus.PREPARING, SessionStatus.FAILED, SessionStatus.TERMINATED}:
                booking.analysis_session_count = int(booking.analysis_session_count or 0) + 1
                booking.analysis_last_session = timezone.now()
                booking.save(update_fields=["analysis_session_count", "analysis_last_session", "updated_at"])
                self.audit.log(booking, "Launch", details=str(session.id), actor=user)
            else:
                self.audit.log(
                    booking,
                    "LaunchPending",
                    details=f"{session.id}:{session.status}",
                    actor=user,
                )
        else:
            self.audit.log(booking, "LaunchReuse", details=str(session.id), actor=user)

        launcher_url = f"/api/v1/bookings/{booking.booking_id}/analysis/desktop/?view=html"
        payload: dict = {
            "eligible": True,
            "session_id": str(session.id),
            "status": session.status,
            "launcher_url": launcher_url,
            "mock": bool(orch.settings.mock_guacamole),
        }

        if session.status == SessionStatus.FAILED:
            detail = (session.failure_detail or "Analysis Environment preparation failed.").strip()
            payload["launch_pending"] = False
            payload["detail"] = detail
            payload["failure"] = {
                "user_message": detail,
                "failure_category": "credentials"
                if "credentials" in detail.lower() or "rdp" in detail.lower()
                else "prepare",
                "failed_stage": "guacamole" if "credentials" in detail.lower() else "prepare",
            }
            return payload

        try:
            if session.status in {
                SessionStatus.TOKEN_GENERATED,
                SessionStatus.READY,
                SessionStatus.LAUNCHED,
                SessionStatus.CONNECTING,
                SessionStatus.CONNECTED,
                SessionStatus.ACTIVE,
                SessionStatus.IDLE,
            } or orch.try_advance_after_prepare(session):
                session.refresh_from_db()
                if session.status == SessionStatus.FAILED:
                    detail = (session.failure_detail or "Analysis Environment preparation failed.").strip()
                    payload["status"] = session.status
                    payload["detail"] = detail
                    payload["failure"] = {
                        "user_message": detail,
                        "failure_category": "credentials"
                        if "credentials" in detail.lower() or "rdp" in detail.lower()
                        else "prepare",
                        "failed_stage": "guacamole" if "credentials" in detail.lower() else "prepare",
                    }
                    return payload
                if request_absolute_uri_builder:
                    launch = GuacamoleIntegrationService().launch(
                        session,
                        user,
                        request_absolute_uri_builder=request_absolute_uri_builder,
                        client_ip=client_ip,
                        user_agent=user_agent,
                    )
                    payload["launch_url"] = launch.get("launch_url")
                    payload["expires_in_seconds"] = launch.get("expires_in_seconds")
                    payload["status"] = launch.get("status") or session.status
                else:
                    payload["launch_url"] = f"/api/v1/analysis/session/{session.id}/launch/"
        except Exception as exc:  # noqa: BLE001
            payload["launch_pending"] = True
            payload["detail"] = str(exc)
        return payload

    def desktop_launcher_payload(self, booking, *, user) -> dict:
        """JSON/HTML shell data for the Launch Analysis Session page."""
        admin = bool(
            user
            and (
                getattr(user, "is_superuser", False)
                or str(getattr(user, "user_type", "") or "").lower()
                in {"admin", "dept_admin", "manager", "officer_in_charge", "operator"}
            )
        )
        summary = self.get_summary(
            booking,
            user=user,
            include_files=admin or (booking.user_id == getattr(user, "pk", None)),
            expose_infrastructure=admin,
        )
        elig = summary["eligibility"]
        reservation = summary.get("reservation")
        workspace = summary.get("workspace")
        from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
        from iic_booking.remote_analysis.guacamole.settings_env import production_guacamole_configured
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

        settings_obj = RemoteAnalysisSettings.get_solo()
        guac_ok = True
        guac_status = "mock"
        try:
            if settings_obj.mock_guacamole:
                guac_status = "mock"
                guac_ok = True
            else:
                configured, problems = production_guacamole_configured(settings_obj)
                if not configured:
                    guac_status = "misconfigured"
                    guac_ok = False
                    summary["guacamole_problems"] = problems
                elif GuacamoleClient(settings_obj).health_check():
                    guac_status = "ok"
                    guac_ok = True
                else:
                    guac_status = "unreachable"
                    guac_ok = False
        except Exception:
            guac_status = "error"
            guac_ok = False

        can_show_launch = bool(
            elig.get("eligible")
            and reservation
            and reservation.get("allocated")
            and workspace
            and guac_ok
            and booking.user_id == getattr(user, "pk", None)
        )
        return {
            **summary,
            "booking_id": booking.booking_id,
            "guacamole": {"status": guac_status, "ok": guac_ok, "mock": bool(settings_obj.mock_guacamole)},
            "can_launch": can_show_launch,
            "launcher_url": f"/api/v1/bookings/{booking.booking_id}/analysis/desktop/?view=html",
            "launch_api": f"/api/v1/bookings/{booking.booking_id}/analysis/launch/",
            "show_launch_button": can_show_launch,
        }

    def archive_workspace(self, booking, *, actor=None):
        archive = self.workspace.archive(booking, actor=actor)
        booking.analysis_available = False
        booking.save(update_fields=["analysis_available", "updated_at"])
        self.audit.log(booking, "WorkspaceArchive", details=str(getattr(archive, "id", "")), actor=actor)
        self.notifications.notify(
            booking.user,
            NotificationType.WORKSPACE_SYNCED,
            "Workspace Archived",
            f"Analysis workspace archived for booking {booking.booking_id}.",
            metadata={"booking_id": booking.booking_id},
        )
        return archive

    def sync_from_reservation(self, reservation) -> None:
        booking = reservation.booking
        if not booking:
            return
        self._link_booking(booking, reservation)

    def _link_booking(self, booking, reservation) -> None:
        fields = ["analysis_reservation", "updated_at"]
        booking.analysis_reservation = reservation
        from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

        ws = AnalysisWorkspace.objects.filter(reservation=reservation).first()
        if ws:
            booking.analysis_workspace = ws
            fields.append("analysis_workspace")
        if not booking.analysis_available:
            booking.analysis_available = True
            fields.append("analysis_available")
        booking.save(update_fields=fields)

    def _serialize_reservation(self, reservation, *, expose_infrastructure: bool = False) -> dict | None:
        if not reservation:
            return None
        payload = {
            "id": str(reservation.id),
            "status": reservation.status,
            "allocated": bool(reservation.workstation_id),
            "requested_start": reservation.requested_start.isoformat() if reservation.requested_start else None,
            "requested_end": reservation.requested_end.isoformat() if reservation.requested_end else None,
        }
        # S1: never expose hostname / workstation ids to researchers
        if expose_infrastructure and reservation.workstation_id:
            payload["workstation"] = reservation.workstation.hostname
            payload["workstation_id"] = str(reservation.workstation_id)
        return payload


    def _serialize_workspace(self, workspace) -> dict | None:
        if not workspace:
            return None
        from iic_booking.remote_analysis.workspace_models import WorkspaceFile

        output_files = list(
            WorkspaceFile.objects.filter(
                workspace=workspace,
                deleted=False,
                is_current=True,
                relative_path__startswith="Processed/",
            ).values("id", "original_name", "relative_path", "size", "sha256")[:50]
        )
        return {
            "id": str(workspace.id),
            "status": workspace.status,
            "sync_phase": getattr(workspace, "sync_phase", None),
            "sync_progress_percent": getattr(workspace, "sync_progress_percent", 0),
            "sync_message": getattr(workspace, "sync_message", ""),
            "usage_bytes": workspace.current_usage_bytes,
            "quota_gb": workspace.quota_gb,
            "archived_at": workspace.archived_at.isoformat() if workspace.archived_at else None,
            "output_files": [
                {
                    "id": str(f["id"]),
                    "name": f["original_name"],
                    "relative_path": f["relative_path"],
                    "size": f["size"],
                    "sha256": f["sha256"],
                }
                for f in output_files
            ],
        }

    def _serialize_session(self, session) -> dict | None:
        if not session:
            return None
        return {
            "id": str(session.id),
            "status": session.status,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "launch_time": session.launch_time.isoformat() if getattr(session, "launch_time", None) else None,
        }

    def get_analysis_job(self, booking, *, user=None) -> dict | None:
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine

        admin = bool(user and (getattr(user, "is_superuser", False) or str(getattr(user, "user_type", "")).lower() in {
            "admin", "dept_admin", "manager", "officer_in_charge", "operator"
        }))
        return WorkflowEngine().serialize_job(WorkflowEngine().get_active_job(booking), admin=admin)

    def complete_analysis_step(self, booking, *, user, step_number: int | None = None, force: bool = False) -> dict:
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine, WorkflowEngineError

        engine = WorkflowEngine()
        job = engine.get_active_job(booking)
        if job is None:
            raise SessionError("No active Analysis Job.", code="no_job")
        if booking.user_id != getattr(user, "pk", None) and not getattr(user, "is_superuser", False):
            raise SessionError("Only the booking owner may complete a step.", code="forbidden")
        try:
            completed_n = step_number or job.current_step_number
            result = engine.complete_step(job, completed_n, force=force, actor=user)
        except WorkflowEngineError as exc:
            raise SessionError(str(exc), code=exc.code) from exc

        self.audit.log(booking, "StepCompleted", details=str(result), actor=user)

        # Handoff: if advanced and not same environment, allocate + launch for next step
        if result.get("advanced") and result.get("handoff_required"):
            next_n = result.get("next_step")
            job.refresh_from_db()
            next_job_step = job.steps.select_related("workflow_step").filter(step_number=next_n).first()
            if next_job_step:
                engine.copy_step_output_to_next_input(job, completed_n, next_n)
                catalog, profile, _ = engine.resolve_step_software(next_job_step.workflow_step)
                # New reservation for next environment — complete prior if needed, then create
                from iic_booking.remote_analysis.constants import ReservationStatus as RS
                from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

                prior = booking.analysis_reservation
                if prior and prior.status not in TERMINAL_RESERVATION:
                    prior.status = RS.COMPLETED
                    prior.save(update_fields=["status", "updated_at"])
                    booking.analysis_reservation = None
                    booking.save(update_fields=["analysis_reservation", "updated_at"])

                reservation = self.ensure_reservation(
                    booking, actor=user, auto_allocate=True, software_profile=profile
                )
                job.reservation = reservation
                job.save(update_fields=["reservation", "updated_at"])
                engine.activate_step(job, next_n)
                result["job"] = engine.serialize_job(job)
                result["handoff"] = {"reservation_id": str(reservation.id), "status": reservation.status}
                self.audit.log(booking, "WorkflowHandoff", details=f"step={next_n}", actor=user)

        if result.get("completed"):
            self.audit.log(booking, "WorkflowCompleted", details=str(job.id), actor=user)
        return result

    def release_checkin(self, booking, *, user, reason: str = "Released by user") -> dict:
        """User declines a reserved Analysis PC before starting the desktop."""
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.services.checkin import CheckinService

        reservation = booking.analysis_reservation
        if reservation is None or reservation.status != ReservationStatus.AWAITING_CHECKIN:
            raise SessionError("No reserved Analysis Environment awaiting check-in.", code="no_checkin")
        result = CheckinService().release_checkin(reservation, actor=user, reason=reason)
        self.audit.log(booking, "CheckinReleased", details=str(result), actor=user)
        return {"ok": True, **result}

    def start_checked_in_session(
        self,
        booking,
        *,
        user,
        client_ip: str | None = None,
        request_absolute_uri_builder=None,
        user_agent: str = "",
    ) -> dict:
        """Explicit Start Analysis Session after reservation check-in."""
        from iic_booking.remote_analysis.guacamole.session import SessionError

        reservation = booking.analysis_reservation
        if reservation is None or reservation.status != ReservationStatus.AWAITING_CHECKIN:
            raise SessionError(
                "No Analysis Environment is reserved for check-in. Request analysis first.",
                code="no_checkin",
            )
        if reservation.checkin_expires_at and reservation.checkin_expires_at < timezone.now():
            from iic_booking.remote_analysis.services.checkin import CheckinService

            CheckinService().expire_due(limit=5)
            raise SessionError("Check-in window expired. Please request analysis again.", code="checkin_expired")
        return self.launch_session(
            booking,
            user=user,
            client_ip=client_ip,
            request_absolute_uri_builder=request_absolute_uri_builder,
            user_agent=user_agent,
        )

    def end_analysis(self, booking, *, user, reason: str = "Finished early by user") -> dict:
        """
        Researcher finish-early: terminate open session (if any), release reservation,
        free workstation, collect/upload results, close reverse tunnel, drain queue.
        """
        from iic_booking.remote_analysis.constants import WorkstationStatus
        from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
        from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
        from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
        from iic_booking.remote_analysis.services.reservation import ReservationService
        from iic_booking.remote_analysis.services.scheduler import SchedulerService
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        if booking.user_id != getattr(user, "pk", None) and not getattr(user, "is_superuser", False):
            raise SessionError("Only the booking owner may end analysis.", code="forbidden")

        reason = (reason or "Finished early by user").strip()[:512]
        session = (
            RemoteDesktopSession.objects.filter(
                booking_id=booking.pk,
                status__in=OPEN_SESSION_STATUSES,
            )
            .order_by("-created_at")
            .first()
        )
        if session is None and booking.analysis_reservation_id:
            session = (
                RemoteDesktopSession.objects.filter(
                    reservation_id=booking.analysis_reservation_id,
                    status__in=OPEN_SESSION_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )

        session_ended = False
        reservation_released = False
        try:
            if session is not None:
                SessionOrchestrator().terminate(session, user=user, reason=reason)
                session_ended = True
                reservation_released = True
            else:
                reservation = booking.analysis_reservation
                if reservation is None:
                    reservation = (
                        AnalysisReservation.objects.filter(booking_id=booking.pk)
                        .exclude(status__in=TERMINAL_RESERVATION)
                        .order_by("-created_at")
                        .first()
                    )
                if reservation and reservation.status not in TERMINAL_RESERVATION:
                    ws = reservation.workstation
                    ReservationService().release(
                        reservation,
                        actor=user,
                        reason=reason,
                        final_status=ReservationStatus.COMPLETED,
                    )
                    reservation_released = True
                    if ws is not None and ws.status not in {
                        WorkstationStatus.DISABLED,
                        WorkstationStatus.MAINTENANCE,
                    }:
                        SchedulerService()._free_workstation(reservation)
                        ws.refresh_from_db()
                        if ws.status == WorkstationStatus.BUSY:
                            from iic_booking.remote_analysis.models import WorkstationStateHistory

                            WorkstationStateHistory.objects.create(
                                workstation=ws,
                                from_status=ws.status,
                                to_status=WorkstationStatus.AVAILABLE,
                                reason=f"Early end analysis: {reason}"[:500],
                            )
                            ws.status = WorkstationStatus.AVAILABLE
                            ws.save(update_fields=["status", "updated_at"])

            if not session_ended and not reservation_released:
                raise SessionError("No active analysis session or reservation to end.", code="nothing_to_end")

            queue_stats = {}
            try:
                queue_stats = SchedulerService().process_queue() or {}
            except Exception:
                logger.exception("process_queue failed after end_analysis booking=%s", booking.pk)

            self.audit.log(
                booking,
                "AnalysisEndedEarly",
                details=(
                    f"session_ended={session_ended}; "
                    f"reservation_released={reservation_released}; queue={queue_stats}"
                ),
                actor=user,
            )
            return {
                "ok": True,
                "session_ended": session_ended,
                "reservation_released": reservation_released,
                "session_id": str(session.id) if session is not None else None,
                "queue": queue_stats,
                "reason": reason,
            }
        except SessionError:
            raise
        except Exception as exc:
            logger.exception("end_analysis failed booking=%s", booking.pk)
            raise SessionError(
                "Could not end analysis cleanly. Please try again or contact support.",
                code="end_failed",
            ) from exc

    def extend_analysis(self, booking, *, user) -> dict:
        """Extend active session when nobody else is waiting."""
        from datetime import timedelta

        from django.utils import timezone

        from iic_booking.remote_analysis.constants import QueueEntryStatus
        from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.scheduler_models import ReservationQueue
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        if booking.user_id != getattr(user, "pk", None) and not getattr(user, "is_superuser", False):
            raise SessionError("Only the booking owner may extend analysis.", code="forbidden")

        session = (
            RemoteDesktopSession.objects.filter(booking_id=booking.pk, status__in=OPEN_SESSION_STATUSES)
            .order_by("-created_at")
            .first()
        )
        if session is None and booking.analysis_reservation_id:
            session = (
                RemoteDesktopSession.objects.filter(
                    reservation_id=booking.analysis_reservation_id,
                    status__in=OPEN_SESSION_STATUSES,
                )
                .order_by("-created_at")
                .first()
            )
        if session is None:
            raise SessionError("No active analysis session to extend.", code="no_session")

        others = ReservationQueue.objects.filter(status=QueueEntryStatus.WAITING).exclude(
            reservation_id=session.reservation_id
        )
        if others.exists():
            raise SessionError(
                "Another analysis request is currently waiting. "
                "Session extension is unavailable to ensure fair access.",
                code="queue_blocked",
            )

        minutes = int(getattr(booking.equipment, "analysis_extension_minutes", None) or 15)
        base = session.expires_at if session.expires_at and session.expires_at > timezone.now() else timezone.now()
        session.expires_at = base + timedelta(minutes=max(1, minutes))
        session.save(update_fields=["expires_at", "updated_at"])
        self.audit.log(
            booking,
            "AnalysisExtended",
            details=f"+{minutes}m expires_at={session.expires_at.isoformat()}",
            actor=user,
        )
        return {
            "ok": True,
            "extended_minutes": minutes,
            "expires_at": session.expires_at.isoformat(),
            "remaining_seconds": max(0, int((session.expires_at - timezone.now()).total_seconds())),
            "message": "No users are waiting. Your analysis session has been extended.",
        }

    def upload_past_data(self, booking, *, user, uploaded_file, folder: str = "RawData") -> dict:
        """Upload extra/past files into workspace RawData and sync to agent Input when possible."""
        from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
        from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager

        if booking.user_id != getattr(user, "pk", None) and not getattr(user, "is_superuser", False):
            raise SessionError("Only the booking owner may upload analysis files.", code="forbidden")
        if getattr(booking, "analysis_closed_at", None):
            raise SessionError(
                "Remote analysis session is over for this booking.",
                code="analysis_ended",
            )

        workspace = self.workspace.get_for_booking(booking)
        if workspace is None:
            # Ensure reservation + portal workspace so uploads work before Start Analysis.
            try:
                reservation = self.ensure_reservation(booking, actor=user, auto_allocate=False)
            except Exception as exc:  # noqa: BLE001
                raise SessionError(
                    "No analysis workspace for this booking yet. Start Analyze Data once, then upload.",
                    code="no_workspace",
                ) from exc
            try:
                workspace = WorkspaceSyncService().ensure_for_reservation(
                    reservation, actor=user, ingest=False
                )
                self._link_booking(booking, reservation)
            except Exception as exc:  # noqa: BLE001
                raise SessionError(
                    "Could not create analysis workspace for uploads.",
                    code="no_workspace",
                ) from exc

        # Bind workstation from reservation / open session so late sync can reach the agent.
        if not workspace.workstation_id:
            ws = None
            reservation = getattr(workspace, "reservation", None) or booking.analysis_reservation
            if reservation is not None and getattr(reservation, "workstation_id", None):
                ws = reservation.workstation
            if ws is None:
                open_session = (
                    RemoteDesktopSession.objects.filter(
                        booking_id=booking.pk,
                        status__in=OPEN_SESSION_STATUSES,
                    )
                    .select_related("workstation")
                    .order_by("-created_at")
                    .first()
                )
                if open_session is not None:
                    ws = open_session.workstation
            if ws is not None:
                workspace.workstation = ws
                workspace.save(update_fields=["workstation", "updated_at"])

        folder = (folder or "RawData").strip() or "RawData"
        try:
            file_row = TransferManager().upload(
                workspace,
                uploaded_file,
                folder=folder,
                actor=user,
                source="portal",
            )
        except TransferError as exc:
            raise SessionError(str(exc), code=getattr(exc, "code", "upload_failed")) from exc

        sync_command_id = None
        if workspace.workstation_id:
            try:
                cmd = WorkspaceSyncService().issue_sync_command(workspace, actor=user)
                sync_command_id = str(getattr(cmd, "id", "") or "") or None
            except Exception as exc:  # noqa: BLE001
                self.audit.log(
                    booking,
                    "PastDataSyncDeferred",
                    details=str(exc),
                    actor=user,
                    success=False,
                )
        else:
            self.audit.log(
                booking,
                "PastDataSyncDeferred",
                details="No workstation assigned yet; file will sync on prepare/launch",
                actor=user,
                success=True,
            )

        self.audit.log(
            booking,
            "PastDataUploaded",
            details=f"{file_row.relative_path}; sync={sync_command_id}",
            actor=user,
        )
        return {
            "ok": True,
            "workspace_id": str(workspace.id),
            "file": {
                "id": str(file_row.id),
                "name": file_row.original_name or file_row.relative_path,
                "relative_path": file_row.relative_path,
                "size": file_row.size,
            },
            "sync_command_id": sync_command_id,
            "agent_folder": "Input" if folder.replace("\\", "/").split("/")[0] in {"RawData", "Metadata"} else folder,
        }

    def pause_analysis_job(self, booking, *, user) -> dict:
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine, WorkflowEngineError

        engine = WorkflowEngine()
        job = engine.get_active_job(booking)
        if job is None:
            raise SessionError("No active Analysis Job.", code="no_job")
        try:
            engine.pause(job)
        except WorkflowEngineError as exc:
            raise SessionError(str(exc), code=exc.code) from exc
        self.audit.log(booking, "WorkflowPaused", details=str(job.id), actor=user)
        return {"job": engine.serialize_job(job)}

    def resume_analysis_job(self, booking, *, user, client_ip=None, request_absolute_uri_builder=None, user_agent="") -> dict:
        from iic_booking.remote_analysis.guacamole.session import SessionError
        from iic_booking.remote_analysis.services.workflow_engine import WorkflowEngine, WorkflowEngineError

        engine = WorkflowEngine()
        job = engine.get_active_job(booking)
        if job is None:
            raise SessionError("No active Analysis Job.", code="no_job")
        try:
            engine.resume(job)
        except WorkflowEngineError as exc:
            raise SessionError(str(exc), code=exc.code) from exc
        self.audit.log(booking, "WorkflowResumed", details=str(job.id), actor=user)
        payload = self.launch_session(
            booking,
            user=user,
            client_ip=client_ip,
            request_absolute_uri_builder=request_absolute_uri_builder,
            user_agent=user_agent,
        )
        payload["job"] = engine.serialize_job(job)
        payload.pop("workstation_id", None)
        payload.pop("hostname", None)
        return payload

