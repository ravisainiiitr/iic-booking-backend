"""Department Sync Agent admin — operations-focused."""

from __future__ import annotations

import json
import secrets

from django.contrib import admin, messages
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import (
    AgentAssignment,
    AgentHeartbeat,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    SyncLog,
    SyncLogSeverity,
)

from .filters import DepartmentFilter, LaboratoryFilter, LifecycleFilter, OnlineStatusFilter
from .helpers import (
    action_button,
    admin_action_buttons,
    hash_secret,
    heartbeat_age_display,
    lifecycle_badge,
    online_badge,
    warning_html,
)
from .scoping import resolve_sync_admin_scope, scope_agents
from .validation import validate_agent


@admin.register(DepartmentSyncAgent)
class DepartmentSyncAgentAdmin(admin.ModelAdmin):
    change_form_template = "admin/sync/departmentsyncagent/change_form.html"
    list_display = (
        "agent_name",
        "department",
        "laboratory",
        "machine_name",
        "version",
        "lifecycle_badge_col",
        "online_badge_col",
        "last_heartbeat_at",
        "last_reported_configuration_version",
        "last_reported_schema_version",
        "assigned_equipment_count",
        "ops_links",
    )
    list_filter = (DepartmentFilter, LaboratoryFilter, LifecycleFilter, OnlineStatusFilter, "version")
    search_fields = (
        "agent_name",
        "machine_name",
        "agent_uuid",
        "id",
        "machine_guid",
        "department__name",
        "laboratory__name",
    )
    autocomplete_fields = ("department", "laboratory")
    readonly_fields = (
        "id",
        "agent_uuid",
        "registered_at",
        "created_at",
        "updated_at",
        "last_heartbeat_at",
        "last_seen_at",
        "last_boot_at",
        "last_reported_configuration_version",
        "last_reported_schema_version",
        "computed_online_status",
        "validation_warnings_panel",
        "agent_secret_hash",
        "enrollment_token_hash",
        "agent_secret_rotated_at",
        "access_token_hash",
        "access_token_issued_at",
        "access_token_expires_at",
    )
    ordering = ("-registered_at",)
    actions = (
        "action_disable_agents",
        "action_enable_agents",
        "action_revoke_agents",
        "action_force_bootstrap_required",
        "action_generate_enrollment_secret",
        "action_rotate_secret",
    )

    fieldsets = (
        (
            _("Identity"),
            {
                "fields": (
                    "agent_name",
                    "department",
                    "laboratory",
                    "machine_name",
                    "machine_guid",
                    "version",
                    "operating_system",
                    "status",
                    "is_active",
                )
            },
        ),
        (
            _("Computed / Runtime"),
            {
                "fields": (
                    "computed_online_status",
                    "last_heartbeat_at",
                    "last_seen_at",
                    "last_boot_at",
                    "last_reported_configuration_version",
                    "last_reported_schema_version",
                    "validation_warnings_panel",
                )
            },
        ),
        (
            _("Security"),
            {
                "fields": (
                    "agent_secret_hash",
                    "agent_secret_rotated_at",
                    "enrollment_token_hash",
                    "access_token_hash",
                    "access_token_issued_at",
                    "access_token_expires_at",
                    "bootstrap_required",
                    "restart_required",
                    "upgrade_required",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("System"),
            {
                "fields": ("id", "agent_uuid", "registered_at", "created_at", "updated_at"),
                "classes": ("collapse",),
            },
        ),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("department", "laboratory")
            .annotate(
                _assigned_count=Count(
                    "assignments",
                    filter=Q(assignments__is_active=True),
                    distinct=True,
                )
            )
        )
        return scope_agents(qs, resolve_sync_admin_scope(request))

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        custom = [
            path(
                "<uuid:agent_id>/diagnostics/",
                self.admin_site.admin_view(self.diagnostics_view),
                name="%s_%s_diagnostics" % info,
            ),
            path(
                "<uuid:agent_id>/export-config/",
                self.admin_site.admin_view(self.export_configuration_view),
                name="%s_%s_export_config" % info,
            ),
            path(
                "<uuid:agent_id>/heartbeats/",
                self.admin_site.admin_view(self.heartbeat_history_redirect),
                name="%s_%s_heartbeats" % info,
            ),
            path(
                "<uuid:agent_id>/logs/",
                self.admin_site.admin_view(self.logs_redirect),
                name="%s_%s_logs" % info,
            ),
            path(
                "<uuid:agent_id>/generate-secret/",
                self.admin_site.admin_view(self.generate_secret_view),
                name="%s_%s_generate_secret" % info,
            ),
            path(
                "<uuid:agent_id>/rotate-secret/",
                self.admin_site.admin_view(self.rotate_secret_view),
                name="%s_%s_rotate_secret" % info,
            ),
        ]
        return custom + super().get_urls()

    @admin.display(description=_("Lifecycle"), ordering="status")
    def lifecycle_badge_col(self, obj):
        return lifecycle_badge(obj.status, obj.get_status_display())

    @admin.display(description=_("Online"))
    def online_badge_col(self, obj):
        return online_badge(obj.last_heartbeat_at)

    @admin.display(description=_("Assigned equipment"), ordering="_assigned_count")
    def assigned_equipment_count(self, obj):
        return getattr(obj, "_assigned_count", obj.assignments.filter(is_active=True).count())

    @admin.display(description=_("Online status"))
    def computed_online_status(self, obj):
        return online_badge(obj.last_heartbeat_at)

    @admin.display(description=_("Validation"))
    def validation_warnings_panel(self, obj):
        if not obj or not obj.pk:
            return "-"
        issues = validate_agent(obj)
        if not issues:
            return format_html('<span style="color:#28a745;">{}</span>', _("No warnings"))
        return mark_safe_join(warning_html(i.message) for i in issues)

    @admin.display(description=_("Ops"))
    def ops_links(self, obj):
        info = self.model._meta.app_label, self.model._meta.model_name
        return admin_action_buttons(
            action_button(
                reverse("admin:%s_%s_diagnostics" % info, args=[obj.pk]),
                _("Diagnostics"),
                bg="#17a2b8",
            ),
            action_button(
                reverse("admin:%s_%s_heartbeats" % info, args=[obj.pk]),
                _("Heartbeats"),
            ),
            action_button(
                reverse("admin:%s_%s_logs" % info, args=[obj.pk]),
                _("Logs"),
                bg="#6c757d",
            ),
        )

    def diagnostics_view(self, request, agent_id):
        agent = get_object_or_404(
            DepartmentSyncAgent.objects.select_related("department", "laboratory"),
            pk=agent_id,
        )
        latest_hb = (
            AgentHeartbeat.objects.filter(sync_agent=agent).order_by("-reported_at").first()
        )
        assigned = (
            AgentAssignment.objects.filter(sync_agent=agent, is_active=True)
            .select_related("sync_profile", "sync_profile__equipment")
            .order_by("assigned_at")
        )
        recent_errors = (
            SyncLog.objects.filter(
                sync_agent=agent,
                severity__in=[SyncLogSeverity.ERROR, SyncLogSeverity.CRITICAL],
            )
            .order_by("-created_at")[:20]
        )
        context = {
            **self.admin_site.each_context(request),
            "title": _("Agent diagnostics — %(name)s") % {"name": agent.agent_name},
            "opts": self.model._meta,
            "agent": agent,
            "online_badge": online_badge(agent.last_heartbeat_at),
            "lifecycle_badge": lifecycle_badge(agent.status, agent.get_status_display()),
            "heartbeat_age": heartbeat_age_display(agent.last_heartbeat_at),
            "latest_heartbeat": latest_hb,
            "assigned": assigned,
            "assigned_count": assigned.count(),
            "recent_errors": recent_errors,
            "readonly": True,
        }
        return render(request, "admin/sync/departmentsyncagent/diagnostics.html", context)

    def export_configuration_view(self, request, agent_id):
        agent = get_object_or_404(
            DepartmentSyncAgent.objects.select_related("department", "laboratory"),
            pk=agent_id,
        )
        assignments = (
            AgentAssignment.objects.filter(sync_agent=agent, is_active=True)
            .select_related("sync_profile", "sync_profile__equipment")
        )
        payload = {
            "agent_uuid": str(agent.agent_uuid),
            "agent_name": agent.agent_name,
            "department": agent.department.name if agent.department_id else None,
            "laboratory": agent.laboratory.name if agent.laboratory_id else None,
            "status": agent.status,
            "version": agent.version,
            "last_reported_configuration_version": agent.last_reported_configuration_version,
            "last_reported_schema_version": agent.last_reported_schema_version,
            "equipment": [
                {
                    "equipment_id": a.sync_profile.equipment_id,
                    "equipment_code": a.sync_profile.equipment.code,
                    "watch_folder": a.sync_profile.watch_folder,
                    "enabled_features": a.sync_profile.enabled_features,
                    "configuration_version": a.sync_profile.configuration_version,
                    "schema_version": a.sync_profile.schema_version,
                    "sync_enabled": a.sync_profile.sync_enabled,
                }
                for a in assignments
            ],
            "exported_at": timezone.now().isoformat(),
        }
        response = HttpResponse(
            json.dumps(payload, indent=2),
            content_type="application/json",
        )
        response["Content-Disposition"] = (
            f'attachment; filename="dsa-config-{agent.agent_uuid}.json"'
        )
        return response

    def heartbeat_history_redirect(self, request, agent_id):
        url = reverse("admin:sync_agentheartbeat_changelist")
        return HttpResponseRedirect(f"{url}?sync_agent__id__exact={agent_id}")

    def logs_redirect(self, request, agent_id):
        url = reverse("admin:sync_synclog_changelist")
        return HttpResponseRedirect(f"{url}?sync_agent__id__exact={agent_id}")

    def generate_secret_view(self, request, agent_id):
        agent = get_object_or_404(DepartmentSyncAgent, pk=agent_id)
        plaintext = secrets.token_urlsafe(32)
        agent.enrollment_token_hash = hash_secret(plaintext)
        agent.status = AgentLifecycleStatus.REGISTERED
        agent.save(update_fields=["enrollment_token_hash", "status", "updated_at"])
        messages.warning(
            request,
            _(
                "Enrollment secret for %(name)s (shown once): %(secret)s"
            )
            % {"name": agent.agent_name, "secret": plaintext},
        )
        return HttpResponseRedirect(
            reverse("admin:sync_departmentsyncagent_change", args=[agent.pk])
        )

    def rotate_secret_view(self, request, agent_id):
        agent = get_object_or_404(DepartmentSyncAgent, pk=agent_id)
        plaintext = secrets.token_urlsafe(32)
        agent.agent_secret_hash = hash_secret(plaintext)
        agent.agent_secret_rotated_at = timezone.now()
        agent.save(
            update_fields=["agent_secret_hash", "agent_secret_rotated_at", "updated_at"]
        )
        messages.warning(
            request,
            _("Rotated agent secret for %(name)s (shown once): %(secret)s")
            % {"name": agent.agent_name, "secret": plaintext},
        )
        return HttpResponseRedirect(
            reverse("admin:sync_departmentsyncagent_change", args=[agent.pk])
        )

    @admin.action(description=_("Disable selected agents"))
    def action_disable_agents(self, request, queryset):
        updated = queryset.update(status=AgentLifecycleStatus.DISABLED, is_active=False)
        self.message_user(request, _("Disabled %(n)s agent(s).") % {"n": updated})

    @admin.action(description=_("Enable selected agents"))
    def action_enable_agents(self, request, queryset):
        updated = queryset.exclude(status=AgentLifecycleStatus.REVOKED).update(
            status=AgentLifecycleStatus.ENROLLED,
            is_active=True,
        )
        self.message_user(request, _("Enabled %(n)s agent(s).") % {"n": updated})

    @admin.action(description=_("Revoke selected agents"))
    def action_revoke_agents(self, request, queryset):
        updated = queryset.update(status=AgentLifecycleStatus.REVOKED, is_active=False)
        self.message_user(request, _("Revoked %(n)s agent(s).") % {"n": updated})

    @admin.action(description=_("Force Bootstrap Required (bump assigned config versions)"))
    def action_force_bootstrap_required(self, request, queryset):
        bumped = 0
        for agent in queryset.prefetch_related(
            Prefetch(
                "assignments",
                queryset=AgentAssignment.objects.filter(is_active=True).select_related("sync_profile"),
            )
        ):
            agent.bootstrap_required = True
            agent.save(update_fields=["bootstrap_required", "updated_at"])
            for assignment in agent.assignments.all():
                assignment.sync_profile.bump_configuration_version()
                bumped += 1
        self.message_user(
            request,
            _("Marked bootstrap required and bumped configuration_version on %(n)s profile(s).")
            % {"n": bumped},
        )

    @admin.action(description=_("Generate enrollment secret"))
    def action_generate_enrollment_secret(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                _("Select exactly one agent to generate an enrollment secret."),
                level=messages.ERROR,
            )
            return
        agent = queryset.first()
        return HttpResponseRedirect(
            reverse("admin:sync_departmentsyncagent_generate_secret", args=[agent.pk])
        )

    @admin.action(description=_("Rotate secret"))
    def action_rotate_secret(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                _("Select exactly one agent to rotate the secret."),
                level=messages.ERROR,
            )
            return
        agent = queryset.first()
        return HttpResponseRedirect(
            reverse("admin:sync_departmentsyncagent_rotate_secret", args=[agent.pk])
        )

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        extra_context = extra_context or {}
        if object_id:
            info = self.model._meta.app_label, self.model._meta.model_name
            extra_context["sync_agent_ops_links"] = [
                (
                    reverse("admin:%s_%s_diagnostics" % info, args=[object_id]),
                    _("View diagnostics"),
                ),
                (
                    reverse("admin:%s_%s_export_config" % info, args=[object_id]),
                    _("Export configuration"),
                ),
                (
                    reverse("admin:%s_%s_generate_secret" % info, args=[object_id]),
                    _("Generate enrollment secret"),
                ),
                (
                    reverse("admin:%s_%s_rotate_secret" % info, args=[object_id]),
                    _("Rotate secret"),
                ),
                (
                    reverse("admin:%s_%s_heartbeats" % info, args=[object_id]),
                    _("View heartbeat history"),
                ),
                (
                    reverse("admin:%s_%s_logs" % info, args=[object_id]),
                    _("View logs"),
                ),
            ]
        return super().changeform_view(request, object_id, form_url, extra_context)


def mark_safe_join(parts):
    from django.utils.safestring import mark_safe

    return mark_safe("".join(str(p) for p in parts))
