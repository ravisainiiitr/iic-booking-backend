from django.contrib import admin
from django.db.models import Q, Sum

from .models import FileStatus, ResearchActivity, ResearchFile, ResearchWorkspace


class ReadOnlyAdmin(admin.ModelAdmin):
    """Monitoring only: research content is managed by its owners through the portal."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ResearchWorkspace)
class ResearchWorkspaceAdmin(ReadOnlyAdmin):
    list_display = ("name", "owner", "status", "storage_used", "created_at", "last_activity_at")
    list_filter = ("status",)
    search_fields = ("name", "owner__email", "owner__name")

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("owner")
            .annotate(_storage=Sum("files__size_bytes", filter=Q(files__status=FileStatus.AVAILABLE)))
        )

    @admin.display(description="Storage (MB)", ordering="_storage")
    def storage_used(self, obj):
        return round((obj._storage or 0) / (1024**2), 1)


@admin.register(ResearchFile)
class ResearchFileAdmin(ReadOnlyAdmin):
    list_display = ("display_name", "workspace", "status", "size_bytes", "detected_type", "uploaded_by", "created_at")
    list_filter = ("status", "detected_type")
    search_fields = ("display_name", "workspace__name", "uploaded_by__email")
    exclude = ("multipart_upload_id",)


@admin.register(ResearchActivity)
class ResearchActivityAdmin(ReadOnlyAdmin):
    list_display = ("workspace", "action", "actor", "target_label", "created_at")
    list_filter = ("action",)
    search_fields = ("workspace__name", "target_label", "actor__email")
