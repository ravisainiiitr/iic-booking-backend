"""Agent Assignment admin."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent

from .filters import AgentDepartmentFilter
from .helpers import color_badge
from .scoping import resolve_sync_admin_scope, scope_by_agent_department


class ReplaceAgentForm(forms.Form):
    sync_agent = forms.ModelChoiceField(
        queryset=DepartmentSyncAgent.objects.order_by("agent_name"),
        label=_("Replacement agent"),
    )


class MoveEquipmentForm(forms.Form):
    sync_agent = forms.ModelChoiceField(
        queryset=DepartmentSyncAgent.objects.order_by("agent_name"),
        label=_("Target agent"),
    )


@admin.register(AgentAssignment)
class AgentAssignmentAdmin(admin.ModelAdmin):
    list_display = (
        "equipment_col",
        "sync_agent",
        "laboratory_col",
        "assigned_at",
        "status_badge",
    )
    list_filter = (AgentDepartmentFilter, "is_active", "assigned_at")
    search_fields = (
        "sync_profile__equipment__name",
        "sync_profile__equipment__code",
        "sync_agent__agent_name",
        "sync_agent__equipment__name",
        "sync_agent__equipment__code",
    )
    autocomplete_fields = ("sync_agent", "sync_profile")
    readonly_fields = ("id", "assigned_at", "unassigned_at", "created_at", "updated_at")
    ordering = ("-assigned_at",)
    actions = (
        "action_deactivate_assignment",
        "action_replace_agent",
        "action_move_equipment",
        "action_assign_multiple_hint",
    )

    fieldsets = (
        (
            _("Assignment"),
            {"fields": ("sync_agent", "sync_profile", "is_active", "notes")},
        ),
        (
            _("Timestamps"),
            {"fields": ("assigned_at", "unassigned_at", "created_at", "updated_at", "id")},
        ),
    )

    def get_queryset(self, request):
        qs = (
            super()
            .get_queryset(request)
            .select_related(
                "sync_agent",
                "sync_agent__equipment",
                "sync_agent__department",
                "sync_profile",
                "sync_profile__equipment",
                "sync_profile__equipment__internal_department",
            )
        )
        return scope_by_agent_department(qs, resolve_sync_admin_scope(request))

    def get_urls(self):
        return [
            path(
                "replace-agent/",
                self.admin_site.admin_view(self.replace_agent_view),
                name="sync_agentassignment_replace_agent",
            ),
            path(
                "move-equipment/",
                self.admin_site.admin_view(self.move_equipment_view),
                name="sync_agentassignment_move_equipment",
            ),
        ] + super().get_urls()

    @admin.display(description=_("Equipment"), ordering="sync_profile__equipment__code")
    def equipment_col(self, obj):
        return obj.sync_profile.equipment

    @admin.display(description=_("Primary equipment"))
    def laboratory_col(self, obj):
        eq = obj.sync_agent.equipment
        return f"{eq.code} — {eq.name}" if eq else "-"

    @admin.display(description=_("Status"))
    def status_badge(self, obj):
        if obj.is_active:
            return color_badge(_("Active"), "#28a745")
        return color_badge(_("Inactive"), "#6c757d")

    @admin.action(description=_("Deactivate assignment"))
    def action_deactivate_assignment(self, request, queryset):
        updated = queryset.filter(is_active=True).update(
            is_active=False,
            unassigned_at=timezone.now(),
        )
        self.message_user(request, _("Deactivated %(n)s assignment(s).") % {"n": updated})

    @admin.action(description=_("Replace agent…"))
    def action_replace_agent(self, request, queryset):
        request.session["sync_replace_assignment_ids"] = [str(pk) for pk in queryset.values_list("pk", flat=True)]
        return HttpResponseRedirect(reverse("admin:sync_agentassignment_replace_agent"))

    @admin.action(description=_("Move equipment…"))
    def action_move_equipment(self, request, queryset):
        request.session["sync_move_assignment_ids"] = [str(pk) for pk in queryset.values_list("pk", flat=True)]
        return HttpResponseRedirect(reverse("admin:sync_agentassignment_move_equipment"))

    @admin.action(description=_("Assign multiple equipment…"))
    def action_assign_multiple_hint(self, request, queryset):
        self.message_user(
            request,
            _(
                "To assign multiple equipment: open Equipment Sync Profiles, "
                "select profiles, then use the “Assign agent…” action."
            ),
            level=messages.INFO,
        )

    def replace_agent_view(self, request):
        ids = request.session.get("sync_replace_assignment_ids") or []
        assignments = AgentAssignment.objects.filter(pk__in=ids).select_related(
            "sync_profile__equipment",
            "sync_agent",
        )
        if request.method == "POST":
            form = ReplaceAgentForm(request.POST)
            if form.is_valid():
                new_agent = form.cleaned_data["sync_agent"]
                now = timezone.now()
                replaced = 0
                for assignment in assignments:
                    if assignment.is_active:
                        assignment.is_active = False
                        assignment.unassigned_at = now
                        assignment.save(update_fields=["is_active", "unassigned_at", "updated_at"])
                    AgentAssignment.objects.create(
                        sync_agent=new_agent,
                        sync_profile=assignment.sync_profile,
                        is_active=True,
                        notes=_("Replaced from %(old)s") % {"old": assignment.sync_agent.agent_name},
                    )
                    assignment.sync_profile.bump_configuration_version()
                    replaced += 1
                request.session.pop("sync_replace_assignment_ids", None)
                self.message_user(
                    request,
                    _("Replaced agent on %(n)s assignment(s).") % {"n": replaced},
                )
                return HttpResponseRedirect(reverse("admin:sync_agentassignment_changelist"))
        else:
            form = ReplaceAgentForm()
        context = {
            **self.admin_site.each_context(request),
            "title": _("Replace agent"),
            "opts": self.model._meta,
            "form": form,
            "assignments": assignments,
        }
        return render(request, "admin/sync/agentassignment/replace_agent.html", context)

    def move_equipment_view(self, request):
        # Same mechanics as replace; kept as a distinct ops verb.
        ids = request.session.get("sync_move_assignment_ids") or []
        assignments = AgentAssignment.objects.filter(pk__in=ids).select_related(
            "sync_profile__equipment",
            "sync_agent",
        )
        if request.method == "POST":
            form = MoveEquipmentForm(request.POST)
            if form.is_valid():
                new_agent = form.cleaned_data["sync_agent"]
                now = timezone.now()
                moved = 0
                for assignment in assignments:
                    if assignment.is_active:
                        assignment.is_active = False
                        assignment.unassigned_at = now
                        assignment.save(update_fields=["is_active", "unassigned_at", "updated_at"])
                    AgentAssignment.objects.create(
                        sync_agent=new_agent,
                        sync_profile=assignment.sync_profile,
                        is_active=True,
                        notes=_("Moved from %(old)s") % {"old": assignment.sync_agent.agent_name},
                    )
                    assignment.sync_profile.bump_configuration_version()
                    moved += 1
                request.session.pop("sync_move_assignment_ids", None)
                self.message_user(request, _("Moved %(n)s equipment assignment(s).") % {"n": moved})
                return HttpResponseRedirect(reverse("admin:sync_agentassignment_changelist"))
        else:
            form = MoveEquipmentForm()
        context = {
            **self.admin_site.each_context(request),
            "title": _("Move equipment to another agent"),
            "opts": self.model._meta,
            "form": form,
            "assignments": assignments,
        }
        return render(request, "admin/sync/agentassignment/move_equipment.html", context)
