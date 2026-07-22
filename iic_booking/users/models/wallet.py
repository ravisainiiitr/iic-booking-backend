"""Wallet and SubWallet models. Main wallet is a container; all balance and transactions are in sub-wallets."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import CASCADE
from django.db.models import CharField
from django.db.models import DateField
from django.db.models import DecimalField
from django.db.models import DateTimeField
from django.db.models import ForeignKey
from django.db.models import IntegerField
from django.db.models import Model
from django.db.models import OneToOneField
from django.db.models import PROTECT
from django.db.models import SET_NULL
from django.db.models import TextField
from django.db.models import BooleanField
from django.db.models import JSONField
from django.db.models import F
from django.db.models import Sum
from django.db.models import UniqueConstraint
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
import secrets
from datetime import timedelta

from .user import User
from .user_type import UserType
from .department import Department, DepartmentType


class Wallet(Model):
    """Wallet container for a user. Balance and transactions live only in sub-wallets (per department with equipment)."""

    user = OneToOneField(
        User,
        on_delete=CASCADE,
        related_name="wallet",
        verbose_name=_("User"),
        help_text=_("User who owns this wallet"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Wallet")
        verbose_name_plural = _("Wallets")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Wallet for {self.user.email}"

    @property
    def total_balance(self) -> Decimal:
        """Consolidated balance: sum of all sub-wallet balances."""
        result = self.sub_wallets.aggregate(total=Sum("balance"))
        return result["total"] or Decimal("0.00")

    def clean(self) -> None:
        """Validate that user can have a wallet."""
        if self.user and not self.user.can_have_wallet():
            raise ValidationError(
                _("Only Individual Student, Faculty, and External user types (Educational Institute, R&D Center, Institute, Other) can have their own wallets. Regular Students use faculty wallets.")
            )

    def save(self, *args, **kwargs) -> None:
        """Override save to validate user type."""
        self.full_clean()
        super().save(*args, **kwargs)

    def has_student_access(self, student: User) -> bool:
        """Check if a regular student or 'Other' user has access to this wallet.
        
        Args:
            student: User to check (must be regular STUDENT or OTHER)
            
        Returns:
            bool: True if user has approved access to this wallet
        """
        # Only regular STUDENT and OTHER users can access faculty wallets
        if not student or student.user_type not in {UserType.STUDENT, UserType.OTHER}:
            return False
        
        return WalletJoinRequest.objects.filter(
            student=student,
            wallet=self,
            status=WalletJoinRequestStatus.APPROVED
        ).exists()


class SubWallet(Model):
    """Sub-wallet tied to a main wallet and an internal department. Used for department-wise fund management and equipment booking deductions."""

    wallet = ForeignKey(
        Wallet,
        on_delete=CASCADE,
        related_name="sub_wallets",
        verbose_name=_("Wallet"),
        help_text=_("Main wallet this sub-wallet belongs to"),
    )
    department = ForeignKey(
        Department,
        on_delete=CASCADE,
        related_name="sub_wallets",
        verbose_name=_("Internal Department"),
        help_text=_("Internal department this sub-wallet is for"),
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    balance = DecimalField(
        _("Balance"),
        max_digits=10,
        decimal_places=2,
        default=0.00,
        help_text=_("Current sub-wallet balance for this department"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Sub-Wallet")
        verbose_name_plural = _("Sub-Wallets")
        ordering = ["department__name"]
        constraints = [
            UniqueConstraint(
                fields=["wallet", "department"],
                name="unique_wallet_department_subwallet",
            )
        ]

    def __str__(self) -> str:
        return f"Sub-wallet {self.department.name} for {self.wallet.user.email} - ₹{self.balance}"

    def recalculate_balance(self) -> Decimal:
        """Recalculate balance from all sub-wallet transactions."""
        total = Decimal("0.00")
        for txn in self.transactions.all():
            if txn.transaction_type == SubWalletTransaction.TransactionType.CREDIT:
                total += txn.amount
            elif txn.transaction_type == SubWalletTransaction.TransactionType.DEBIT:
                total -= txn.amount
        SubWallet.objects.filter(id=self.id).update(balance=total)
        self.refresh_from_db()
        return self.balance

    def credit(self, amount, description: str = "", related_user=None) -> "SubWalletTransaction":
        """Add money to this sub-wallet. related_user: user associated with this transaction (for shared-wallet filtering)."""
        amount_decimal = Decimal(str(amount))
        if amount_decimal <= 0:
            raise ValueError("Amount must be positive")
        SubWallet.objects.filter(id=self.id).update(balance=F("balance") + amount_decimal)
        self.refresh_from_db()
        txn = SubWalletTransaction.objects.create(
            sub_wallet=self,
            transaction_type=SubWalletTransaction.TransactionType.CREDIT,
            amount=amount_decimal,
            description=description,
            related_user=related_user,
        )
        try:
            from iic_booking.users.department_faculty_credit_facility import on_subwallet_credited

            on_subwallet_credited(self, amount_decimal, source="subwallet.credit")
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "department faculty credit recovery failed after credit on subwallet %s", self.id
            )
        return txn

    def debit(
        self,
        amount,
        description: str = "",
        related_user=None,
        *,
        minimum_balance_after: Optional[Decimal] = None,
    ) -> "SubWalletTransaction":
        """Deduct money from this sub-wallet. related_user: user associated with this transaction (for shared-wallet filtering).

        minimum_balance_after: lowest allowed balance after debit (default 0). Use a negative floor
        when an active credit facility allows temporary overdraft.
        """
        amount_decimal = Decimal(str(amount))
        if amount_decimal <= 0:
            raise ValueError("Amount must be positive")
        floor = Decimal("0.00") if minimum_balance_after is None else Decimal(str(minimum_balance_after))
        self.refresh_from_db()
        new_balance = self.balance - amount_decimal
        if new_balance < floor:
            raise ValueError("Insufficient balance")
        SubWallet.objects.filter(id=self.id).update(balance=F("balance") - amount_decimal)
        self.refresh_from_db()
        txn = SubWalletTransaction.objects.create(
            sub_wallet=self,
            transaction_type=SubWalletTransaction.TransactionType.DEBIT,
            amount=amount_decimal,
            description=description,
            related_user=related_user,
        )
        try:
            from iic_booking.users.department_faculty_credit_facility import on_subwallet_debited

            on_subwallet_debited(self, amount_decimal)
        except Exception:
            import logging

            logging.getLogger(__name__).exception(
                "department faculty credit activation failed after debit on subwallet %s", self.id
            )
        return txn


class SubWalletTransaction(Model):
    """Transaction for a sub-wallet (department-wise)."""

    class TransactionType:
        CREDIT = "credit"
        DEBIT = "debit"
        CHOICES = [
            (CREDIT, _("Credit")),
            (DEBIT, _("Debit")),
        ]

    sub_wallet = ForeignKey(
        SubWallet,
        on_delete=PROTECT,
        related_name="transactions",
        verbose_name=_("Sub-Wallet"),
    )
    transaction_type = CharField(
        _("Transaction Type"),
        max_length=50,
        choices=TransactionType.CHOICES,
    )
    amount = DecimalField(
        _("Amount"),
        max_digits=10,
        decimal_places=2,
    )
    description = TextField(_("Description"), blank=True)
    # When set: transaction is attributed to this user (e.g. booking by student on faculty wallet).
    # Used so students on a shared wallet only see their own transactions; faculty sees all.
    related_user = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="related_sub_wallet_transactions",
        verbose_name=_("Related user"),
        help_text=_("User who initiated or is associated with this transaction (for filtering student view on shared wallets)"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Sub-Wallet Transaction")
        verbose_name_plural = _("Sub-Wallet Transactions")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.transaction_type} - {self.amount} - {self.sub_wallet.department.name}"


class WalletRazorpayOrder(Model):
    """Stores Razorpay order_id so we can credit the correct sub-wallet on verify."""

    wallet = ForeignKey(
        Wallet,
        on_delete=CASCADE,
        related_name="razorpay_orders",
        verbose_name=_("Wallet"),
    )
    department = ForeignKey(
        Department,
        on_delete=CASCADE,
        related_name="razorpay_recharge_orders",
        verbose_name=_("Department (sub-wallet)"),
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    amount_paise = IntegerField(_("Amount (paise)"), help_text=_("Order amount in paise"))
    order_id = CharField(_("Razorpay Order ID"), max_length=255, unique=True, db_index=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet Razorpay Order")
        verbose_name_plural = _("Wallet Razorpay Orders")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Order {self.order_id} → {self.department.name}"


class WalletJoinRequestStatus(models.TextChoices):
    """Status choices for wallet join requests."""
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    CANCELLED = 'CANCELLED', _('Cancelled')


class WalletJoinRequest(Model):
    """Model for students to request joining a faculty's wallet."""
    
    student = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name='wallet_join_requests',
        verbose_name=_("Student"),
        help_text=_("Student requesting to join the wallet"),
    )
    faculty = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name='wallet_join_requests_received',
        verbose_name=_("Faculty"),
        help_text=_("Faculty member whose wallet the student wants to join"),
    )
    wallet = ForeignKey(
        Wallet,
        on_delete=CASCADE,
        related_name='join_requests',
        verbose_name=_("Wallet"),
        help_text=_("Wallet the student wants to join"),
        null=True,
        blank=True,
    )
    status = CharField(
        _("Status"),
        max_length=20,
        choices=WalletJoinRequestStatus.choices,
        default=WalletJoinRequestStatus.PENDING,
        help_text=_("Status of the join request"),
    )
    message = TextField(
        _("Message"),
        blank=True,
        help_text=_("Optional message from student"),
    )
    faculty_response = TextField(
        _("Faculty Response"),
        blank=True,
        help_text=_("Optional response message from faculty"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    responded_at = DateTimeField(
        _("Responded at"),
        null=True,
        blank=True,
        help_text=_("When the faculty responded to the request"),
    )
    
    class Meta:
        verbose_name = _("Wallet Join Request")
        verbose_name_plural = _("Wallet Join Requests")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['student', 'status']),
            models.Index(fields=['faculty', 'status']),
        ]
    
    def __str__(self) -> str:
        return f"{self.student.email} -> {self.faculty.email} ({self.status})"
    
    def clean(self) -> None:
        """Validate wallet join request."""
        # Regular STUDENT and OTHER users can request to join faculty wallets
        # INDIVIDUAL_STUDENT and other external users have their own wallets and cannot join
        allowed_types = {UserType.STUDENT, UserType.OTHER}
        if self.student and self.student.user_type not in allowed_types:
            raise ValidationError(_("Only regular students and 'Other' type users can request to join a faculty wallet. Other user types have their own wallets."))
        
        # Validate faculty is actually a faculty
        if self.faculty and self.faculty.user_type != UserType.FACULTY:
            raise ValidationError(_("Only faculty members can have wallets that students can join."))
        
        # Validate faculty has a wallet and set it if not set
        if self.faculty:
            try:
                faculty_wallet = self.faculty.wallet
                if not self.wallet:
                    self.wallet = faculty_wallet
            except Wallet.DoesNotExist:
                raise ValidationError(_("The faculty member does not have a wallet."))
    
    def save(self, *args, **kwargs) -> None:
        """Override save to validate and set wallet."""
        self.full_clean()
        super().save(*args, **kwargs)
    
    def approve(self, response_message: str = "") -> None:
        """Approve the join request."""
        from django.utils import timezone
        self.status = WalletJoinRequestStatus.APPROVED
        self.faculty_response = response_message
        self.responded_at = timezone.now()
        self.save()
    
    def reject(self, response_message: str = "") -> None:
        """Reject the join request."""
        from django.utils import timezone
        self.status = WalletJoinRequestStatus.REJECTED
        self.faculty_response = response_message
        self.responded_at = timezone.now()
        self.save()
    
    def cancel(self) -> None:
        """Cancel the join request (by student)."""
        self.status = WalletJoinRequestStatus.CANCELLED
        self.save()
    
    def remove(self, response_message: str = "") -> None:
        """Remove the student from the wallet (by faculty).
        
        This is used when faculty wants to remove a student who has approved access.
        """
        from django.utils import timezone
        self.status = WalletJoinRequestStatus.CANCELLED
        self.faculty_response = response_message or "You have been removed from this wallet."
        self.responded_at = timezone.now()
        self.save()


class WalletRechargeMode(models.TextChoices):
    """Offline wallet recharge modes."""

    PROJECT_GRANT = "project_grant", _("Recharge via Project Grant")
    DIRECT_CASH_DEPOSIT = "direct_cash_deposit", _("Direct Cash Deposit / Bank Transfer")


class WalletRechargeRequestStatus(models.TextChoices):
    """Status choices for wallet recharge requests."""
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    CANCELLED = 'CANCELLED', _('Cancelled')


class WalletRechargeRejectionReason(models.TextChoices):
    """Predefined rejection reasons for SRIC / admin email rejection form."""

    WRONG_PROJECT_GRANT = "wrong_project_grant", _("Wrong Project Grant Code")
    INSUFFICIENT_BALANCE = "insufficient_balance", _("Insufficient Balance in Project Grant")
    MISMATCH_USER_INFO = "mismatch_user_info", _("Mismatch in User Information")
    OTHER = "other", _("Others")


class WalletRechargeCancellationSource(models.TextChoices):
    """Who cancelled a pending recharge request."""

    USER = "user", _("Cancelled by User")
    ADMIN = "admin", _("Cancelled by Administrator")
    DEPT_ADMIN = "dept_admin", _("Cancelled by Department Administrator")
    SYSTEM = "system", _("Cancelled by System")


class WalletRechargeCreditFacilityStatus(models.TextChoices):
    """Temporary overdraft tied to a pending recharge request (parse confirmation)."""

    INACTIVE = "inactive", _("Inactive")
    ACTIVE = "active", _("Active — credit window open")
    EXPIRED_UNPAID = "expired_unpaid", _("Window ended without parse credit — bookings on hold")


class WalletRechargeRequest(Model):
    """Internal user wallet recharge request with secure email approval workflow."""
    
    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name='wallet_recharge_requests',
        verbose_name=_("User"),
        help_text=_("User requesting wallet recharge"),
    )
    wallet = ForeignKey(
        Wallet,
        on_delete=CASCADE,
        related_name='recharge_requests',
        verbose_name=_("Wallet"),
        help_text=_("Wallet to be recharged"),
    )
    department = ForeignKey(
        Department,
        on_delete=CASCADE,
        related_name='recharge_requests',
        verbose_name=_("Department (sub-wallet to credit)"),
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
        null=True,
        blank=True,
        help_text=_("Internal department whose sub-wallet will be credited. Required for all new recharge requests."),
    )
    amount = DecimalField(
        _("Amount"),
        max_digits=10,
        decimal_places=2,
        help_text=_("Amount to recharge"),
    )
    project = ForeignKey(
        'users.Project',
        on_delete=SET_NULL,
        blank=True,
        null=True,
        related_name='recharge_requests',
        verbose_name=_("Project"),
        help_text=_("Optional project associated with the recharge"),
    )
    project_details = TextField(
        _("Project Details"),
        blank=True,
        help_text=_("Optional project details for the recharge (deprecated, use project field instead)"),
    )
    # Audit snapshots at request time
    employee_number = CharField(
        _("Employee Number"),
        max_length=50,
        blank=True,
        help_text=_("Snapshot of user emp_id / employee number at request time"),
    )
    user_department_name = CharField(
        _("User Department"),
        max_length=255,
        blank=True,
        help_text=_("Snapshot of the requesting user's home department name"),
    )
    department_grant_code = CharField(
        _("Department Grant Code (Credit)"),
        max_length=100,
        blank=True,
        help_text=_("Wallet credit grant code for the selected recharge department"),
    )
    project_grant_code = CharField(
        _("Project Grant Code (Debit)"),
        max_length=100,
        blank=True,
        help_text=_("Project grant code used for debit (typically project_code)"),
    )
    recharge_mode = CharField(
        _("Recharge Mode"),
        max_length=32,
        choices=WalletRechargeMode.choices,
        default=WalletRechargeMode.PROJECT_GRANT,
        db_index=True,
        help_text=_(
            "Offline recharge path: Project Grant (faculty) or Direct Cash Deposit / Bank Transfer "
            "(all internal users)."
        ),
    )
    undertaking_accepted = BooleanField(
        _("Undertaking Accepted"),
        default=False,
        help_text=_(
            "User acknowledged that Direct Cash Deposit / Bank Transfer is used only when no "
            "active project grant is available."
        ),
    )
    fund_receipt_verified = BooleanField(
        _("Fund Receipt Verified"),
        default=False,
        help_text=_(
            "Department Account In-charge confirmed that funds were credited to the department "
            "grant/account (final financial verification)."
        ),
    )
    fund_receipt_verified_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_recharge_fund_verifications",
        verbose_name=_("Fund Receipt Verified By"),
    )
    fund_receipt_verified_at = DateTimeField(
        _("Fund Receipt Verified At"),
        null=True,
        blank=True,
    )
    fund_receipt_verification_remarks = TextField(
        _("Fund Receipt Verification Remarks"),
        blank=True,
    )
    account_incharge = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_recharge_requests_as_incharge",
        verbose_name=_("Department Account In-charge"),
        help_text=_(
            "Department Account In-charge notified for this request. This role monitors and manages "
            "department financial activities including wallet recharges, grant utilization, wallet "
            "transactions, credit facility usage, and related department financial records."
        ),
    )
    action_token = CharField(
        _("Action Token"),
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Secure token for email Approve / Reject links"),
    )
    status = CharField(
        _("Status"),
        max_length=20,
        choices=WalletRechargeRequestStatus.choices,
        default=WalletRechargeRequestStatus.PENDING,
        help_text=_("Status of the recharge request"),
    )
    user_otp_code = CharField(
        _("User OTP Code"),
        max_length=6,
        blank=True,
        help_text=_("OTP code sent to user's email for verification"),
    )
    user_otp_expires_at = DateTimeField(
        _("User OTP Expires At"),
        null=True,
        blank=True,
        help_text=_("When the user OTP expires"),
    )
    user_otp_verified = BooleanField(
        _("User OTP Verified"),
        default=False,
        help_text=_("Whether user has verified their OTP"),
    )
    sric_notification_sent = BooleanField(
        _("SRIC office notification sent"),
        default=False,
        help_text=_(
            "Set when the SRIC Office approval email has been sent for this request."
        ),
    )
    credit_facility_opted_in = BooleanField(
        _("Credit facility opted in"),
        default=False,
        help_text=_(
            "Faculty chose temporary credit line when balance was below threshold at OTP send time."
        ),
    )
    credit_limit_amount = DecimalField(
        _("Credit facility limit (₹)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Max overdraft: min(admin cap, requested amount). Set when OTP is verified."),
    )
    credit_window_ends_at = DateTimeField(
        _("Credit facility window ends at"),
        null=True,
        blank=True,
        help_text=_("Parse credit expected before this time (from settings at activation)."),
    )
    credit_facility_status = CharField(
        _("Credit facility status"),
        max_length=20,
        choices=WalletRechargeCreditFacilityStatus.choices,
        default=WalletRechargeCreditFacilityStatus.INACTIVE,
        help_text=_("Tracks temporary overdraft lifecycle for this request."),
    )
    credit_expiry_notified_at = DateTimeField(
        _("Credit expiry notification sent at"),
        null=True,
        blank=True,
        help_text=_("When the booking-hold email was sent after the window expired."),
    )
    otp_code = CharField(
        _("OTP Code"),
        max_length=6,
        blank=True,
        help_text=_("Deprecated: accounts team OTP (no longer required for email approval)"),
    )
    otp_expires_at = DateTimeField(
        _("OTP Expires At"),
        null=True,
        blank=True,
        help_text=_("Deprecated: when the accounts team OTP expires"),
    )
    approved_by_email = CharField(
        _("Approved / Rejected By Email"),
        max_length=255,
        blank=True,
        help_text=_("Email address that approved/rejected/cancelled the request"),
    )
    processed_by = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="processed_wallet_recharge_requests",
        verbose_name=_("Processed By"),
        help_text=_("Authenticated user who approved/rejected/cancelled (if any)"),
    )
    response_message = TextField(
        _("Response Message"),
        blank=True,
        help_text=_("Optional response / notes from approver or rejector"),
    )
    rejection_reason_code = CharField(
        _("Rejection Reason Code"),
        max_length=40,
        choices=WalletRechargeRejectionReason.choices,
        blank=True,
        help_text=_("Predefined rejection reason when status is Rejected"),
    )
    rejection_reason_text = TextField(
        _("Rejection Reason Text"),
        blank=True,
        help_text=_("Free-text rejection reason (required when reason is Others)"),
    )
    cancellation_source = CharField(
        _("Cancellation Source"),
        max_length=20,
        choices=WalletRechargeCancellationSource.choices,
        blank=True,
        help_text=_("Who cancelled the pending request"),
    )
    utr_reference = CharField(
        _("UTR / Transfer Reference"),
        max_length=255,
        blank=True,
        help_text=_("Bank UTR submitted by user for offline deposit (govt / NEFT)"),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    responded_at = DateTimeField(
        _("Responded at"),
        null=True,
        blank=True,
        help_text=_("When the request was approved/rejected/cancelled"),
    )
    
    class Meta:
        verbose_name = _("Wallet Recharge Request")
        verbose_name_plural = _("Wallet Recharge Requests")
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'created_at']),
        ]
    
    def __str__(self) -> str:
        dept_name = self.department.name if self.department else "No Department"
        return f"Recharge Request: {self.user.email} - ₹{self.amount} - {dept_name} ({self.status})"

    @property
    def request_id_display(self) -> str:
        return f"WRR-{self.pk}" if self.pk else "WRR-—"
    
    def clean(self) -> None:
        """Validate that department is provided for new recharge requests."""
        if not self.department and not self.pk:  # Only enforce for new records
            raise ValidationError(_("Department is required for recharge requests. Recharge requests are now department-specific (sub-wallet based)."))
    
    def save(self, *args, **kwargs) -> None:
        """Override save to validate department and ensure action token."""
        if not self.action_token:
            self.action_token = secrets.token_urlsafe(32)
        self.full_clean()
        super().save(*args, **kwargs)
    
    def generate_user_otp(self) -> str:
        """Generate a 6-digit OTP for user verification and set expiry (10 minutes)."""
        otp = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
        self.user_otp_code = otp
        self.user_otp_expires_at = timezone.now() + timedelta(minutes=10)
        self.user_otp_verified = False
        self.save(update_fields=['user_otp_code', 'user_otp_expires_at', 'user_otp_verified'])
        return otp
    
    def verify_user_otp(self, otp: str) -> bool:
        """Verify if the provided user OTP is valid and not expired."""
        if not self.user_otp_code or not self.user_otp_expires_at:
            return False
        
        if timezone.now() > self.user_otp_expires_at:
            return False
        
        if self.user_otp_verified:
            return False  # Already verified
        
        return self.user_otp_code == otp
    
    def mark_user_otp_verified(self) -> None:
        """Mark user OTP as verified."""
        self.user_otp_verified = True
        self.save(update_fields=['user_otp_verified'])
    
    def generate_otp(self) -> str:
        """Deprecated accounts OTP — retained for compatibility."""
        otp = ''.join([str(secrets.randbelow(10)) for _ in range(6)])
        self.otp_code = otp
        self.otp_expires_at = timezone.now() + timedelta(minutes=15)
        self.save(update_fields=['otp_code', 'otp_expires_at'])
        return otp
    
    def verify_otp(self, otp: str) -> bool:
        """Deprecated accounts OTP verify."""
        if not self.otp_code or not self.otp_expires_at:
            return False
        
        if timezone.now() > self.otp_expires_at:
            return False
        
        return self.otp_code == otp
    
    def approve(self, response_message: str = "", *, actor=None, actor_email: str = "") -> None:
        """Approve and credit wallet. Prefer wallet_recharge_workflow.approve_request for locking."""
        from iic_booking.users.wallet_recharge_workflow import approve_request

        approve_request(
            self,
            response_message=response_message,
            actor=actor,
            actor_email=actor_email,
        )
    
    def reject(
        self,
        response_message: str,
        *,
        reason_code: str = "",
        actor=None,
        actor_email: str = "",
    ) -> None:
        """Reject the request. Prefer wallet_recharge_workflow.reject_request for locking."""
        from iic_booking.users.wallet_recharge_workflow import reject_request

        reject_request(
            self,
            reason_code=reason_code or WalletRechargeRejectionReason.OTHER,
            reason_text=response_message,
            actor=actor,
            actor_email=actor_email,
        )
    
    def cancel(self, *, source: str = "", actor=None, actor_email: str = "", note: str = "") -> None:
        """Cancel a pending request (keeps the row for audit). Prefer workflow.cancel_request."""
        from iic_booking.users.wallet_recharge_workflow import cancel_request

        cancel_request(
            self,
            source=source or WalletRechargeCancellationSource.USER,
            actor=actor,
            actor_email=actor_email,
            note=note,
        )


class WalletRechargeRequestAuditLog(Model):
    """Immutable audit trail for wallet recharge request status changes."""

    request = ForeignKey(
        WalletRechargeRequest,
        on_delete=CASCADE,
        related_name="audit_logs",
        verbose_name=_("Recharge Request"),
    )
    from_status = CharField(_("From Status"), max_length=20, blank=True)
    to_status = CharField(_("To Status"), max_length=20)
    action = CharField(_("Action"), max_length=40, help_text=_("created / submitted / approved / rejected / cancelled"))
    actor = ForeignKey(
        User,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="wallet_recharge_audit_actions",
        verbose_name=_("Actor"),
    )
    actor_email = CharField(_("Actor Email"), max_length=255, blank=True)
    message = TextField(_("Message"), blank=True)
    metadata = JSONField(_("Metadata"), default=dict, blank=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet Recharge Request Audit Log")
        verbose_name_plural = _("Wallet Recharge Request Audit Logs")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.request_id}: {self.action} → {self.to_status}"

class WalletRechargeImportRecord(Model):
    """
    Tracks wallet recharge rows imported from IIC accounts text file (e.g. IIC Wallet-27-02-2026.txt).
    Prevents double-credit: duplicate key is (date, receipt_no, emp_no). Same entry is not credited again.
    """
    receipt_no = CharField(_("Receipt No."), max_length=50, db_index=True)
    financial_year_start = DateField(
        _("Financial Year Start"),
        help_text=_("Start of financial year (April 1) for this receipt"),
    )
    user = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="recharge_import_records",
        verbose_name=_("User"),
    )
    department = ForeignKey(
        Department,
        on_delete=PROTECT,
        related_name="recharge_import_records",
        verbose_name=_("Department (sub-wallet credited)"),
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    amount = DecimalField(_("Amount"), max_digits=10, decimal_places=2)
    dated = DateField(_("Dated"), null=True, blank=True)
    received_from_raw = TextField(_("Received From (raw)"), blank=True)
    remarks = TextField(_("Remarks"), blank=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet Recharge Import Record")
        verbose_name_plural = _("Wallet Recharge Import Records")
        ordering = ["-created_at"]
        constraints = [
            UniqueConstraint(
                fields=["receipt_no", "dated", "user"],
                name="unique_receipt_dated_user",
                condition=Q(dated__isnull=False),
            ),
            UniqueConstraint(
                fields=["receipt_no", "user"],
                name="unique_receipt_user_no_date",
                condition=Q(dated__isnull=True),
            ),
        ]

    def __str__(self) -> str:
        return f"Receipt {self.receipt_no} FY{self.financial_year_start} → {self.user.email} ₹{self.amount}"


class ExternalUserBankDetails(Model):
    """Bank details for external users for wallet withdrawals/transfers."""

    user = OneToOneField(
        User,
        on_delete=CASCADE,
        related_name="bank_details",
        verbose_name=_("User"),
    )
    account_holder_name = CharField(_("Account Holder Name"), max_length=255)
    bank_name = CharField(_("Bank Name"), max_length=255)
    account_number = CharField(_("Account Number"), max_length=64)
    ifsc_code = CharField(_("IFSC Code"), max_length=20)
    branch_name = CharField(_("Branch Name"), max_length=255, blank=True)
    account_type = CharField(_("Account Type"), max_length=50, blank=True)
    upi_id = CharField(_("UPI ID"), max_length=255, blank=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("External User Bank Details")
        verbose_name_plural = _("External User Bank Details")

    def __str__(self) -> str:
        return f"Bank details for {self.user.email}"

    def masked_account_number(self) -> str:
        s = (self.account_number or "").strip()
        if len(s) <= 4:
            return s
        return ("*" * (len(s) - 4)) + s[-4:]


class WalletWithdrawalRequestStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")
    CANCELLED = "CANCELLED", _("Cancelled")
    COMPLETED = "COMPLETED", _("Completed")


class WalletWithdrawalRequest(Model):
    """Withdrawal request for external users to transfer wallet balance to bank account.

    Funds are debited from sub-wallets when the request is created (held in system).
    On REJECTED/CANCELLED, the debited allocations are credited back.
    """

    user = ForeignKey(
        User,
        on_delete=CASCADE,
        related_name="wallet_withdrawal_requests",
        verbose_name=_("User"),
    )
    wallet = ForeignKey(
        Wallet,
        on_delete=CASCADE,
        related_name="withdrawal_requests",
        verbose_name=_("Wallet"),
    )
    amount = DecimalField(_("Amount"), max_digits=10, decimal_places=2)
    status = CharField(
        _("Status"),
        max_length=20,
        choices=WalletWithdrawalRequestStatus.choices,
        default=WalletWithdrawalRequestStatus.PENDING,
    )
    # Snapshot of bank details at request time (avoid later edits changing history)
    bank_snapshot = JSONField(default=dict, blank=True)
    # How funds were debited from sub-wallets: [{sub_wallet_id, department_id, amount}]
    allocations = JSONField(default=list, blank=True)
    user_note = TextField(_("User Note"), blank=True)
    approved_by_email = CharField(_("Approved By Email"), max_length=255, blank=True)
    response_message = TextField(_("Response Message"), blank=True)
    utr_reference = CharField(_("UTR / Transfer Reference"), max_length=255, blank=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    responded_at = DateTimeField(_("Responded at"), null=True, blank=True)
    completed_at = DateTimeField(_("Completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Wallet Withdrawal Request")
        verbose_name_plural = _("Wallet Withdrawal Requests")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"Withdrawal #{self.id} {self.user.email} ₹{self.amount} ({self.status})"


class WalletPeerTransferStatus(models.TextChoices):
    PENDING_OTP = "PENDING_OTP", _("Pending OTP")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Expired")


class WalletPeerTransfer(Model):
    """Faculty wallet-to-wallet transfer under a single department grant (OTP-gated)."""

    transaction_id = CharField(
        _("Transaction ID"),
        max_length=40,
        unique=True,
        db_index=True,
        help_text=_("Public unique transfer reference (e.g. W2W-…)"),
    )
    sender = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="wallet_peer_transfers_sent",
        verbose_name=_("Sender"),
    )
    recipient = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="wallet_peer_transfers_received",
        verbose_name=_("Recipient"),
    )
    initiated_by = ForeignKey(
        User,
        on_delete=PROTECT,
        related_name="wallet_peer_transfers_initiated",
        verbose_name=_("Initiated By"),
    )
    department = ForeignKey(
        Department,
        on_delete=PROTECT,
        related_name="wallet_peer_transfers",
        verbose_name=_("Department (Grant)"),
        help_text=_("Department sub-wallet grant used for debit and credit"),
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    grant_code = CharField(_("Grant Code"), max_length=100, blank=True)
    amount = DecimalField(_("Transfer Amount"), max_digits=10, decimal_places=2)
    remarks = TextField(_("Remarks"), blank=True)
    status = CharField(
        _("Transaction Status"),
        max_length=20,
        choices=WalletPeerTransferStatus.choices,
        default=WalletPeerTransferStatus.PENDING_OTP,
        db_index=True,
    )
    otp_code = CharField(_("OTP Code"), max_length=6, blank=True)
    otp_expires_at = DateTimeField(_("OTP Expires At"), null=True, blank=True)
    otp_verified = BooleanField(_("OTP Verification Status"), default=False)
    otp_verified_at = DateTimeField(_("OTP Verified At"), null=True, blank=True)
    sender_balance_after = DecimalField(
        _("Sender Sub-wallet Balance After"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    recipient_balance_after = DecimalField(
        _("Recipient Sub-wallet Balance After"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
    )
    failure_reason = TextField(_("Failure Reason"), blank=True)
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)
    completed_at = DateTimeField(_("Completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("Wallet Peer Transfer")
        verbose_name_plural = _("Wallet Peer Transfers")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["sender", "status"]),
            models.Index(fields=["recipient", "status"]),
            models.Index(fields=["status", "created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.transaction_id} ₹{self.amount} ({self.status})"

    @staticmethod
    def generate_transaction_id() -> str:
        stamp = timezone.now().strftime("%Y%m%d")
        suffix = secrets.token_hex(3).upper()
        return f"W2W-{stamp}-{suffix}"

    def save(self, *args, **kwargs) -> None:
        if not self.transaction_id:
            for _ in range(5):
                candidate = self.generate_transaction_id()
                if not WalletPeerTransfer.objects.filter(transaction_id=candidate).exists():
                    self.transaction_id = candidate
                    break
            if not self.transaction_id:
                self.transaction_id = self.generate_transaction_id()
        super().save(*args, **kwargs)

    def generate_otp(self) -> str:
        otp = "".join([str(secrets.randbelow(10)) for _ in range(6)])
        self.otp_code = otp
        self.otp_expires_at = timezone.now() + timedelta(minutes=10)
        self.otp_verified = False
        self.otp_verified_at = None
        self.save(
            update_fields=[
                "otp_code",
                "otp_expires_at",
                "otp_verified",
                "otp_verified_at",
                "updated_at",
            ]
        )
        return otp

    def verify_otp(self, otp: str) -> bool:
        if not self.otp_code or not self.otp_expires_at:
            return False
        if timezone.now() > self.otp_expires_at:
            return False
        if self.otp_verified:
            return False
        return self.otp_code == (otp or "").strip()

