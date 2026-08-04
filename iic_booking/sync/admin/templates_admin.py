"""Equipment Sync Template admin + apply-to-profile action."""

from __future__ import annotations

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import EquipmentSyncProfile, EquipmentSyncTemplate


class ApplyTemplateForm(forms.Form):
    profile = forms.ModelChoiceField(
        queryset=EquipmentSyncProfile.objects.select_related("equipment").order_by(
            "equipment__code"
        ),
        label=_("Target sync profile"),
    )
    bump_version = forms.BooleanField(
        required=False,
        initial=True,
        label=_("Bump configuration_version (Configuration Push)"),
    )


@admin.register(EquipmentSyncTemplate)
class EquipmentSyncTemplateAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "code",
        "department",
        "network_mode",
        "share_name",
        "sync_interval_seconds",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "network_mode", "department")
    search_fields = ("name", "code", "description")
    prepopulated_fields = {"code": ("name",)}
    actions = ("action_apply_to_profiles",)
    change_form_template = None

    fieldsets = (
        (None, {"fields": ("name", "code", "description", "department", "is_active")}),
        (
            _("Sync paths"),
            {"fields": ("share_name", "watch_folder", "unc_path_template", "sync_interval_seconds")},
        ),
        (
            _("Flags"),
            {"fields": ("sync_enabled", "watch_enabled", "upload_enabled", "enabled_features")},
        ),
        (
            _("Network & PC policy"),
            {
                "fields": (
                    "network_mode",
                    "windows_account_policy",
                    "folder_layout",
                    "firewall_profile",
                    "retry_policy",
                    "required_software",
                    "health_thresholds",
                )
            },
        ),
        (
            _("Credentials"),
            {"fields": ("smb_username", "smb_credential_reference")},
        ),
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "<uuid:template_id>/apply/",
                self.admin_site.admin_view(self.apply_view),
                name="sync_equipmentsynctemplate_apply",
            ),
        ]
        return custom + urls

    def apply_view(self, request, template_id):
        template = EquipmentSyncTemplate.objects.get(pk=template_id)
        if request.method == "POST":
            form = ApplyTemplateForm(request.POST)
            if form.is_valid():
                profile = form.cleaned_data["profile"]
                bump = form.cleaned_data["bump_version"]
                template.apply_to_profile(profile, bump=bump)
                # Mark assigned agents for bootstrap refresh
                for assignment in profile.assignments.filter(is_active=True).select_related(
                    "sync_agent"
                ):
                    agent = assignment.sync_agent
                    if agent:
                        agent.bootstrap_required = True
                        agent.save(update_fields=["bootstrap_required", "updated_at"])
                messages.success(
                    request,
                    _(
                        "Applied template %(code)s to %(equipment)s "
                        "(configuration_version=%(ver)s)."
                    )
                    % {
                        "code": template.code,
                        "equipment": profile.equipment,
                        "ver": profile.configuration_version,
                    },
                )
                return redirect("admin:sync_equipmentsyncprofile_change", profile.pk)
        else:
            form = ApplyTemplateForm()
        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": _("Apply template %(name)s") % {"name": template.name},
            "form": form,
            "template_obj": template,
        }
        return render(request, "admin/sync/equipmentsynctemplate/apply.html", context)

    @admin.action(description=_("Open apply form for selected template…"))
    def action_apply_to_profiles(self, request, queryset):
        first = queryset.first()
        if not first:
            return
        return redirect(
            reverse("admin:sync_equipmentsynctemplate_apply", args=[first.pk])
        )
