"""Equipment Sync Profile admin."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.db.models import Prefetch
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent, EquipmentSyncProfile

from .filters import ProfileAgentFilter, ProfileDepartmentFilter
from .helpers import warning_html
from .scoping import resolve_sync_admin_scope, scope_profiles
from .validation import validate_sync_profile


class AssignAgentForm(forms.Form):
    sync_agent = forms.ModelChoiceField(
        queryset=DepartmentSyncAgent.objects.order_by("agent_name"),
        label=_("Agent"),
    )


@admin.register(EquipmentSyncProfile)
class EquipmentSyncProfileAdmin(admin.ModelAdmin):
    list_display = (
        "equipment",
        "department_col",
        "assigned_agent_col",
        "watch_folder",
        "upload_enabled_col",
        "configuration_version",
        "schema_version",
        "sync_enabled",
        "validation_col",
    )
    list_filter = (ProfileDepartmentFilter, ProfileAgentFilter, "sync_enabled", "upload_enabled")
    search_fields = (
        "equipment__name",
        "equipment__code",
        "watch_folder",
        "hostname",
        "share_name",
        "unc_path",
        "id",
    )
    autocomplete_fields = ("equipment",)
    readonly_fields = ("id", "created_at", "updated_at", "validation_warnings_panel", "assigned_agent_readonly")
    ordering = ("equipment__code",)
    actions = (
        "action_increment_configuration_version",
        "action_enable_sync",
        "action_disable_sync",
        "action_assign_agent",
        "action_remove_agent",
        "action_clone_configuration",
    )

    fieldsets = (
        (
            _("Equipment"),
            {"fields": ("equipment", "assigned_agent_readonly", "sync_enabled")},
        ),
        (
            _("Network / paths"),
            {
                "fields": (
                    "hostname",
                    "ip_address",
                    "share_name",
                    "unc_path",
                    "watch_folder",
                    "sync_interval_seconds",
                )
            },
        ),
        (
            _("Features"),
            {
                "fields": (
                    "enabled_features",
                    "watch_enabled",
                    "upload_enabled",
                )
            },
        ),
        (
            _("Versioning"),
            {"fields": ("configuration_version", "schema_version")},
        ),
        (
            _("Credentials"),
            {"fields": ("smb_credential_reference", "smb_username", "notes")},
        ),
        (
            _("Validation"),
            {"fields": ("validation_warnings_panel",)},
        ),
        (
            _("System"),
            {"fields": ("id", "created_at", "updated_at"), "classes": ("collapse",)},
        ),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related("equipment", "equipment__internal_department")
            .prefetch_related(
                Prefetch(
                    "assignments",
                    queryset=AgentAssignment.objects.filter(is_active=True).select_related(
                        "sync_agent",
                        "sync_agent__laboratory",
                    ),
                )
            )
        )
        return scope_profiles(qs, resolve_sync_admin_scope(request))

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "assign-agent/",
                self.admin_site.admin_view(self.assign_agent_view),
                name="sync_equipmentsyncprofile_assign_agent",
            ),
        ]
        return custom + urls

    def _active_assignment(self, obj):
        for assignment in obj.assignments.all():
            if assignment.is_active:
                return assignment
        return None

    @admin.display(description=_("Department"))
    def department_col(self, obj):
        dept = getattr(obj.equipment, "internal_department", None)
        return dept.name if dept else "-"

    @admin.display(description=_("Assigned agent"))
    def assigned_agent_col(self, obj):
        assignment = self._active_assignment(obj)
        return assignment.sync_agent.agent_name if assignment else format_html(
            '<span style="color:#dc3545;">{}</span>', _("None")
        )

    @admin.display(description=_("Upload"), boolean=True)
    def upload_enabled_col(self, obj):
        features = obj.enabled_features or {}
        if "upload" in features:
            return bool(features.get("upload"))
        return obj.upload_enabled

    @admin.display(description=_("Validation"))
    def validation_col(self, obj):
        issues = validate_sync_profile(obj)
        if not issues:
            return format_html('<span style="color:#28a745;">OK</span>')
        return format_html(
            '<span style="color:#856404;font-weight:bold;">{}</span>',
            _("%(n)s warning(s)") % {"n": len(issues)},
        )

    @admin.display(description=_("Validation warnings"))
    def validation_warnings_panel(self, obj):
        if not obj or not obj.pk:
            return "-"
        issues = validate_sync_profile(obj)
        if not issues:
            return format_html('<span style="color:#28a745;">{}</span>', _("No warnings"))
        return mark_safe("".join(str(warning_html(i.message)) for i in issues))

    @admin.display(description=_("Assigned agent"))
    def assigned_agent_readonly(self, obj):
        assignment = self._active_assignment(obj) if obj and obj.pk else None
        if not assignment:
            return _("None")
        return f"{assignment.sync_agent.agent_name} ({assignment.sync_agent.agent_uuid})"

    @admin.action(description=_("Increment configuration version"))
    def action_increment_configuration_version(self, request, queryset):
        for profile in queryset:
            profile.bump_configuration_version()
        self.message_user(
            request,
            _("Incremented configuration_version for %(n)s profile(s).") % {"n": queryset.count()},
        )

    @admin.action(description=_("Enable sync"))
    def action_enable_sync(self, request, queryset):
        updated = queryset.update(sync_enabled=True)
        self.message_user(request, _("Enabled sync on %(n)s profile(s).") % {"n": updated})

    @admin.action(description=_("Disable sync"))
    def action_disable_sync(self, request, queryset):
        updated = queryset.update(sync_enabled=False)
        self.message_user(request, _("Disabled sync on %(n)s profile(s).") % {"n": updated})

    @admin.action(description=_("Remove agent assignment"))
    def action_remove_agent(self, request, queryset):
        now = timezone.now()
        updated = AgentAssignment.objects.filter(
            sync_profile__in=queryset,
            is_active=True,
        ).update(is_active=False, unassigned_at=now)
        self.message_user(request, _("Deactivated %(n)s assignment(s).") % {"n": updated})

    @admin.action(description=_("Assign agent…"))
    def action_assign_agent(self, request, queryset):
        selected = list(queryset.values_list("pk", flat=True))
        request.session["sync_assign_profile_ids"] = [str(pk) for pk in selected]
        return HttpResponseRedirect(reverse("admin:sync_equipmentsyncprofile_assign_agent"))

    @admin.action(description=_("Clone configuration"))
    def action_clone_configuration(self, request, queryset):
        if queryset.count() != 1:
            self.message_user(
                request,
                _("Select exactly one profile to clone. Choose the target equipment on the next screen later; "
                  "for now clone creates a message with source details."),
                level=messages.WARNING,
            )
            return
        source = queryset.select_related("equipment").first()
        self.message_user(
            request,
            _(
                "Clone source ready: %(code)s — watch=%(watch)s features=%(features)s. "
                "Create a new profile for the target equipment and copy these values "
                "(dedicated clone wizard can be added when needed)."
            )
            % {
                "code": source.equipment.code,
                "watch": source.watch_folder or "-",
                "features": source.enabled_features,
            },
            level=messages.INFO,
        )

    def assign_agent_view(self, request):
        ids = request.session.get("sync_assign_profile_ids") or []
        profiles = EquipmentSyncProfile.objects.filter(pk__in=ids).select_related("equipment")
        if request.method == "POST":
            form = AssignAgentForm(request.POST)
            if form.is_valid():
                agent = form.cleaned_data["sync_agent"]
                now = timezone.now()
                # Deactivate existing active assignments for these profiles.
                AgentAssignment.objects.filter(sync_profile__in=profiles, is_active=True).update(
                    is_active=False,
                    unassigned_at=now,
                )
                created = 0
                for profile in profiles:
                    AgentAssignment.objects.create(
                        sync_agent=agent,
                        sync_profile=profile,
                        is_active=True,
                    )
                    profile.bump_configuration_version()
                    created += 1
                request.session.pop("sync_assign_profile_ids", None)
                self.message_user(
                    request,
                    _("Assigned %(agent)s to %(n)s profile(s).")
                    % {"agent": agent.agent_name, "n": created},
                )
                return HttpResponseRedirect(reverse("admin:sync_equipmentsyncprofile_changelist"))
        else:
            form = AssignAgentForm()
        context = {
            **self.admin_site.each_context(request),
            "title": _("Assign agent to equipment sync profiles"),
            "opts": self.model._meta,
            "form": form,
            "profiles": profiles,
        }
        return render(request, "admin/sync/equipmentsyncprofile/assign_agent.html", context)
