"""Procurement & Assets data model.

Money is ``Decimal(14, 2)``; quantities ``Decimal(14, 3)``. Financial / procurement / asset records are
never hard-deleted (``ArchivableModel``); audit, approval history and the stock ledger are append-only
(``AppendOnlyModel``). Every row carries its ``department`` so object-level checks never need joins
through user-supplied ids.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from . import constants as c

USER = settings.AUTH_USER_MODEL
ZERO = Decimal("0.00")
ZERO_QTY = Decimal("0.000")


def money(**kwargs):
    kwargs.setdefault("max_digits", 14)
    kwargs.setdefault("decimal_places", 2)
    kwargs.setdefault("default", ZERO)
    return models.DecimalField(**kwargs)


def qty(**kwargs):
    kwargs.setdefault("max_digits", 14)
    kwargs.setdefault("decimal_places", 3)
    kwargs.setdefault("default", ZERO_QTY)
    return models.DecimalField(**kwargs)


class ImmutableRecordError(Exception):
    """Raised when code tries to modify or delete an append-only record."""


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only.")

    def delete(self):
        raise ImmutableRecordError(f"{self.model.__name__} rows are append-only.")


class AppendOnlyModel(models.Model):
    objects = AppendOnlyQuerySet.as_manager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ImmutableRecordError(f"{type(self).__name__} rows are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} rows are append-only.")


class ArchivableModel(models.Model):
    """Soft delete only. ``delete()`` is refused; use ``services.archive``."""

    is_archived = models.BooleanField(default=False, db_index=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    archived_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    archive_reason = models.TextField(blank=True, default="")

    class Meta:
        abstract = True

    def delete(self, *args, **kwargs):
        raise ImmutableRecordError(f"{type(self).__name__} rows cannot be deleted; archive them instead.")


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
class ProcurementManagementConfiguration(TimeStamped):
    """Per-department switches and thresholds. The module is OFF until the Main Administrator enables it.

    Threshold defaults are only initial values for a new row; the workflow always reads the row.
    """

    department = models.OneToOneField(
        "users.Department", on_delete=models.CASCADE, related_name="procurement_management_config"
    )
    module_enabled = models.BooleanField(default=False)
    pilot_mode = models.BooleanField(
        default=True,
        help_text=_("While on, only the pilot users below can use the module in this department; everyone else sees it as disabled."),
    )
    pilot_users = models.ManyToManyField(USER, blank=True, related_name="procurement_pilot_configs")

    consumables_enabled = models.BooleanField(default=True)
    non_consumables_enabled = models.BooleanField(default=True)
    asset_register_enabled = models.BooleanField(default=True)
    plan_enabled = models.BooleanField(default=True)
    non_plan_enabled = models.BooleanField(default=True)
    amc_enabled = models.BooleanField(default=True)
    general_purchase_enabled = models.BooleanField(default=True)
    minor_purchase_enabled = models.BooleanField(default=True)
    major_purchase_enabled = models.BooleanField(default=True)
    limited_life_enabled = models.BooleanField(default=True)

    small_purchase_threshold = money(
        default=Decimal("2000.00"),
        validators=[MinValueValidator(ZERO)],
        help_text=_("Total (incl. GST) at or below which a direct small purchase is allowed."),
    )
    hod_approval_threshold = money(
        default=Decimal("25000.00"),
        validators=[MinValueValidator(ZERO)],
        help_text=_("Requests above this total need HOD / Competent Authority approval when the request type says so."),
    )
    comparative_quotation_threshold = money(
        default=Decimal("25000.00"),
        validators=[MinValueValidator(ZERO)],
        help_text=_("Above this value a comparative statement of quotations is required."),
    )
    asset_capitalization_threshold = money(
        default=Decimal("5000.00"),
        validators=[MinValueValidator(ZERO)],
        help_text=_("Asset items at or above this unit cost are capitalised in the asset register."),
    )
    variance_tolerance_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("10.00"), validators=[MinValueValidator(ZERO)]
    )
    variance_action = models.CharField(
        max_length=20, choices=c.VarianceAction.choices, default=c.VarianceAction.OFFICE_REVIEW
    )

    require_invoice = models.BooleanField(default=True)
    require_specification = models.BooleanField(
        default=True, help_text=_("Specification is mandatory for non-consumable and asset requests.")
    )
    require_comparative_statement = models.BooleanField(default=True)
    require_asset_allocation = models.BooleanField(
        default=True, help_text=_("Asset items must be entered in the asset register before completion.")
    )
    allow_office_direct_purchase_entry = models.BooleanField(default=True)
    allow_resubmission = models.BooleanField(default=True)
    hod_approval_mode = models.CharField(
        max_length=10, choices=c.HodApprovalMode.choices, default=c.HodApprovalMode.EITHER
    )
    current_financial_year = models.CharField(
        max_length=7, blank=True, default="", help_text=_("Plan cycle FY (e.g. 2026-27). Empty = current FY.")
    )
    plan_submission_open = models.BooleanField(default=True)
    amc_reminder_days = models.PositiveIntegerField(default=60)

    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = _("Procurement configuration")
        verbose_name_plural = _("Procurement configurations")

    def __str__(self) -> str:
        return f"Procurement config — {self.department} ({'on' if self.module_enabled else 'off'})"


class ProcurementRoleAssignment(TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="pm_role_assignments")
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="pm_role_assignments")
    role = models.CharField(max_length=20, choices=[(r.value, r.label) for r in c.ASSIGNABLE_ROLES])
    permissions = models.JSONField(default=list, blank=True, help_text=_("Granular Office permissions."))
    active = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["department", "user", "role"], name="pm_role_unique_dept_user_role"),
        ]
        indexes = [models.Index(fields=["user", "active"]), models.Index(fields=["department", "role", "active"])]

    def __str__(self) -> str:
        return f"{self.user_id} {self.role} @ {self.department_id}"


# ---------------------------------------------------------------------------
# Masters
# ---------------------------------------------------------------------------
class ItemCategory(TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="pm_item_categories")
    code = models.CharField(max_length=40)
    name = models.CharField(max_length=120)
    nature = models.CharField(max_length=24, choices=c.ItemNature.choices)
    is_asset = models.BooleanField(default=False)
    tracks_stock = models.BooleanField(default=False)
    small_purchase_allowed = models.BooleanField(default=True)
    approval_exempt = models.BooleanField(
        default=False, help_text=_("Exempt from the mandatory approval workflow above the small-purchase threshold.")
    )
    hod_required_always = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["department", "code"], name="pm_category_unique_code")]

    def __str__(self) -> str:
        return self.name


class RequestTypeConfig(TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="pm_request_types")
    code = models.CharField(max_length=30, choices=c.RequestTypeCode.choices)
    name = models.CharField(max_length=120)
    default_nature = models.CharField(max_length=24, choices=c.ItemNature.choices, blank=True, default="")
    requires_oic = models.BooleanField(default=True)
    requires_stores = models.BooleanField(default=True)
    hod_rule = models.CharField(max_length=20, choices=c.HodRule.choices, default=c.HodRule.ABOVE_THRESHOLD)
    stores_issue_flow = models.BooleanField(
        default=False, help_text=_("Stores checks availability and issues from stock before procurement.")
    )
    allow_small_purchase = models.BooleanField(default=True)
    requires_specification = models.BooleanField(default=False)
    procurement_steps = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["department", "code"], name="pm_request_type_unique_code")]

    def __str__(self) -> str:
        return self.name


class GSTRate(TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.CASCADE, related_name="pm_gst_rates")
    name = models.CharField(max_length=60)
    rate = models.DecimalField(max_digits=5, decimal_places=2, validators=[MinValueValidator(ZERO)])
    cgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    sgst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    igst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["rate"]
        constraints = [models.UniqueConstraint(fields=["department", "rate"], name="pm_gst_unique_rate")]

    def __str__(self) -> str:
        return f"{self.name} ({self.rate}%)"


class Vendor(ArchivableModel, TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_vendors")
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=255)
    gstin = models.CharField(max_length=15, blank=True, default="")
    pan = models.CharField(max_length=10, blank=True, default="")
    address = models.TextField(blank=True, default="")
    state = models.CharField(max_length=60, blank=True, default="")
    contact_person = models.CharField(max_length=120, blank=True, default="")
    phone = models.CharField(max_length=40, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["department", "active"]), models.Index(fields=["gstin"])]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class Item(ArchivableModel, TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_items")
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=255)
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, related_name="items")
    uom = models.CharField(max_length=30, default="Nos")
    specification = models.TextField(blank=True, default="")
    hsn_sac = models.CharField(max_length=20, blank=True, default="")
    default_gst_rate = models.ForeignKey(GSTRate, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    min_level = qty()
    reorder_level = qty()
    legacy_inventory_item = models.ForeignKey(
        "equipment.InventoryItem", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    active = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["department", "active"])]

    def __str__(self) -> str:
        return f"{self.code} {self.name}"


class NumberSequence(models.Model):
    prefix = models.CharField(max_length=10)
    financial_year = models.CharField(max_length=7)
    last_value = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prefix", "financial_year"], name="pm_number_seq_unique")]

    def __str__(self) -> str:
        return f"{self.prefix}/{self.financial_year}: {self.last_value}"


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------
class PurchaseRequest(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_requests")
    laboratory = models.ForeignKey(
        "sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_requests"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_requests"
    )
    request_type = models.ForeignKey(RequestTypeConfig, on_delete=models.PROTECT, related_name="requests")
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="requests")
    nature = models.CharField(max_length=24, choices=c.ItemNature.choices, blank=True, default="")
    origin = models.CharField(max_length=20, choices=c.RequestOrigin.choices, default=c.RequestOrigin.REQUEST)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices, default=c.FundingType.OTHER)
    financial_year = models.CharField(max_length=7)
    title = models.CharField(max_length=255)
    justification = models.TextField(blank=True, default="")
    specification = models.TextField(blank=True, default="")
    required_by = models.DateField(null=True, blank=True)
    priority = models.CharField(max_length=10, choices=c.Priority.choices, default=c.Priority.NORMAL)

    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="pm_requests_raised")
    raised_as_role = models.CharField(max_length=20, choices=c.ModuleRole.choices)
    status = models.CharField(max_length=24, choices=c.RequestStatus.choices, default=c.RequestStatus.DRAFT)
    held_from_status = models.CharField(max_length=24, choices=c.RequestStatus.choices, blank=True, default="")
    approval_route = models.JSONField(default=list, blank=True)
    route_index = models.PositiveSmallIntegerField(default=0)

    estimated_total = money()
    approved_amount = money(null=True, blank=True, default=None)
    is_small_purchase = models.BooleanField(default=False)
    hod_required = models.BooleanField(default=False)
    last_reason = models.TextField(blank=True, default="")
    resubmission_count = models.PositiveSmallIntegerField(default=0)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["department", "status"]),
            models.Index(fields=["department", "financial_year"]),
            models.Index(fields=["requested_by", "status"]),
            models.Index(fields=["equipment", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"

    @property
    def current_stage(self) -> str:
        if self.status in c.PENDING_STATUSES and self.route_index < len(self.approval_route or []):
            return self.approval_route[self.route_index]
        return ""


class PurchaseRequestLine(TimeStamped):
    request = models.ForeignKey(PurchaseRequest, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True, related_name="request_lines")
    description = models.CharField(max_length=255)
    specification = models.TextField(blank=True, default="")
    quantity = qty(validators=[MinValueValidator(Decimal("0.001"))])
    uom = models.CharField(max_length=30, default="Nos")
    estimated_unit_price = money(validators=[MinValueValidator(ZERO)])
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO, validators=[MinValueValidator(ZERO)])
    line_total = money()
    issued_quantity = qty()

    class Meta:
        ordering = ["id"]
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name="pm_req_line_qty_gt_0")]


def document_upload_to(instance, filename):
    ext = os.path.splitext(filename or "")[1].lower()
    now = timezone.now()
    return f"procurement_management/{instance.department_id}/{now:%Y/%m}/{uuid.uuid4().hex}{ext}"


class ProcurementDocument(ArchivableModel, TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_documents")
    doc_type = models.CharField(max_length=30, choices=c.DocumentType.choices, default=c.DocumentType.OTHER)
    file = models.FileField(upload_to=document_upload_to, max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    page_group = models.UUIDField(null=True, blank=True, help_text=_("Pages captured together share a group."))
    page_number = models.PositiveSmallIntegerField(default=1)
    description = models.CharField(max_length=255, blank=True, default="")
    uploaded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="pm_documents_uploaded")

    purchase_request = models.ForeignKey(
        PurchaseRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    procurement_record = models.ForeignKey(
        "ProcurementRecord", on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    invoice = models.ForeignKey("Invoice", on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    asset = models.ForeignKey("Asset", on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    proposal = models.ForeignKey("PlanProposal", on_delete=models.PROTECT, null=True, blank=True, related_name="documents")
    amc_record = models.ForeignKey(
        "AMCServiceRecord", on_delete=models.PROTECT, null=True, blank=True, related_name="documents"
    )
    quotation = models.ForeignKey("Quotation", on_delete=models.PROTECT, null=True, blank=True, related_name="documents")

    class Meta:
        ordering = ["page_group", "page_number", "id"]
        indexes = [models.Index(fields=["department", "doc_type"])]

    def __str__(self) -> str:
        return f"{self.doc_type} {self.original_name}"


class ApprovalAction(AppendOnlyModel):
    """Immutable decision history for requests, requirements, proposals and variance re-approvals."""

    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_approval_actions")
    purchase_request = models.ForeignKey(
        PurchaseRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="approval_actions"
    )
    proposal = models.ForeignKey(
        "PlanProposal", on_delete=models.PROTECT, null=True, blank=True, related_name="approval_actions"
    )
    requirement = models.ForeignKey(
        "PlanRequirement", on_delete=models.PROTECT, null=True, blank=True, related_name="approval_actions"
    )
    stage = models.CharField(max_length=12, choices=c.ApprovalStage.choices)
    action = models.CharField(max_length=24, choices=c.ApprovalActionType.choices)
    from_status = models.CharField(max_length=30, blank=True, default="")
    to_status = models.CharField(max_length=30, blank=True, default="")
    actor = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="pm_approval_actions")
    actor_role = models.CharField(max_length=20, blank=True, default="")
    comments = models.TextField(blank=True, default="")
    amount = money(null=True, blank=True, default=None)
    is_offline = models.BooleanField(default=False)
    offline_approver_name = models.CharField(max_length=255, blank=True, default="")
    offline_approver_designation = models.CharField(max_length=255, blank=True, default="")
    offline_approval_date = models.DateField(null=True, blank=True)
    offline_reference = models.CharField(max_length=255, blank=True, default="")
    offline_document = models.ForeignKey(
        ProcurementDocument, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["department", "created_at"]), models.Index(fields=["actor", "created_at"])]


# ---------------------------------------------------------------------------
# Plan / non-plan requirements
# ---------------------------------------------------------------------------
class PlanProposal(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_proposals")
    financial_year = models.CharField(max_length=7)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices, default=c.FundingType.PLAN)
    title = models.CharField(max_length=255)
    remarks = models.TextField(blank=True, default="")
    status = models.CharField(max_length=20, choices=c.ProposalStatus.choices, default=c.ProposalStatus.DRAFT)
    total_amount = money()
    approved_amount = money(null=True, blank=True, default=None)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["department", "financial_year", "funding_type"])]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"


class PlanRequirement(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_requirements")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_requirements"
    )
    financial_year = models.CharField(max_length=7)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices, default=c.FundingType.PLAN)
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    description = models.CharField(max_length=255)
    specification = models.TextField(blank=True, default="")
    justification = models.TextField(blank=True, default="")
    quantity = qty(validators=[MinValueValidator(Decimal("0.001"))])
    uom = models.CharField(max_length=30, default="Nos")
    estimated_unit_cost = money(validators=[MinValueValidator(ZERO)])
    estimated_total = money()
    approved_amount = money(null=True, blank=True, default=None)
    priority = models.CharField(max_length=10, choices=c.Priority.choices, default=c.Priority.NORMAL)
    status = models.CharField(max_length=24, choices=c.RequirementStatus.choices, default=c.RequirementStatus.DRAFT)
    raised_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="pm_requirements_raised")
    added_by_office = models.BooleanField(default=False)
    proposal = models.ForeignKey(
        PlanProposal, on_delete=models.PROTECT, null=True, blank=True, related_name="requirements"
    )
    merged_into = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="merged_from")
    split_from = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="split_into")
    original_values = models.JSONField(default=dict, blank=True)
    procurement_record = models.ForeignKey(
        "ProcurementRecord", on_delete=models.PROTECT, null=True, blank=True, related_name="requirements"
    )
    submitted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["department", "financial_year", "funding_type", "status"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.description}"


class RequirementChangeLog(AppendOnlyModel):
    requirement = models.ForeignKey(PlanRequirement, on_delete=models.PROTECT, related_name="change_logs")
    change_type = models.CharField(max_length=10, choices=c.RequirementChangeType.choices)
    field = models.CharField(max_length=60, blank=True, default="")
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    related_requirement = models.ForeignKey(
        PlanRequirement, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    reason = models.TextField()
    changed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["changed_at", "id"]


# ---------------------------------------------------------------------------
# Procurement workspace
# ---------------------------------------------------------------------------
class ProcurementRecord(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_procurement_records")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_procurement_records"
    )
    purchase_request = models.ForeignKey(
        PurchaseRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="procurement_records"
    )
    proposal = models.ForeignKey(
        PlanProposal, on_delete=models.PROTECT, null=True, blank=True, related_name="procurement_records"
    )
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    origin = models.CharField(max_length=20, choices=c.RequestOrigin.choices, default=c.RequestOrigin.REQUEST)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices, default=c.FundingType.OTHER)
    financial_year = models.CharField(max_length=7)
    title = models.CharField(max_length=255)
    is_small_purchase = models.BooleanField(default=False)
    status = models.CharField(
        max_length=20, choices=c.ProcurementRecordStatus.choices, default=c.ProcurementRecordStatus.OPEN
    )
    required_steps = models.JSONField(default=list, blank=True)
    completed_steps = models.JSONField(default=list, blank=True)
    approved_amount = money(null=True, blank=True, default=None)
    estimated_amount = money()

    indent_number = models.CharField(max_length=80, blank=True, default="")
    indent_date = models.DateField(null=True, blank=True)
    specification = models.TextField(blank=True, default="")
    rfq_reference = models.CharField(max_length=120, blank=True, default="")
    rfq_date = models.DateField(null=True, blank=True)
    selected_vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    selection_justification = models.TextField(blank=True, default="")
    po_number = models.CharField(max_length=80, blank=True, default="")
    po_date = models.DateField(null=True, blank=True)
    po_amount = money(null=True, blank=True, default=None)
    expected_delivery_date = models.DateField(null=True, blank=True)
    delivery_date = models.DateField(null=True, blank=True)
    delivery_challan_number = models.CharField(max_length=80, blank=True, default="")
    inspection_date = models.DateField(null=True, blank=True)
    inspection_result = models.CharField(
        max_length=20, choices=c.InspectionResult.choices, default=c.InspectionResult.PENDING
    )
    inspection_remarks = models.TextField(blank=True, default="")
    inspected_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    payment_status = models.CharField(max_length=20, choices=c.PaymentStatus.choices, default=c.PaymentStatus.UNPAID)
    paid_amount = money()
    payment_date = models.DateField(null=True, blank=True)
    payment_reference = models.CharField(max_length=120, blank=True, default="")
    purchase_date = models.DateField(null=True, blank=True)
    purchased_by_name = models.CharField(max_length=255, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["department", "status"]),
            models.Index(fields=["department", "financial_year"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.title}"


class Quotation(ArchivableModel, TimeStamped):
    procurement_record = models.ForeignKey(ProcurementRecord, on_delete=models.PROTECT, related_name="quotations")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="quotations")
    quotation_reference = models.CharField(max_length=120, blank=True, default="")
    quotation_date = models.DateField(null=True, blank=True)
    amount = money(validators=[MinValueValidator(ZERO)])
    gst_amount = money(validators=[MinValueValidator(ZERO)])
    total_amount = money()
    delivery_period = models.CharField(max_length=120, blank=True, default="")
    warranty = models.CharField(max_length=120, blank=True, default="")
    compliance = models.CharField(max_length=20, choices=c.Compliance.choices, default=c.Compliance.COMPLIANT)
    remarks = models.TextField(blank=True, default="")
    is_selected = models.BooleanField(default=False)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["total_amount", "id"]


class Invoice(ArchivableModel, TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_invoices")
    procurement_record = models.ForeignKey(ProcurementRecord, on_delete=models.PROTECT, related_name="invoices")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices")
    vendor_name_text = models.CharField(max_length=255, blank=True, default="")
    invoice_number = models.CharField(max_length=80)
    invoice_date = models.DateField()
    supply_type = models.CharField(max_length=12, choices=c.SupplyType.choices, default=c.SupplyType.INTRA_STATE)
    taxable_amount = money()
    cgst_amount = money()
    sgst_amount = money()
    igst_amount = money()
    other_charges = money()
    total_amount = money()
    approved_amount = money(null=True, blank=True, default=None)
    variance_amount = money()
    variance_percent = models.DecimalField(max_digits=8, decimal_places=2, default=ZERO)
    variance_status = models.CharField(
        max_length=20, choices=c.VarianceStatus.choices, default=c.VarianceStatus.NOT_APPLICABLE
    )
    variance_reviewed_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    variance_reviewed_at = models.DateTimeField(null=True, blank=True)
    variance_review_note = models.TextField(blank=True, default="")
    paid_amount = money()
    payment_status = models.CharField(max_length=20, choices=c.PaymentStatus.choices, default=c.PaymentStatus.UNPAID)
    remarks = models.TextField(blank=True, default="")
    recorded_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-invoice_date", "-id"]
        indexes = [models.Index(fields=["department", "invoice_date"]), models.Index(fields=["vendor", "invoice_number"])]


class InvoiceLine(TimeStamped):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="lines")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    description = models.CharField(max_length=255)
    quantity = qty(validators=[MinValueValidator(Decimal("0.001"))])
    uom = models.CharField(max_length=30, default="Nos")
    unit_price = money(validators=[MinValueValidator(ZERO)])
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2, default=ZERO)
    taxable_amount = money()
    gst_amount = money()
    line_total = money()

    class Meta:
        ordering = ["id"]


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------
class Asset(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_assets")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_assets"
    )
    item = models.ForeignKey(Item, on_delete=models.PROTECT, null=True, blank=True, related_name="assets")
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, related_name="assets")
    description = models.CharField(max_length=255)
    make = models.CharField(max_length=120, blank=True, default="")
    model_number = models.CharField(max_length=120, blank=True, default="")
    serial_number = models.CharField(max_length=120, blank=True, default="")
    asset_tag = models.CharField(max_length=120, blank=True, default="")
    procurement_record = models.ForeignKey(
        ProcurementRecord, on_delete=models.PROTECT, null=True, blank=True, related_name="assets"
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name="assets")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    purchase_date = models.DateField(null=True, blank=True)
    cost = money(validators=[MinValueValidator(ZERO)])
    is_capitalized = models.BooleanField(default=False)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices, default=c.FundingType.OTHER)
    financial_year = models.CharField(max_length=7, blank=True, default="")
    warranty_until = models.DateField(null=True, blank=True)
    location = models.CharField(max_length=255, blank=True, default="")
    custodian = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="pm_assets_custody")
    status = models.CharField(max_length=24, choices=c.AssetStatus.choices, default=c.AssetStatus.IN_STORE)
    remarks = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["department", "status"]),
            models.Index(fields=["serial_number"]),
            models.Index(fields=["equipment"]),
        ]

    def __str__(self) -> str:
        return f"{self.number} {self.description}"


class AssetStatusHistory(AppendOnlyModel):
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="status_history")
    from_status = models.CharField(max_length=24, blank=True, default="")
    to_status = models.CharField(max_length=24)
    reason = models.TextField()
    changed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["changed_at", "id"]


class AssetTransfer(TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="transfers")
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_asset_transfers")
    transfer_type = models.CharField(max_length=10, choices=c.TransferType.choices)
    status = models.CharField(max_length=10, choices=c.TransferStatus.choices, default=c.TransferStatus.REQUESTED)
    from_laboratory = models.ForeignKey(
        "sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    to_laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    from_equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    to_equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    from_location = models.CharField(max_length=255, blank=True, default="")
    to_location = models.CharField(max_length=255, blank=True, default="")
    from_custodian = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    to_custodian = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    from_status = models.CharField(max_length=24, blank=True, default="")
    reason = models.TextField()
    expected_return_date = models.DateField(null=True, blank=True)
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Consumable stock
# ---------------------------------------------------------------------------
class StockBalance(models.Model):
    """Current quantity per (department, store/lab, item). Only ``stock.post`` changes it."""

    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_stock_balances")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="stock_balances")
    quantity = qty()
    min_level = qty()
    reorder_level = qty()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["department", "laboratory", "item"],
                name="pm_stock_balance_unique_lab",
                condition=models.Q(laboratory__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["department", "item"],
                name="pm_stock_balance_unique_central",
                condition=models.Q(laboratory__isnull=True),
            ),
            models.CheckConstraint(condition=models.Q(quantity__gte=0), name="pm_stock_balance_non_negative"),
        ]


class StockTransaction(AppendOnlyModel):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_stock_transactions")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="stock_transactions")
    tx_type = models.CharField(max_length=16, choices=c.StockTxType.choices)
    quantity = qty(validators=[MinValueValidator(Decimal("0.001"))])
    balance_after = qty()
    unit_cost = money(null=True, blank=True, default=None)
    transaction_date = models.DateField(default=timezone.localdate)
    reference_type = models.CharField(max_length=40, blank=True, default="")
    reference_number = models.CharField(max_length=80, blank=True, default="")
    purchase_request = models.ForeignKey(
        PurchaseRequest, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_transactions"
    )
    procurement_record = models.ForeignKey(
        ProcurementRecord, on_delete=models.PROTECT, null=True, blank=True, related_name="stock_transactions"
    )
    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    issued_to = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    remarks = models.TextField(blank=True, default="")
    performed_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["transaction_date", "id"]
        indexes = [models.Index(fields=["department", "item", "transaction_date"])]
        constraints = [models.CheckConstraint(condition=models.Q(quantity__gt=0), name="pm_stock_tx_qty_gt_0")]

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.tx_type in c.STOCK_INWARD else -self.quantity


# ---------------------------------------------------------------------------
# AMC / service
# ---------------------------------------------------------------------------
class AMCServiceRecord(ArchivableModel, TimeStamped):
    number = models.CharField(max_length=40, unique=True)
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_amc_records")
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="pm_amc_records")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, null=True, blank=True, related_name="amc_records")
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, null=True, blank=True, related_name="amc_records")
    contract_type = models.CharField(max_length=12, choices=c.AMCContractType.choices, default=c.AMCContractType.AMC)
    contract_reference = models.CharField(max_length=120, blank=True, default="")
    start_date = models.DateField()
    end_date = models.DateField()
    contract_value = money(validators=[MinValueValidator(ZERO)])
    gst_amount = money(validators=[MinValueValidator(ZERO)])
    total_value = money()
    coverage = models.TextField(blank=True, default="")
    status = models.CharField(max_length=10, choices=c.AMCStatus.choices, default=c.AMCStatus.ACTIVE)
    renewed_from = models.ForeignKey("self", on_delete=models.PROTECT, null=True, blank=True, related_name="renewals")
    procurement_record = models.ForeignKey(
        ProcurementRecord, on_delete=models.PROTECT, null=True, blank=True, related_name="amc_records"
    )
    legacy_amc_contract = models.ForeignKey(
        "equipment.EquipmentAMCContract", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["end_date"]
        indexes = [models.Index(fields=["department", "end_date"]), models.Index(fields=["equipment", "status"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(end_date__gte=models.F("start_date")), name="pm_amc_end_after_start")
        ]


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
class BudgetAllocation(ArchivableModel, TimeStamped):
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="pm_budgets")
    financial_year = models.CharField(max_length=7)
    funding_type = models.CharField(max_length=10, choices=c.FundingType.choices)
    laboratory = models.ForeignKey("sync.Laboratory", on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    category = models.ForeignKey(ItemCategory, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    amount = money(validators=[MinValueValidator(ZERO)])
    reference = models.CharField(max_length=120, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["-financial_year", "funding_type"]
        indexes = [models.Index(fields=["department", "financial_year", "funding_type"])]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
class ProcurementAuditLog(AppendOnlyModel):
    department = models.ForeignKey(
        "users.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="pm_audit_logs"
    )
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=40)
    object_number = models.CharField(max_length=40, blank=True, default="")
    old_value = models.JSONField(default=dict, blank=True)
    new_value = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["object_type", "object_id"]),
            models.Index(fields=["department", "created_at"]),
        ]
