"""Serializers for Wallet and SubWallet models."""

from decimal import Decimal

from rest_framework import serializers

from ..models import (
    Wallet,
    SubWallet,
    SubWalletTransaction,
    WalletJoinRequest,
    WalletRechargeRequest,
    WalletRechargeRequestAuditLog,
    ExternalUserBankDetails,
    WalletWithdrawalRequest,
    UserType,
    DepartmentType,
)


class SubWalletTransactionSerializer(serializers.ModelSerializer[SubWalletTransaction]):
    """Serializer for sub-wallet transactions. balance_after is injected from view context."""

    balance_after = serializers.SerializerMethodField()
    equipment_name = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    department_code = serializers.SerializerMethodField()
    related_user_name = serializers.SerializerMethodField()
    related_user_email = serializers.SerializerMethodField()
    virtual_booking_id = serializers.SerializerMethodField()
    description_display = serializers.SerializerMethodField()

    class Meta:
        model = SubWalletTransaction
        fields = [
            "id",
            "transaction_type",
            "amount",
            "description",
            "created_at",
            "balance_after",
            "equipment_name",
            "department_name",
            "department_code",
            "related_user_name",
            "related_user_email",
            "virtual_booking_id",
            "description_display",
        ]
        read_only_fields = ["id", "created_at"]

    def get_balance_after(self, obj: SubWalletTransaction):
        """From view context: balance_after_map keyed by transaction id."""
        mapping = (self.context or {}).get("balance_after_map") or {}
        val = mapping.get(obj.id)
        if val is None:
            return None
        return str(val)

    def get_related_user_name(self, obj: SubWalletTransaction) -> str | None:
        u = getattr(obj, "related_user", None)
        if not u:
            return None
        return (u.name or u.email or "").strip() or None

    def get_related_user_email(self, obj: SubWalletTransaction) -> str | None:
        u = getattr(obj, "related_user", None)
        if not u or not getattr(u, "email", None):
            return None
        return (u.email or "").strip() or None

    def get_virtual_booking_id(self, obj: SubWalletTransaction) -> str | None:
        import re
        desc = (obj.description or "").strip()
        if not desc:
            return None
        m = re.search(r"\|\s*Ref:\s*(\S+)", desc, re.I)
        if m:
            return m.group(1).strip()
        m = re.search(r"\bRef:\s*(\S+)", desc, re.I)
        if m:
            return m.group(1).strip()
        return None

    def get_description_display(self, obj: SubWalletTransaction) -> str:
        """Description for UI: virtual ref first; strip duplicate Student line when related_user is set."""
        import re
        desc = (obj.description or "").strip()
        if not desc:
            return ""
        vid = self.get_virtual_booking_id(obj)
        if getattr(obj, "related_user_id", None):
            desc = re.sub(r"\s*-\s*Student:\s*.+$", "", desc, flags=re.I).strip()
        if vid:
            core = re.sub(r"\s*\|\s*Ref:\s*" + re.escape(vid) + r"\s*$", "", desc, flags=re.I).strip()
            return f"Ref: {vid} — {core}" if core else f"Ref: {vid}"
        return desc

    def get_department_name(self, obj: SubWalletTransaction):
        """From sub_wallet.department (requires select_related)."""
        if obj.sub_wallet and obj.sub_wallet.department:
            return obj.sub_wallet.department.name
        return None

    def get_department_code(self, obj: SubWalletTransaction):
        """From sub_wallet.department (requires select_related)."""
        if obj.sub_wallet and obj.sub_wallet.department:
            return getattr(obj.sub_wallet.department, "code", None)
        return None

    def get_equipment_name(self, obj: SubWalletTransaction) -> str | None:
        """Extract equipment from description; return full equipment name (resolve code to name when possible)."""
        import re
        from iic_booking.equipment.models import Equipment

        desc = (obj.description or "").strip()
        if not desc:
            return None

        def resolve_to_name(parsed: str) -> str:
            """If parsed looks like an equipment code, return equipment name; else return parsed."""
            if not parsed:
                return parsed
            try:
                eq = Equipment.objects.filter(code=parsed).first()
                if eq and getattr(eq, "name", None):
                    return eq.name
            except Exception:
                pass
            return parsed

        # Debit for new booking: "Booking #CODE - Full Name (may include parentheses) (N minutes)"
        m = re.search(
            r"Booking #([A-Za-z0-9_]+)\s*[-–]\s*(.+?)\s*\(\s*(\d+)\s*minutes\s*\)",
            desc,
            re.I,
        )
        if m:
            name_part = m.group(2).strip()
            if name_part:
                return name_part
            return resolve_to_name(m.group(1).strip())

        # Urgent approval debit: "Urgent approval: Booking #CODE - Name (Hold converted) ..."
        m = re.search(
            r"Urgent approval:\s*Booking #([A-Za-z0-9_]+)\s*[-–]\s*(.+?)\s*\(\s*Hold converted\s*\)",
            desc,
            re.I,
        )
        if m:
            name_part = m.group(2).strip()
            if name_part:
                return name_part
            return resolve_to_name(m.group(1).strip())

        # "Refund for {name} and Booking #..." — full name already in description
        m = re.search(r"Refund for (.+?) and Booking", desc, re.I)
        if m:
            return m.group(1).strip()
        # Refund variants that end with "Booking #N - CODE" (code only)
        m = re.search(r"Refund(?: for cancelled)?(?: \(Operator Unavailable\))? for Booking #\d+\s*[-–]\s*([A-Za-z0-9_]+)", desc, re.I)
        if m:
            return resolve_to_name(m.group(1).strip())

        # "Additional charge for {name}, Booking..."
        m = re.search(r"Additional charge for ([^,]+),", desc, re.I)
        if m:
            return resolve_to_name(m.group(1).strip())
        # "Charge recalculation for Booking #N - CODE" or older "... - {code}"
        m = re.search(r"Booking #?\d+\s*[-–]\s*([A-Za-z0-9_]+)", desc)
        if m:
            return resolve_to_name(m.group(1).strip())
        # "Debit/Credit for ... Equipment: CODE" style
        m = re.search(r"[Ee]quipment[:\s]+([A-Za-z0-9_]+)", desc)
        if m:
            return resolve_to_name(m.group(1).strip())
        return None


class SubWalletSerializer(serializers.ModelSerializer[SubWallet]):
    """Serializer for sub-wallet (department-wise)."""

    department_id = serializers.IntegerField(source="department.id", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True, allow_null=True)

    class Meta:
        model = SubWallet
        fields = [
            "id",
            "department_id",
            "department_name",
            "department_code",
            "balance",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "balance", "created_at", "updated_at"]


class AdminSubWalletListSerializer(serializers.ModelSerializer[SubWallet]):
    """Admin list serializer for SubWallet (mirrors Django admin/users/subwallet/ list)."""

    wallet_id = serializers.IntegerField(source="wallet.id", read_only=True)
    wallet_user_email = serializers.SerializerMethodField()
    department_id = serializers.IntegerField(source="department.id", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    department_code = serializers.CharField(source="department.code", read_only=True, allow_null=True)

    class Meta:
        model = SubWallet
        fields = [
            "id",
            "wallet_id",
            "wallet_user_email",
            "department_id",
            "department_name",
            "department_code",
            "balance",
            "created_at",
            "updated_at",
        ]
        read_only_fields = fields

    def get_wallet_user_email(self, obj: SubWallet):
        if obj.wallet and obj.wallet.user:
            return obj.wallet.user.email
        return "-"


class AdminSubWalletCreateSerializer(serializers.ModelSerializer[SubWallet]):
    """Serializer for admin create sub-wallet (mirrors Django admin/users/subwallet/add/)."""

    class Meta:
        model = SubWallet
        fields = ["wallet", "department"]

    def validate_department(self, value):
        if value and value.department_type != DepartmentType.INTERNAL:
            raise serializers.ValidationError(
                "Only internal departments can have sub-wallets. Select an internal department."
            )
        return value

    def create(self, validated_data):
        wallet = validated_data["wallet"]
        department = validated_data["department"]
        sub_wallet, created = SubWallet.objects.get_or_create(
            wallet=wallet,
            department=department,
            defaults={"balance": Decimal("0.00")},
        )
        if not created:
            raise serializers.ValidationError(
                {"non_field_errors": ["A sub-wallet for this wallet and department already exists."]}
            )
        return sub_wallet


class WalletSerializer(serializers.ModelSerializer[Wallet]):
    """Serializer for wallet (balance is total_balance from sub-wallets)."""

    balance = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = ["balance"]
        read_only_fields = ["balance"]

    def get_balance(self, obj):
        return obj.total_balance


class AdminWalletSerializer(serializers.ModelSerializer[Wallet]):
    """Admin list/change serializer for Wallet (mirrors Django admin/users/wallet/)."""

    user_email = serializers.SerializerMethodField()
    user_name = serializers.SerializerMethodField()
    user_type_display = serializers.SerializerMethodField()
    total_balance_display = serializers.SerializerMethodField()
    sub_wallet_count = serializers.SerializerMethodField()

    class Meta:
        model = Wallet
        fields = [
            "id",
            "user",
            "user_email",
            "user_name",
            "user_type_display",
            "total_balance_display",
            "sub_wallet_count",
        ]
        read_only_fields = ["id", "user_email", "user_name", "user_type_display", "total_balance_display", "sub_wallet_count"]

    def get_user_email(self, obj: Wallet):
        return obj.user.email if obj.user else "-"

    def get_user_name(self, obj: Wallet):
        return obj.user.name if obj.user and obj.user.name else "-"

    def get_user_type_display(self, obj: Wallet):
        if obj.user:
            return obj.user.get_user_type_display_label() or "-"
        return "-"

    def get_total_balance_display(self, obj: Wallet):
        if obj:
            return str(obj.total_balance)
        return "0.00"

    def get_sub_wallet_count(self, obj: Wallet):
        return obj.sub_wallets.count() if obj else 0


class WalletBalanceSerializer(serializers.Serializer):
    """Serializer for wallet balance response."""

    balance = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)


class WalletCreditSerializer(serializers.Serializer):
    """Serializer for wallet credit operation."""

    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        help_text="Amount to add to wallet (must be positive)",
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="Optional description for the transaction",
    )


class WalletDebitSerializer(serializers.Serializer):
    """Serializer for wallet debit operation."""

    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        help_text="Amount to deduct from wallet (must be positive)",
    )
    description = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=500,
        help_text="Optional description for the transaction",
    )


class WalletJoinRequestSerializer(serializers.ModelSerializer):
    """Serializer for wallet join requests."""
    
    student_name = serializers.SerializerMethodField()
    student_email = serializers.SerializerMethodField()
    student_phone = serializers.SerializerMethodField()
    student_profile_picture = serializers.SerializerMethodField()
    student_branch_name = serializers.SerializerMethodField()
    student_degree_name = serializers.SerializerMethodField()
    student_department_name = serializers.SerializerMethodField()
    faculty_name = serializers.SerializerMethodField()
    faculty_email = serializers.SerializerMethodField()
    faculty_phone = serializers.SerializerMethodField()
    faculty_profile_picture = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = WalletJoinRequest
        fields = [
            'id',
            'student',
            'student_name',
            'student_email',
            'student_phone',
            'student_profile_picture',
            'student_branch_name',
            'student_degree_name',
            'student_department_name',
            'faculty',
            'faculty_name',
            'faculty_email',
            'faculty_phone',
            'faculty_profile_picture',
            'wallet',
            'status',
            'status_display',
            'message',
            'faculty_response',
            'created_at',
            'updated_at',
            'responded_at',
        ]
        read_only_fields = [
            'id',
            'student',
            'faculty',
            'wallet',
            'status',
            'faculty_response',
            'created_at',
            'updated_at',
            'responded_at',
        ]
    
    def get_student_name(self, obj):
        """Return student's name or email."""
        if obj.student:
            return obj.student.name or obj.student.email
        return None
    
    def get_student_email(self, obj):
        """Return student's email."""
        return obj.student.email if obj.student else None
    
    def get_student_phone(self, obj):
        """Return student's phone number."""
        return obj.student.phone_number if obj.student else None
    
    def get_student_profile_picture(self, obj):
        """Return student's stable profile-picture proxy URL (does not expire)."""
        if obj.student:
            return obj.student.get_profile_picture_url_or_none(request=self.context.get("request"))
        return None
    
    def get_student_branch_name(self, obj):
        """Return student's branch name (program details for TA nomination)."""
        return getattr(obj.student, 'branch_name', None) or ''
    
    def get_student_degree_name(self, obj):
        """Return student's degree name (program details for TA nomination)."""
        return getattr(obj.student, 'degree_name', None) or ''
    
    def get_student_department_name(self, obj):
        """Return student's department name (program details for TA nomination)."""
        if obj.student and getattr(obj.student, 'department', None):
            return obj.student.department.name or ''
        return ''
    
    def get_faculty_name(self, obj):
        """Return faculty's name or email."""
        if obj.faculty:
            return obj.faculty.name or obj.faculty.email
        return None
    
    def get_faculty_email(self, obj):
        """Return faculty's email."""
        return obj.faculty.email if obj.faculty else None
    
    def get_faculty_phone(self, obj):
        """Return faculty's phone number."""
        return obj.faculty.phone_number if obj.faculty else None
    
    def get_faculty_profile_picture(self, obj):
        """Return faculty's stable profile-picture proxy URL (does not expire)."""
        if obj.faculty:
            return obj.faculty.get_profile_picture_url_or_none(request=self.context.get("request"))
        return None


class WalletJoinRequestCreateSerializer(serializers.Serializer):
    """Serializer for creating wallet join requests."""
    
    faculty_email = serializers.EmailField(
        help_text="Email address of the faculty member whose wallet you want to join"
    )
    message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Optional message to the faculty member",
    )


class WalletJoinRequestResponseSerializer(serializers.Serializer):
    """Serializer for faculty responding to wallet join requests."""
    
    response_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Optional response message to the student",
    )


class WalletRechargeRequestAuditLogSerializer(serializers.ModelSerializer):
    actor_name = serializers.SerializerMethodField()

    class Meta:
        model = WalletRechargeRequestAuditLog
        fields = [
            "id",
            "from_status",
            "to_status",
            "action",
            "actor",
            "actor_name",
            "actor_email",
            "message",
            "metadata",
            "created_at",
        ]
        read_only_fields = fields

    def get_actor_name(self, obj):
        if obj.actor_id:
            return obj.actor.name or obj.actor.email
        return obj.actor_email or ""


class WalletRechargeRequestSerializer(serializers.ModelSerializer):
    """Serializer for wallet recharge requests (to a department sub-wallet)."""
    
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    user_emp_id = serializers.SerializerMethodField()
    request_id = serializers.CharField(source="request_id_display", read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    department_id = serializers.SerializerMethodField()
    department_name = serializers.SerializerMethodField()
    project_id = serializers.SerializerMethodField()
    project_name = serializers.SerializerMethodField()
    project_code = serializers.SerializerMethodField()
    project_agency = serializers.SerializerMethodField()
    project_head_name = serializers.SerializerMethodField()
    project_head_email = serializers.SerializerMethodField()
    account_incharge_email = serializers.SerializerMethodField()
    account_incharge_name = serializers.SerializerMethodField()
    fund_receipt_verified_by_name = serializers.SerializerMethodField()
    audit_logs = WalletRechargeRequestAuditLogSerializer(many=True, read_only=True)
    
    class Meta:
        model = WalletRechargeRequest
        fields = [
            'id',
            'request_id',
            'user',
            'user_name',
            'user_email',
            'user_emp_id',
            'wallet',
            'department',
            'department_id',
            'department_name',
            'amount',
            'project',
            'project_id',
            'project_name',
            'project_code',
            'project_agency',
            'project_head_name',
            'project_head_email',
            'project_details',
            'employee_number',
            'user_department_name',
            'department_grant_code',
            'project_grant_code',
            'recharge_mode',
            'undertaking_accepted',
            'fund_receipt_verified',
            'fund_receipt_verified_by',
            'fund_receipt_verified_by_name',
            'fund_receipt_verified_at',
            'fund_receipt_verification_remarks',
            'account_incharge',
            'account_incharge_email',
            'account_incharge_name',
            'status',
            'status_display',
            'user_otp_verified',
            'sric_notification_sent',
            'credit_facility_opted_in',
            'credit_limit_amount',
            'credit_window_ends_at',
            'credit_facility_status',
            'approved_by_email',
            'processed_by',
            'response_message',
            'rejection_reason_code',
            'rejection_reason_text',
            'cancellation_source',
            'created_at',
            'updated_at',
            'responded_at',
            'audit_logs',
        ]
        read_only_fields = [
            'id',
            'request_id',
            'user',
            'wallet',
            'status',
            'employee_number',
            'user_department_name',
            'department_grant_code',
            'project_grant_code',
            'recharge_mode',
            'undertaking_accepted',
            'fund_receipt_verified',
            'fund_receipt_verified_by',
            'fund_receipt_verified_by_name',
            'fund_receipt_verified_at',
            'fund_receipt_verification_remarks',
            'account_incharge',
            'user_otp_verified',
            'sric_notification_sent',
            'credit_facility_opted_in',
            'credit_limit_amount',
            'credit_window_ends_at',
            'credit_facility_status',
            'approved_by_email',
            'processed_by',
            'response_message',
            'rejection_reason_code',
            'rejection_reason_text',
            'cancellation_source',
            'created_at',
            'updated_at',
            'responded_at',
            'audit_logs',
        ]
    
    def get_user_name(self, obj):
        """Return user's name or email."""
        if obj.user:
            return obj.user.name or obj.user.email
        return None
    
    def get_user_email(self, obj):
        """Return user's email."""
        return obj.user.email if obj.user else None

    def get_user_emp_id(self, obj):
        if obj.employee_number:
            return obj.employee_number
        return (obj.user.emp_id or "").strip() if obj.user_id else ""

    def get_department_id(self, obj):
        return obj.department_id

    def get_department_name(self, obj):
        try:
            return obj.department.name if obj.department_id else None
        except Exception:
            return None

    def get_project_id(self, obj):
        return obj.project_id

    def get_project_name(self, obj):
        try:
            return obj.project.name if obj.project_id else None
        except Exception:
            return None

    def get_project_code(self, obj):
        try:
            return obj.project.project_code if obj.project_id else None
        except Exception:
            return None

    def get_account_incharge_email(self, obj):
        try:
            if obj.account_incharge_id:
                return obj.account_incharge.email or ""
        except Exception:
            return ""
        return ""

    def get_account_incharge_name(self, obj):
        try:
            if obj.account_incharge_id:
                return obj.account_incharge.name or obj.account_incharge.email or ""
        except Exception:
            return ""
        return ""

    def get_fund_receipt_verified_by_name(self, obj):
        try:
            u = getattr(obj, "fund_receipt_verified_by", None)
            if not u:
                return ""
            return (u.name or "").strip() or (u.email or "")
        except Exception:
            return ""

    def get_project_agency(self, obj):
        try:
            p = obj.project
            return (p.agency or "").strip() if p else ""
        except Exception:
            return ""

    def get_project_head_name(self, obj):
        try:
            p = obj.project
            fac = getattr(p, "faculty", None) if p else None
            if not fac:
                return ""
            return (fac.name or "").strip() or (fac.email or "")
        except Exception:
            return ""

    def get_project_head_email(self, obj):
        try:
            p = obj.project
            fac = getattr(p, "faculty", None) if p else None
            return fac.email if fac else ""
        except Exception:
            return ""


class WalletRechargeRequestCreateSerializer(serializers.Serializer):
    """Serializer for creating wallet recharge requests (to a department sub-wallet)."""
    
    amount = serializers.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("100"),
        help_text="Amount to recharge (minimum ₹100 for offline recharge request)",
    )
    department_id = serializers.IntegerField(
        help_text="Internal department ID whose sub-wallet will be credited (required)",
    )
    project_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Project ID (required for faculty Project Grant mode)",
    )
    recharge_mode = serializers.ChoiceField(
        choices=[
            ("project_grant", "Recharge via Project Grant"),
            ("direct_cash_deposit", "Direct Cash Deposit / Bank Transfer"),
        ],
        required=False,
        default="project_grant",
        help_text="Offline recharge mode",
    )
    undertaking_accepted = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Required for Direct Cash Deposit / Bank Transfer mode",
    )
    credit_facility_opted_in = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Faculty only: request temporary credit line when department balance is below admin threshold.",
    )
    
    def validate_project_id(self, value):
        """Validate that project exists, is active, and belongs to the user."""
        if value is None:
            return value
        
        from ..models import Project
        
        try:
            project = Project.objects.get(id=value)
        except Project.DoesNotExist:
            raise serializers.ValidationError("Project not found.")
        
        # Check if project belongs to the user (will be checked in view)
        # Check if project is active
        if not project.is_active:
            raise serializers.ValidationError("Only active projects can be selected.")
        
        return value


class WalletRechargeRequestVerifyUserOtpSerializer(serializers.Serializer):
    """Serializer for verifying user OTP and creating recharge request."""
    
    request_id = serializers.IntegerField(
        help_text="ID of the temporary request from send_user_otp_for_recharge",
    )
    user_otp = serializers.CharField(
        max_length=6,
        min_length=6,
        help_text="6-digit OTP code from user's email",
    )


class WalletRechargeRequestApproveSerializer(serializers.Serializer):
    """Serializer for approving/rejecting wallet recharge requests via OTP."""
    
    otp = serializers.CharField(
        max_length=6,
        min_length=6,
        help_text="6-digit OTP code from email",
    )
    approved_by_email = serializers.EmailField(
        help_text="Email address of the person approving/rejecting",
    )
    response_message = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=1000,
        help_text="Optional response message",
    )


class ExternalUserBankDetailsSerializer(serializers.ModelSerializer[ExternalUserBankDetails]):
    masked_account_number = serializers.SerializerMethodField()

    class Meta:
        model = ExternalUserBankDetails
        fields = [
            "account_holder_name",
            "bank_name",
            "account_number",
            "masked_account_number",
            "ifsc_code",
            "branch_name",
            "account_type",
            "upi_id",
            "updated_at",
        ]
        read_only_fields = ["masked_account_number", "updated_at"]

    def get_masked_account_number(self, obj: ExternalUserBankDetails):
        return obj.masked_account_number()


class ExternalUserBankDetailsUpsertSerializer(serializers.Serializer):
    account_holder_name = serializers.CharField(max_length=255)
    bank_name = serializers.CharField(max_length=255)
    account_number = serializers.CharField(max_length=64)
    ifsc_code = serializers.CharField(max_length=20)
    branch_name = serializers.CharField(required=False, allow_blank=True, max_length=255)
    account_type = serializers.CharField(required=False, allow_blank=True, max_length=50)
    upi_id = serializers.CharField(required=False, allow_blank=True, max_length=255)


class WalletWithdrawalRequestSerializer(serializers.ModelSerializer[WalletWithdrawalRequest]):
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    user_email = serializers.SerializerMethodField()

    class Meta:
        model = WalletWithdrawalRequest
        fields = [
            "id",
            "user",
            "user_email",
            "wallet",
            "amount",
            "status",
            "status_display",
            "bank_snapshot",
            "allocations",
            "user_note",
            "approved_by_email",
            "response_message",
            "utr_reference",
            "created_at",
            "responded_at",
            "completed_at",
        ]
        read_only_fields = fields

    def get_user_email(self, obj: WalletWithdrawalRequest):
        return obj.user.email if obj.user else None


class WalletWithdrawalRequestCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    user_note = serializers.CharField(required=False, allow_blank=True, max_length=2000)

