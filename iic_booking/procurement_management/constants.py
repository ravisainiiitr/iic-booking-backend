from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _

DISABLED_CODE = "procurement_disabled"


class ModuleRole(models.TextChoices):
    """Roles resolved per department. OPERATOR / OIC / HOD / MAIN_ADMIN are derived from existing
    portal data; OC_STORES / OFFICE / AUDITOR (and an optional HOD override) come from
    ``ProcurementRoleAssignment``."""

    LAB_OPERATOR = "LAB_OPERATOR", _("Lab Operator")
    OIC = "OIC", _("Officer in Charge")
    OC_STORES = "OC_STORES", _("OC Stores")
    OFFICE = "OFFICE", _("Office")
    HOD = "HOD", _("HOD / Competent Authority")
    AUDITOR = "AUDITOR", _("Auditor (read-only)")
    MAIN_ADMIN = "MAIN_ADMIN", _("Main Administrator")
    ACCOUNTS = "ACCOUNTS", _("Accounts In Charge")
    LAB_INCHARGE = "LAB_INCHARGE", _("Lab In Charge")


ASSIGNABLE_ROLES = (
    ModuleRole.OC_STORES,
    ModuleRole.OFFICE,
    ModuleRole.HOD,
    ModuleRole.AUDITOR,
    ModuleRole.ACCOUNTS,
    ModuleRole.LAB_INCHARGE,
)


class OfficePermission(models.TextChoices):
    """Granular permissions for the Office role (OC Stores implicitly has the stores ones)."""

    RECORD_SMALL_PURCHASE = "record_small_purchase", _("Record small purchases")
    CONSOLIDATE = "consolidate", _("Consolidate plan / non-plan requirements")
    PROCUREMENT = "procurement", _("Run procurement workspace (RFQ, quotations, PO)")
    INVOICES = "invoices", _("Record invoices and review variance")
    PAYMENTS = "payments", _("Record payments")
    ASSETS = "assets", _("Manage asset register")
    STOCK = "stock", _("Manage consumable stock")
    AMC = "amc", _("Manage AMC / service records")
    MASTERS = "masters", _("Manage item / vendor / GST masters")
    OFFLINE_APPROVAL = "offline_approval", _("Record offline HOD approvals")
    BUDGET = "budget", _("Manage budget allocations")
    REPORTS = "reports", _("View reports and exports")


ALL_OFFICE_PERMISSIONS = tuple(OfficePermission.values)
STORES_PERMISSIONS = frozenset(
    {OfficePermission.STOCK, OfficePermission.ASSETS, OfficePermission.MASTERS, OfficePermission.REPORTS}
)
ACCOUNTS_PERMISSIONS = frozenset(
    {OfficePermission.INVOICES, OfficePermission.PAYMENTS, OfficePermission.BUDGET, OfficePermission.REPORTS}
)


class ItemNature(models.TextChoices):
    CONSUMABLE = "CONSUMABLE", _("Consumable")
    NON_CONSUMABLE = "NON_CONSUMABLE", _("Non-Consumable")
    LIMITED_LIFE_ASSET = "LIMITED_LIFE_ASSET", _("Limited-Life Asset")
    MINOR_ASSET = "MINOR_ASSET", _("Minor Asset")
    MAJOR_ASSET = "MAJOR_ASSET", _("Major Asset")
    AMC_SERVICE = "AMC_SERVICE", _("AMC / Service")
    GENERAL_OFFICE = "GENERAL_OFFICE", _("General Office")


ASSET_NATURES = frozenset({ItemNature.LIMITED_LIFE_ASSET, ItemNature.MINOR_ASSET, ItemNature.MAJOR_ASSET})
STOCK_NATURES = frozenset({ItemNature.CONSUMABLE, ItemNature.GENERAL_OFFICE})


class RequestTypeCode(models.TextChoices):
    GENERAL_OFFICE = "GENERAL_OFFICE", _("General Office")
    CONSUMABLE = "CONSUMABLE", _("Consumable")
    NON_CONSUMABLE = "NON_CONSUMABLE", _("Non-Consumable")
    MINOR_ASSET = "MINOR_ASSET", _("Minor Asset")
    MAJOR_ASSET = "MAJOR_ASSET", _("Major Asset")
    LIMITED_LIFE_ASSET = "LIMITED_LIFE_ASSET", _("Limited-Life Asset")
    AMC = "AMC", _("AMC")
    REPAIR_MAINTENANCE = "REPAIR_MAINTENANCE", _("Repair / Maintenance")
    SERVICE = "SERVICE", _("Service")
    PLAN_GRANT = "PLAN_GRANT", _("Plan Grant")
    NON_PLAN = "NON_PLAN", _("Non-Plan")
    OTHER = "OTHER", _("Other")


class HodRule(models.TextChoices):
    ALWAYS = "ALWAYS", _("Always")
    ABOVE_THRESHOLD = "ABOVE_THRESHOLD", _("Above HOD approval threshold")
    NEVER = "NEVER", _("Never")


class HodApprovalMode(models.TextChoices):
    IN_APP = "IN_APP", _("In-app only")
    OFFLINE = "OFFLINE", _("Offline only (signed document upload)")
    EITHER = "EITHER", _("In-app or offline")


class VarianceAction(models.TextChoices):
    FLAG_ONLY = "FLAG_ONLY", _("Flag only")
    OFFICE_REVIEW = "OFFICE_REVIEW", _("Office review required")
    REAPPROVAL = "REAPPROVAL", _("Re-approval required")


class FundingType(models.TextChoices):
    PLAN = "PLAN", _("Plan")
    NON_PLAN = "NON_PLAN", _("Non-Plan")
    OTHER = "OTHER", _("Other / Departmental")


class RequestOrigin(models.TextChoices):
    REQUEST = "REQUEST", _("Request")
    DIRECT_PURCHASE = "DIRECT_PURCHASE", _("Direct Purchase / No Prior Request")
    PLAN_REQUIREMENT = "PLAN_REQUIREMENT", _("Plan / Non-Plan Requirement")


class Priority(models.TextChoices):
    LOW = "LOW", _("Low")
    NORMAL = "NORMAL", _("Normal")
    HIGH = "HIGH", _("High")
    URGENT = "URGENT", _("Urgent")


class RequestStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    PENDING_OIC = "PENDING_OIC", _("Pending OIC approval")
    PENDING_STORES = "PENDING_STORES", _("Pending OC Stores review")
    PENDING_HOD = "PENDING_HOD", _("Pending HOD approval")
    PENDING_ACCOUNTS = "PENDING_ACCOUNTS", _("Pending Accounts budget check")
    ON_HOLD = "ON_HOLD", _("On hold")
    REJECTED = "REJECTED", _("Rejected")
    APPROVED = "APPROVED", _("Approved")
    STORES_AVAILABLE = "STORES_AVAILABLE", _("Available in stores")
    STORES_PARTIAL = "STORES_PARTIAL", _("Partially available in stores")
    STORES_NOT_AVAILABLE = "STORES_NOT_AVAILABLE", _("Not available in stores")
    ISSUED = "ISSUED", _("Issued")
    IN_PROCUREMENT = "IN_PROCUREMENT", _("In procurement")
    AWAITING_INVOICE = "AWAITING_INVOICE", _("Purchased — awaiting bill")
    AWAITING_RECEIPT = "AWAITING_RECEIPT", _("Bill recorded — awaiting receipt / asset entry")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")


PENDING_STATUSES = frozenset(
    {RequestStatus.PENDING_OIC, RequestStatus.PENDING_STORES, RequestStatus.PENDING_ACCOUNTS, RequestStatus.PENDING_HOD}
)
TERMINAL_STATUSES = frozenset({RequestStatus.COMPLETED, RequestStatus.CANCELLED, RequestStatus.ISSUED})


class ApprovalStage(models.TextChoices):
    REQUESTER = "REQUESTER", _("Requester")
    OIC = "OIC", _("OIC")
    STORES = "STORES", _("OC Stores")
    HOD = "HOD", _("HOD / Competent Authority")
    OFFICE = "OFFICE", _("Office")
    SYSTEM = "SYSTEM", _("System")
    ACCOUNTS = "ACCOUNTS", _("Accounts In Charge")


STAGE_STATUS = {
    ApprovalStage.OIC: RequestStatus.PENDING_OIC,
    ApprovalStage.STORES: RequestStatus.PENDING_STORES,
    ApprovalStage.ACCOUNTS: RequestStatus.PENDING_ACCOUNTS,
    ApprovalStage.HOD: RequestStatus.PENDING_HOD,
}


class ApprovalActionType(models.TextChoices):
    SUBMIT = "SUBMIT", _("Submitted")
    APPROVE = "APPROVE", _("Approved")
    REJECT = "REJECT", _("Rejected")
    HOLD = "HOLD", _("Put on hold")
    RESUME = "RESUME", _("Resumed")
    RESUBMIT = "RESUBMIT", _("Resubmitted")
    CANCEL = "CANCEL", _("Cancelled")
    OFFLINE_APPROVE = "OFFLINE_APPROVE", _("Approved offline")
    OFFLINE_REJECT = "OFFLINE_REJECT", _("Rejected offline")
    STORES_AVAILABLE = "STORES_AVAILABLE", _("Stores: available")
    STORES_PARTIAL = "STORES_PARTIAL", _("Stores: partially available")
    STORES_NOT_AVAILABLE = "STORES_NOT_AVAILABLE", _("Stores: not available")
    ISSUE = "ISSUE", _("Issued from stores")
    START_PROCUREMENT = "START_PROCUREMENT", _("Procurement started")
    MARK_PURCHASED = "MARK_PURCHASED", _("Purchased")
    INVOICE_RECORDED = "INVOICE_RECORDED", _("Bill recorded")
    COMPLETE = "COMPLETE", _("Completed")
    SEND_FOR_APPROVAL = "SEND_FOR_APPROVAL", _("Sent for approval")
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED", _("Re-approval required (bill variance)")
    STORES_EDIT = "STORES_EDIT", _("Lines modified by OC Stores")


class DocumentType(models.TextChoices):
    QUOTATION = "QUOTATION", _("Quotation")
    SPECIFICATION = "SPECIFICATION", _("Specification")
    ESTIMATE = "ESTIMATE", _("Estimate")
    INVOICE = "INVOICE", _("Invoice / Bill")
    OFFLINE_APPROVAL = "OFFLINE_APPROVAL", _("Signed offline approval")
    INDENT = "INDENT", _("Indent")
    RFQ = "RFQ", _("RFQ")
    COMPARATIVE_STATEMENT = "COMPARATIVE_STATEMENT", _("Comparative statement")
    PURCHASE_ORDER = "PURCHASE_ORDER", _("Purchase order")
    DELIVERY_CHALLAN = "DELIVERY_CHALLAN", _("Delivery challan")
    INSPECTION_REPORT = "INSPECTION_REPORT", _("Inspection / acceptance report")
    PAYMENT_PROOF = "PAYMENT_PROOF", _("Payment proof")
    PROPOSAL = "PROPOSAL", _("Proposal")
    AMC_CONTRACT = "AMC_CONTRACT", _("AMC / service contract")
    ASSET_PHOTO = "ASSET_PHOTO", _("Asset photo")
    OTHER = "OTHER", _("Other")


ALLOWED_DOCUMENT_EXTENSIONS = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


class RequirementStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SUBMITTED = "SUBMITTED", _("Submitted")
    UNDER_CONSOLIDATION = "UNDER_CONSOLIDATION", _("Under consolidation")
    CONSOLIDATED = "CONSOLIDATED", _("Consolidated")
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL", _("Sent for approval")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")
    ON_HOLD = "ON_HOLD", _("On hold")
    PROCUREMENT_IN_PROGRESS = "PROCUREMENT_IN_PROGRESS", _("Procurement in progress")
    PROCURED = "PROCURED", _("Procured")
    CLOSED = "CLOSED", _("Closed")
    REMOVED = "REMOVED", _("Removed (soft)")
    MERGED = "MERGED", _("Merged into another requirement")


class ProposalStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    CONSOLIDATED = "CONSOLIDATED", _("Consolidated")
    SENT_FOR_APPROVAL = "SENT_FOR_APPROVAL", _("Sent for approval")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")
    ON_HOLD = "ON_HOLD", _("On hold")
    CLOSED = "CLOSED", _("Closed")


class RequirementChangeType(models.TextChoices):
    ADD = "ADD", _("Added by office")
    EDIT = "EDIT", _("Edited")
    REMOVE = "REMOVE", _("Removed")
    RESTORE = "RESTORE", _("Restored")
    MERGE = "MERGE", _("Merged")
    SPLIT = "SPLIT", _("Split")
    STATUS = "STATUS", _("Status changed")


class ProcurementStep(models.TextChoices):
    INDENT = "INDENT", _("Indent")
    SPECIFICATION = "SPECIFICATION", _("Specification")
    RFQ = "RFQ", _("RFQ")
    QUOTATIONS = "QUOTATIONS", _("Quotations")
    COMPARATIVE = "COMPARATIVE", _("Comparative statement")
    VENDOR_SELECTION = "VENDOR_SELECTION", _("Vendor selection")
    PURCHASE_ORDER = "PURCHASE_ORDER", _("Purchase order")
    DELIVERY = "DELIVERY", _("Delivery")
    INSPECTION = "INSPECTION", _("Inspection / acceptance")
    INVOICE = "INVOICE", _("Invoice")
    PAYMENT = "PAYMENT", _("Payment")
    STOCK_ASSET_ENTRY = "STOCK_ASSET_ENTRY", _("Stock / asset entry")


PROCUREMENT_STEP_ORDER = [s.value for s in ProcurementStep]


class ProcurementRecordStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    PO_ISSUED = "PO_ISSUED", _("PO issued")
    DELIVERED = "DELIVERED", _("Delivered")
    INVOICED = "INVOICED", _("Invoiced")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")


class InspectionResult(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    ACCEPTED = "ACCEPTED", _("Accepted")
    PARTIALLY_ACCEPTED = "PARTIALLY_ACCEPTED", _("Partially accepted")
    REJECTED = "REJECTED", _("Rejected")


class PaymentStatus(models.TextChoices):
    UNPAID = "UNPAID", _("Unpaid")
    PARTIALLY_PAID = "PARTIALLY_PAID", _("Partially paid")
    PAID = "PAID", _("Paid")


class Compliance(models.TextChoices):
    COMPLIANT = "COMPLIANT", _("Compliant")
    PARTIAL = "PARTIAL", _("Partially compliant")
    NON_COMPLIANT = "NON_COMPLIANT", _("Non-compliant")


class SupplyType(models.TextChoices):
    INTRA_STATE = "INTRA_STATE", _("Intra-state (CGST + SGST)")
    INTER_STATE = "INTER_STATE", _("Inter-state (IGST)")


class VarianceStatus(models.TextChoices):
    NOT_APPLICABLE = "NOT_APPLICABLE", _("No approved amount")
    WITHIN_TOLERANCE = "WITHIN_TOLERANCE", _("Within tolerance")
    FLAGGED = "FLAGGED", _("Flagged")
    OFFICE_REVIEW = "OFFICE_REVIEW", _("Pending office review")
    REAPPROVAL_REQUIRED = "REAPPROVAL_REQUIRED", _("Re-approval required")
    CLEARED = "CLEARED", _("Cleared")


class AssetStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    IN_STORE = "IN_STORE", _("In Store")
    UNDER_INSTALLATION = "UNDER_INSTALLATION", _("Under Installation")
    IN_USE = "IN_USE", _("In Use")
    UNDER_REPAIR = "UNDER_REPAIR", _("Under Repair")
    UNDER_AMC = "UNDER_AMC", _("Under AMC")
    TEMPORARILY_TRANSFERRED = "TEMPORARILY_TRANSFERRED", _("Temporarily Transferred")
    PERMANENTLY_TRANSFERRED = "PERMANENTLY_TRANSFERRED", _("Permanently Transferred")
    LOST = "LOST", _("Lost")
    DAMAGED = "DAMAGED", _("Damaged")
    CONDEMNED = "CONDEMNED", _("Condemned")
    DISPOSED = "DISPOSED", _("Disposed")
    RETIRED = "RETIRED", _("Retired")


ASSET_FINAL_STATUSES = frozenset({AssetStatus.DISPOSED, AssetStatus.RETIRED})
ASSET_TRANSFER_STATUSES = frozenset({AssetStatus.TEMPORARILY_TRANSFERRED, AssetStatus.PERMANENTLY_TRANSFERRED})


class TransferType(models.TextChoices):
    TEMPORARY = "TEMPORARY", _("Temporary")
    PERMANENT = "PERMANENT", _("Permanent")


class TransferStatus(models.TextChoices):
    REQUESTED = "REQUESTED", _("Requested")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")
    COMPLETED = "COMPLETED", _("Completed")
    RETURNED = "RETURNED", _("Returned")
    CANCELLED = "CANCELLED", _("Cancelled")


class StockTxType(models.TextChoices):
    OPENING = "OPENING", _("Opening balance")
    RECEIPT = "RECEIPT", _("Receipt")
    ISSUE = "ISSUE", _("Issue")
    ADJUSTMENT_IN = "ADJUSTMENT_IN", _("Adjustment (+)")
    ADJUSTMENT_OUT = "ADJUSTMENT_OUT", _("Adjustment (−)")
    RETURN = "RETURN", _("Return to stores")


STOCK_INWARD = frozenset({StockTxType.OPENING, StockTxType.RECEIPT, StockTxType.ADJUSTMENT_IN, StockTxType.RETURN})


class AMCContractType(models.TextChoices):
    AMC = "AMC", _("AMC")
    CMC = "CMC", _("CMC")
    WARRANTY = "WARRANTY", _("Warranty / extended warranty")
    SERVICE = "SERVICE", _("Service contract")
    CALIBRATION = "CALIBRATION", _("Calibration")
    REPAIR = "REPAIR", _("Repair")


class AMCStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    EXPIRED = "EXPIRED", _("Expired")
    RENEWED = "RENEWED", _("Renewed")
    CANCELLED = "CANCELLED", _("Cancelled")


class NumberPrefix(models.TextChoices):
    REQUEST = "REQ", _("Request")
    PLAN = "PLAN", _("Plan requirement")
    NON_PLAN = "NP", _("Non-plan requirement")
    PROPOSAL = "PROP", _("Plan / non-plan proposal")
    PROCUREMENT = "PROC", _("Procurement record")
    SMALL_PURCHASE = "SP", _("Small purchase")
    ASSET = "AST", _("Asset")
    AMC = "AMC", _("AMC / service record")
    ITEM = "ITM", _("Item")
    VENDOR = "VEN", _("Vendor")
    TRANSFER = "TRF", _("Asset transfer")
    STOCK = "STK", _("Stock transaction")
    MAINTENANCE = "MNT", _("Maintenance record")
    VERIFICATION = "PV", _("Physical verification")
    DISPOSAL = "DSP", _("Condemnation / disposal")


# ---------------------------------------------------------------------------
# Registers, verification, disposal (GFR 2017 Rules 211-217)
# ---------------------------------------------------------------------------
class RegisterType(models.TextChoices):
    MAJOR = "MAJOR", _("Major (fixed / non-consumable assets)")
    MINOR = "MINOR", _("Minor (low-value / dead stock)")
    LIMITED_LIFE = "LIMITED_LIFE", _("Limited-life assets")
    CONSUMABLE = "CONSUMABLE", _("Consumable stock")


REGISTER_TAG_CODE = {
    RegisterType.MAJOR: "MAJ",
    RegisterType.MINOR: "MIN",
    RegisterType.LIMITED_LIFE: "LLA",
    RegisterType.CONSUMABLE: "CON",
}
REGISTER_CATEGORY_NATURE = {
    RegisterType.MAJOR: ItemNature.MAJOR_ASSET,
    RegisterType.MINOR: ItemNature.MINOR_ASSET,
    RegisterType.LIMITED_LIFE: ItemNature.LIMITED_LIFE_ASSET,
}


class AssetCondition(models.TextChoices):
    NEW = "NEW", _("New")
    GOOD = "GOOD", _("Good / serviceable")
    FAIR = "FAIR", _("Fair")
    POOR = "POOR", _("Poor / needs repair")
    UNSERVICEABLE = "UNSERVICEABLE", _("Unserviceable")


class VerificationResult(models.TextChoices):
    FOUND = "FOUND", _("Found (in order)")
    FOUND_DAMAGED = "FOUND_DAMAGED", _("Found — damaged / unserviceable")
    SHORTAGE = "SHORTAGE", _("Found — quantity short")
    NOT_FOUND = "NOT_FOUND", _("Not found")


class VerificationMethod(models.TextChoices):
    SCAN = "SCAN", _("QR scan")
    MANUAL = "MANUAL", _("Manual")
    IMPORT = "IMPORT", _("Imported")


class CampaignStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    CLOSED = "CLOSED", _("Closed")


class DisposalAction(models.TextChoices):
    CONDEMN = "CONDEMN", _("Condemned (survey / condemnation board)")
    WRITE_OFF = "WRITE_OFF", _("Written off (loss / shortage)")
    DISPOSE = "DISPOSE", _("Disposed")


class DisposalMode(models.TextChoices):
    AUCTION = "AUCTION", _("Public auction / e-auction")
    SCRAP = "SCRAP", _("Sold as scrap")
    BUY_BACK = "BUY_BACK", _("Buy-back / exchange")
    TRANSFER = "TRANSFER", _("Transferred (free of cost)")
    WRITE_OFF = "WRITE_OFF", _("Written off")
    OTHER = "OTHER", _("Other")


DISPOSAL_STATUS = {
    DisposalAction.CONDEMN: AssetStatus.CONDEMNED,
    DisposalAction.WRITE_OFF: AssetStatus.DISPOSED,
    DisposalAction.DISPOSE: AssetStatus.DISPOSED,
}


# ---------------------------------------------------------------------------
# Inventory linkage and stock reasons
# ---------------------------------------------------------------------------
class LinkUsage(models.TextChoices):
    CONSUMABLE = "CONSUMABLE", _("Consumable")
    SPARE = "SPARE", _("Spare part")
    ACCESSORY = "ACCESSORY", _("Accessory")


class StockReason(models.TextChoices):
    DAMAGED = "DAMAGED", _("Damaged / broken")
    EXPIRED = "EXPIRED", _("Expired")
    COUNT_CORRECTION = "COUNT_CORRECTION", _("Physical count correction")
    LOST = "LOST", _("Lost / pilferage")
    CONSUMED_IN_REPAIR = "CONSUMED_IN_REPAIR", _("Used in repair / maintenance")
    OTHER = "OTHER", _("Other")


class LineFulfilment(models.TextChoices):
    UNDECIDED = "", _("Not decided")
    STOCK = "STOCK", _("Issue from stock")
    PROCURE = "PROCURE", _("Procure")


# ---------------------------------------------------------------------------
# Procurement mode (GFR 2017 Rules 149, 154, 155, 161-166; thresholds configurable)
# ---------------------------------------------------------------------------
class PurchaseMode(models.TextChoices):
    DIRECT = "DIRECT", _("Direct purchase (without quotation)")
    GEM = "GEM", _("GeM (Government e-Marketplace)")
    PURCHASE_COMMITTEE = "PURCHASE_COMMITTEE", _("Local Purchase Committee")
    LIMITED_TENDER = "LIMITED_TENDER", _("Limited tender")
    OPEN_TENDER = "OPEN_TENDER", _("Open / advertised tender")
    SINGLE_TENDER = "SINGLE_TENDER", _("Single tender")
    PROPRIETARY = "PROPRIETARY", _("Proprietary article (PAC)")
    RATE_CONTRACT = "RATE_CONTRACT", _("Rate contract")


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------
class MaintenanceKind(models.TextChoices):
    BREAKDOWN = "BREAKDOWN", _("Breakdown repair")
    PREVENTIVE = "PREVENTIVE", _("Preventive maintenance")
    CALIBRATION = "CALIBRATION", _("Calibration")
    AMC_VISIT = "AMC_VISIT", _("AMC / CMC visit")
    UPGRADE = "UPGRADE", _("Upgrade / modification")
    OTHER = "OTHER", _("Other")
