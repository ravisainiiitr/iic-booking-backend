"""Read-only Django admin: groups and group emails are managed from the portal's User Groups page."""

from django.contrib import admin

from .models import FacilityUserGroup, FacilityUserGroupMember, GroupEmailCampaign, GroupEmailRecipient


class ReadOnlyAdmin(admin.ModelAdmin):
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(FacilityUserGroup)
class FacilityUserGroupAdmin(ReadOnlyAdmin):
    list_display = ("name", "kind", "auto_key", "is_archived", "updated_at")
    list_filter = ("kind", "is_archived")
    search_fields = ("name", "auto_key")


@admin.register(FacilityUserGroupMember)
class FacilityUserGroupMemberAdmin(ReadOnlyAdmin):
    list_display = ("group", "user", "booking_count", "supervised_booking_count", "last_booked_at", "added_manually")
    list_filter = ("group__kind", "added_manually")
    search_fields = ("group__name", "user__email", "user__name")
    list_select_related = ("group", "user")


@admin.register(GroupEmailCampaign)
class GroupEmailCampaignAdmin(ReadOnlyAdmin):
    list_display = ("subject", "status", "total_recipients", "sent_count", "failed_count", "created_by", "created_at")
    list_filter = ("status", "cc_mode")
    search_fields = ("subject",)


@admin.register(GroupEmailRecipient)
class GroupEmailRecipientAdmin(ReadOnlyAdmin):
    list_display = ("campaign", "email", "status", "attempts", "sent_at")
    list_filter = ("status",)
    search_fields = ("email", "name", "campaign__subject")
    list_select_related = ("campaign",)
