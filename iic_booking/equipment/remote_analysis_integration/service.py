"""Primary orchestration service — delegates to Remote Analysis reservation/session APIs."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration.audit import BookingAuditBridge
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
from iic_booking.equipment.remote_analysis_integration.notifications import BookingNotificationBridge
from iic_booking.equipment.remote_analysis_integration.timeline import BookingTimelineIntegrationService
from iic_booking.equipment.remote_analysis_integration.workspace import BookingWorkspaceFacade
from iic_booking.remote_analysis.constants import NotificationType, ReservationStatus


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
            "reservation": self._serialize_reservation(
                reservation, expose_infrastructure=expose_infrastructure
            ),
            "workspace": self._serialize_workspace(workspace),
            "session": self._serialize_session(session),
            "timeline": self.timeline.build(booking),
            "files": self.workspace.list_files(booking, limit=50) if include_files else [],
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
        except Exception:
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
                requested_capabilities=requested_capabilities or {},
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
        raw_ready = staging.has_raw_files(booking, request=request)
        require_raw = bool(settings_obj.analyze_data_require_s3_files)
        software_configured = (
            bool(workflows)
            or bool(options)
            or bool((getattr(booking.equipment, "analysis_profile", None) or "").strip())
        )
        can_launch = True
        if user is not None:
            can_launch = bool(getattr(user, "is_superuser", False) or booking.user_id == getattr(user, "pk", None))

        can_analyze = (
            elig.eligible
            and software_configured
            and (raw_ready or not require_raw)
            and can_launch
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
        from iic_booking.remote_analysis.constants import SessionStatus
        from iic_booking.remote_analysis.guacamole.authorization import find_reusable_open_session
        from iic_booking.remote_analysis.guacamole.services import GuacamoleIntegrationService
        from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator

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
            booking.analysis_session_count = int(booking.analysis_session_count or 0) + 1
            booking.analysis_last_session = timezone.now()
            booking.save(update_fields=["analysis_session_count", "analysis_last_session", "updated_at"])
            self.audit.log(booking, "Launch", details=str(session.id), actor=user)
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

