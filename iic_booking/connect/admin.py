"""Django admin for IITR Connect: configuration editable, audit trails read-only. Day-to-day work uses the API/UI."""

from django.contrib import admin

from .models import Community
from .models import CommunityMembership
from .models import CommunitySyncRun
from .models import ConnectRoleAssignment
from .models import ConnectSettings
from .models import DiscussionCategory
from .models import DiscussionTag
from .models import MembershipAuditLog
from .models import ModerationAction
from .models import RecognitionBadge


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Community)
class CommunityAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "status", "equipment_group_ref_id", "is_test_only", "updated_at")
    list_filter = ("kind", "status", "is_test_only")
    search_fields = ("name", "slug", "group_name_snapshot")
    readonly_fields = ("kind", "equipment_group_ref_id", "group_name_snapshot", "archived_at", "created_at", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False  # communities are archived, never deleted


@admin.register(CommunityMembership)
class CommunityMembershipAdmin(admin.ModelAdmin):
    list_display = ("community", "user", "status", "via_booking", "via_oic", "via_operator", "via_manual", "moderator_override")
    list_filter = ("status", "via_booking", "via_oic", "via_operator", "via_manual")
    search_fields = ("user__email", "user__name", "community__name")
    raw_id_fields = ("user", "community", "status_changed_by")

    def has_delete_permission(self, request, obj=None):
        return False  # revoke instead (keeps history)


@admin.register(DiscussionCategory)
class DiscussionCategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "community", "position", "is_active", "staff_only_posting")
    list_filter = ("community", "is_active")


@admin.register(DiscussionTag)
class DiscussionTagAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active")
    search_fields = ("name", "slug")


@admin.register(ConnectRoleAssignment)
class ConnectRoleAssignmentAdmin(admin.ModelAdmin):
    list_display = ("user", "role", "granted_by", "granted_at", "revoked_at")
    raw_id_fields = ("user", "granted_by")


@admin.register(RecognitionBadge)
class RecognitionBadgeAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "position")


@admin.register(ConnectSettings)
class ConnectSettingsAdmin(admin.ModelAdmin):
    list_display = ("pk", "recognition_enabled", "edit_window_minutes", "max_attachment_mb", "updated_at")

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CommunitySyncRun)
class CommunitySyncRunAdmin(ReadOnlyAdmin):
    list_display = ("pk", "scope", "dry_run", "status", "source", "started_at", "finished_at")


@admin.register(MembershipAuditLog)
class MembershipAuditLogAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "community", "user", "action", "actor")
    list_filter = ("action",)


@admin.register(ModerationAction)
class ModerationActionAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "community", "actor", "action", "target_type", "target_id")
    list_filter = ("action", "target_type")
