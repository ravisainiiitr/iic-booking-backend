"""I-STEM admin: mappings are editable; links, jobs, attempts and events are read-only records.

Recovery actions (reconcile, manual linking, resolving uncertain outcomes) are added in a later stage.
Payload fields are hidden from users without ``istem.view_sensitive_payload``.
"""

from django.contrib import admin

from .models import (
    IstemApiAttempt,
    IstemBookingLink,
    IstemCodeMapping,
    IstemEquipmentMapping,
    IstemOutboxJob,
    IstemSyncEvent,
    IstemUserLink,
)

SENSITIVE_PAYLOAD_PERMISSION = "istem.view_sensitive_payload"


class _ReadOnlyAdmin(admin.ModelAdmin):
    """View-only admin. ``sensitive_fields`` are shown only with the sensitive-payload permission."""

    sensitive_fields: tuple[str, ...] = ()

    def get_fields(self, request, obj=None):
        fields = [f.name for f in self.model._meta.fields]
        if not request.user.has_perm(SENSITIVE_PAYLOAD_PERMISSION):
            fields = [f for f in fields if f not in self.sensitive_fields]
        return fields

    def get_readonly_fields(self, request, obj=None):
        return self.get_fields(request, obj)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IstemEquipmentMapping)
class IstemEquipmentMappingAdmin(admin.ModelAdmin):
    list_display = ("equipment", "istem_equipment_id", "sample_count_input_key", "is_active", "verified_at")
    list_filter = ("is_active",)
    search_fields = ("equipment__name", "equipment__code", "istem_equipment_id")
    raw_id_fields = ("equipment",)
    # Set by the verification workflow, never typed in by hand.
    readonly_fields = ("verified_by", "verified_at")


@admin.register(IstemCodeMapping)
class IstemCodeMappingAdmin(admin.ModelAdmin):
    list_display = ("code_type", "local_key", "equipment", "istem_value", "is_active")
    list_filter = ("code_type", "is_active")
    search_fields = ("local_key", "istem_value", "equipment__name", "equipment__code")
    raw_id_fields = ("equipment",)


@admin.register(IstemUserLink)
class IstemUserLinkAdmin(_ReadOnlyAdmin):
    list_display = ("user", "public_user_id", "state", "linked_at", "updated_at")
    list_filter = ("state",)
    search_fields = ("user__email", "public_user_id")


@admin.register(IstemBookingLink)
class IstemBookingLinkAdmin(_ReadOnlyAdmin):
    sensitive_fields = ("create_payload",)
    list_display = ("booking", "sync_state", "request_id", "fbr_no", "remote_status", "needs_review", "updated_at")
    list_filter = ("sync_state", "remote_status", "needs_review", "local_drift")
    search_fields = ("booking__virtual_booking_id", "request_id", "fbr_no", "confirmation_id")


@admin.register(IstemOutboxJob)
class IstemOutboxJobAdmin(_ReadOnlyAdmin):
    list_display = ("operation", "state", "dedupe_key", "attempt_count", "next_attempt_at", "created_at")
    list_filter = ("operation", "state")
    search_fields = ("dedupe_key",)


@admin.register(IstemApiAttempt)
class IstemApiAttemptAdmin(_ReadOnlyAdmin):
    sensitive_fields = ("request_redacted", "response_redacted")
    list_display = ("operation", "outcome", "error_category", "http_status", "started_at", "duration_ms")
    list_filter = ("operation", "outcome", "error_category")


@admin.register(IstemSyncEvent)
class IstemSyncEventAdmin(_ReadOnlyAdmin):
    list_display = ("created_at", "booking_link", "user_link", "from_state", "to_state", "actor", "evidence_type")
    list_filter = ("evidence_type",)
