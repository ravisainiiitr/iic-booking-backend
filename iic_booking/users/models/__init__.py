"""Models package for users app."""

from ..managers import UserManager
from .department import Department, DepartmentType
from .organization_request import OrganizationRequest
from .user import User, UserType, Gender
from .wallet import (
    Wallet,
    SubWallet,
    SubWalletTransaction,
    WalletRazorpayOrder,
    WalletJoinRequest,
    WalletJoinRequestStatus,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
    WalletRechargeCreditFacilityStatus,
    WalletRechargeImportRecord,
    WalletRechargeParseEntry,
    ExternalUserBankDetails,
    WalletWithdrawalRequest,
    WalletWithdrawalRequestStatus,
)
from .wallet_credit_facility_settings import WalletCreditFacilitySettings
from .wallet_student_recharge_settings import WalletStudentRechargeSettings
from .user_document import UserDocument
from .user_group import UserGroup, UserGroupMember
from .project import Project
from .auth_lock import UserLoginLock
from .auth_settings import AuthSettings
from .wallet_sric_settings import WalletSricSettings
from .user_type_inactivity import UserTypeInactivityTimeout
from .billing import ExternalBillingProfile
from .payment import (
    DepartmentPaymentReceipt,
    PaymentGatewayTransaction,
    SricTransferRequest,
)
from .equipment_supply_chain_role import (
    EquipmentSupplyChainRole,
    UserEquipmentSupplyChainRole,
)
from .rbac import PermissionDefinition, DeptAdminPermissionGrant, StaffPermissionGrant
from .admin_panel_access import AdminPanelRoleConfig

__all__ = [
    "UserManager",
    "Department",
    "DepartmentType",
    "OrganizationRequest",
    "User",
    "UserType",
    "Gender",
    "Wallet",
    "SubWallet",
    "SubWalletTransaction",
    "WalletRazorpayOrder",
    "WalletJoinRequest",
    "WalletJoinRequestStatus",
    "WalletRechargeRequest",
    "WalletRechargeRequestStatus",
    "WalletRechargeCreditFacilityStatus",
    "WalletCreditFacilitySettings",
    "WalletStudentRechargeSettings",
    "WalletRechargeImportRecord",
    "WalletRechargeParseEntry",
    "ExternalUserBankDetails",
    "WalletWithdrawalRequest",
    "WalletWithdrawalRequestStatus",
    "UserDocument",
    "UserGroup",
    "UserGroupMember",
    "Project",
    "UserLoginLock",
    "AuthSettings",
    "WalletSricSettings",
    "UserTypeInactivityTimeout",
    "ExternalBillingProfile",
    "PaymentGatewayTransaction",
    "DepartmentPaymentReceipt",
    "SricTransferRequest",
    "EquipmentSupplyChainRole",
    "UserEquipmentSupplyChainRole",
    "PermissionDefinition",
    "DeptAdminPermissionGrant",
    "StaffPermissionGrant",
    "AdminPanelRoleConfig",
]

