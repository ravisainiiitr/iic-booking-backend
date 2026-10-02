from django.contrib import admin

from . import models


@admin.register(models.TrainingPolicy)
class TrainingPolicyAdmin(admin.ModelAdmin):
    list_display = ("id", "scope", "department", "equipment", "version", "is_active", "published_at")
    list_filter = ("scope", "is_active")
    raw_id_fields = ("department", "equipment", "created_by")


@admin.register(models.CertificationLevel)
class CertificationLevelAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "rank", "default_validity_months", "is_active")


@admin.register(models.TrainingEvent)
class TrainingEventAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "kind", "equipment", "status", "capacity", "created_at")
    list_filter = ("kind", "status")
    search_fields = ("title", "slug")
    raw_id_fields = ("equipment", "department", "created_by", "program")


@admin.register(models.TrainingSession)
class TrainingSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "seq", "start_at", "end_at", "status")
    list_filter = ("status",)
    raw_id_fields = ("event", "equipment", "attendance_marked_by")


@admin.register(models.SessionSlotReservation)
class SessionSlotReservationAdmin(admin.ModelAdmin):
    list_display = ("id", "session", "daily_slot", "equipment", "is_family_block", "reserved_at", "released_at")
    raw_id_fields = ("session", "daily_slot", "equipment", "reserved_by", "released_by")


class DemoRequestRevisionInline(admin.TabularInline):
    model = models.DemoRequestRevision
    extra = 0
    readonly_fields = ("actor", "action", "from_status", "to_status", "reason_code", "reason", "created_at")
    can_delete = False


@admin.register(models.DemoRequest)
class DemoRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "requester", "equipment", "purpose", "status", "curtailed", "charge_amount", "submitted_at")
    list_filter = ("status", "purpose", "curtailed")
    raw_id_fields = ("requester", "equipment", "decided_by", "sub_wallet", "wallet_txn", "refund_txn", "event")
    inlines = [DemoRequestRevisionInline]


@admin.register(models.NominationCall)
class NominationCallAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "equipment", "seats", "deadline", "status")
    list_filter = ("status",)
    raw_id_fields = ("event", "equipment", "policy", "opened_by", "legacy_ta_call")


@admin.register(models.TrainingNomination)
class TrainingNominationAdmin(admin.ModelAdmin):
    list_display = ("id", "call", "student", "nominator", "need_category", "status")
    list_filter = ("status", "need_category")
    raw_id_fields = ("call", "student", "nominator", "legacy_nomination")


@admin.register(models.ShortlistRun)
class ShortlistRunAdmin(admin.ModelAdmin):
    list_display = ("id", "call", "status", "seed_timestamp", "published_at")
    list_filter = ("status",)
    raw_id_fields = ("call", "run_by", "published_by")


@admin.register(models.ShortlistEntry)
class ShortlistEntryAdmin(admin.ModelAdmin):
    list_display = ("id", "run", "nomination", "rank", "score_total", "outcome", "seat_type", "overridden")
    list_filter = ("outcome", "overridden")
    raw_id_fields = ("run", "nomination", "override_by")


@admin.register(models.SelectionAppeal)
class SelectionAppealAdmin(admin.ModelAdmin):
    list_display = ("id", "entry", "submitted_by", "status", "decided_by", "created_at")
    list_filter = ("status",)
    raw_id_fields = ("entry", "submitted_by", "decided_by")


@admin.register(models.Registration)
class RegistrationAdmin(admin.ModelAdmin):
    list_display = ("id", "event", "user", "source", "status")
    list_filter = ("status", "source")
    raw_id_fields = ("event", "user", "nomination", "wallet_txn")


@admin.register(models.Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = ("id", "registration", "session", "status", "marked_by", "marked_at")
    raw_id_fields = ("registration", "session", "marked_by")


@admin.register(models.CertificationAward)
class CertificationAwardAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "equipment", "level", "status", "awarded_at", "valid_until")
    list_filter = ("status", "level")
    raw_id_fields = ("user", "equipment", "equipment_group", "source_event", "source_registration", "awarded_by", "revoked_by")
    exclude = ("verify_token",)


@admin.register(models.BadgeDefinition)
class BadgeDefinitionAdmin(admin.ModelAdmin):
    list_display = ("code", "name", "rule_type", "level", "is_active")


@admin.register(models.UserBadge)
class UserBadgeAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "badge", "equipment", "awarded_at", "revoked_at")
    raw_id_fields = ("user", "badge", "equipment", "award")


@admin.register(models.TrainingAuditLog)
class TrainingAuditLogAdmin(admin.ModelAdmin):
    list_display = ("id", "action", "object_type", "object_id", "actor", "created_at")
    list_filter = ("action", "object_type")
    raw_id_fields = ("actor",)
