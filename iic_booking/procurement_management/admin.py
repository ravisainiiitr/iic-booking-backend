from django.contrib import admin

from . import models as m


class ReadOnlyAdmin(admin.ModelAdmin):
    """Inspection only: every write goes through the audited service layer and the API."""

    list_per_page = 50
    show_full_result_count = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}


def _register(model, *, display=(), filters=(), search=(), related=()):
    attrs = {
        "list_display": ("id", *display) if display else ("id", "__str__"),
        "list_filter": filters,
        "search_fields": search,
        "list_select_related": related or False,
    }
    admin.site.register(model, type(f"{model.__name__}Admin", (ReadOnlyAdmin,), attrs))


_register(
    m.ProcurementManagementConfiguration,
    display=("department", "module_enabled", "small_purchase_threshold", "updated_at"),
    filters=("module_enabled",),
    related=("department",),
)
_register(m.ProcurementRoleAssignment, display=("department", "user", "role", "active"), filters=("role", "active"), search=("user__email", "user__name"), related=("department", "user"))
_register(m.ItemCategory, display=("department", "code", "name", "nature", "active"), filters=("nature", "active"), search=("name", "code"), related=("department",))
_register(m.RequestTypeConfig, display=("department", "code", "name", "active"), filters=("active",), related=("department",))
_register(m.GSTRate, display=("department", "rate", "active"), related=("department",))
_register(m.Vendor, display=("department", "name", "gstin", "is_archived"), filters=("is_archived",), search=("name", "gstin"), related=("department",))
_register(m.Item, display=("department", "code", "name", "is_archived"), filters=("is_archived",), search=("name", "code"), related=("department",))
_register(m.NumberSequence)
_register(m.PurchaseRequest, display=("number", "department", "status", "estimated_total", "created_at"), filters=("status",), search=("number", "title"), related=("department",))
_register(m.PurchaseRequestLine)
_register(m.ProcurementDocument, display=("department", "doc_type", "original_name", "created_at"), filters=("doc_type",), search=("original_name",), related=("department",))
_register(m.ApprovalAction)
_register(m.PlanProposal, display=("number", "department", "status", "financial_year"), filters=("status",), search=("number",), related=("department",))
_register(m.PlanRequirement, display=("number", "department", "status", "financial_year"), filters=("status",), search=("number",), related=("department",))
_register(m.RequirementChangeLog)
_register(m.ProcurementRecord, display=("number", "department", "status", "created_at"), filters=("status",), search=("number",), related=("department",))
_register(m.Quotation)
_register(m.Invoice, display=("department", "invoice_number", "total_amount", "invoice_date"), search=("invoice_number",), related=("department",))
_register(m.InvoiceLine)
_register(m.Asset, display=("number", "department", "status", "description"), filters=("status",), search=("number", "serial_number", "description"), related=("department",))
_register(m.AssetRegister, display=("department", "code", "name", "register_type", "active"), filters=("register_type", "active"), search=("code", "name"), related=("department",))
_register(m.VerificationCampaign, display=("number", "department", "title", "status", "started_on"), filters=("status",), search=("number", "title"), related=("department",))
_register(m.AssetVerification, display=("asset", "result", "verified_on", "method"), filters=("result", "method"), related=("asset",))
_register(m.AssetDisposal, display=("number", "asset", "action", "to_status", "recorded_at"), filters=("action",), search=("number",), related=("asset",))
_register(m.ItemEquipmentLink, display=("department", "item", "equipment", "usage", "active"), filters=("usage", "active"), related=("department", "item", "equipment"))
_register(m.MaintenanceRecord, display=("number", "department", "equipment", "kind", "downtime_start"), filters=("kind",), search=("number",), related=("department", "equipment"))
_register(m.AssetStatusHistory)
_register(m.AssetTransfer)
_register(m.StockBalance)
_register(m.StockTransaction)
_register(m.AMCServiceRecord, display=("number", "department", "status", "end_date"), filters=("status",), search=("number",), related=("department",))
_register(m.BudgetAllocation, display=("department", "financial_year", "funding_type", "amount", "is_archived"), filters=("funding_type", "is_archived"), related=("department",))
_register(m.ProcurementAuditLog, display=("created_at", "action", "object_type", "object_number", "actor"), filters=("action",), search=("object_number", "action"), related=("actor",))
