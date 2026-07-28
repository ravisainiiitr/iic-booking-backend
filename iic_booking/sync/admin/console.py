"""Department Sync Operations dashboard entry in Django Admin."""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.translation import gettext_lazy as _

from iic_booking.sync.models import SyncOperationsConsole

from .dashboard import build_operations_dashboard_context
from .helpers import lifecycle_badge, online_badge, severity_badge


@admin.register(SyncOperationsConsole)
class SyncOperationsConsoleAdmin(admin.ModelAdmin):
    """
    Sidebar entry that opens the operations dashboard.

    Not a CRUD screen — changelist redirects to the console.
    """

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.user.is_staff

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.model_name
        return [
            path(
                "console/",
                self.admin_site.admin_view(self.console_view),
                name="%s_%s_console" % info,
            ),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        return HttpResponseRedirect(reverse("admin:sync_syncoperationsconsole_console"))

    def console_view(self, request):
        context = {
            **self.admin_site.each_context(request),
            **build_operations_dashboard_context(),
            "title": _("Department Sync Operations"),
            "opts": self.model._meta,
            "lifecycle_badge": lifecycle_badge,
            "online_badge": online_badge,
            "severity_badge": severity_badge,
            "agent_changelist": reverse("admin:sync_departmentsyncagent_changelist"),
            "profile_changelist": reverse("admin:sync_equipmentsyncprofile_changelist"),
            "assignment_changelist": reverse("admin:sync_agentassignment_changelist"),
            "heartbeat_changelist": reverse("admin:sync_agentheartbeat_changelist"),
            "log_changelist": reverse("admin:sync_synclog_changelist"),
        }
        return render(request, "admin/sync/operations_dashboard.html", context)
