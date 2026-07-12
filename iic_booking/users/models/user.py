"""User model."""

from typing import ClassVar

from django.contrib.auth.models import AbstractUser
from django.core.files.storage import default_storage
from django.db.models import BooleanField
from django.db.models import CharField
from django.db.models import DateField
from django.db.models import DecimalField
from django.db.models import EmailField
from django.db.models import ForeignKey
from django.db.models import ImageField
from django.db.models import PROTECT
from django.db.models import SET_NULL
from django.db.models import DateTimeField
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .department import Department, DepartmentType
from .organization_request import OrganizationRequest
from .user_type import UserType
from ..managers import UserManager


class Gender:
    """Gender options for user profile."""

    MALE = "male"
    FEMALE = "female"
    OTHER = "other"

    @classmethod
    def get_choices(cls):
        return [
            (cls.MALE, _("Male")),
            (cls.FEMALE, _("Female")),
            (cls.OTHER, _("Other")),
        ]


class User(AbstractUser):
    """
    Default custom user model for IIC Booking.
    If adding fields that need to be filled at user signup,
    check forms.SignupForm and forms.SocialSignupForms accordingly.
    """

    # First and last name do not cover name patterns around the globe
    name = CharField(_("Name of User"), blank=True, max_length=255)
    first_name = None  # type: ignore[assignment]
    last_name = None  # type: ignore[assignment]
    email = EmailField(_("email address"), unique=True)
    username = None  # type: ignore[assignment]
    gender = CharField(
        _("Gender"),
        max_length=30,
        choices=Gender.get_choices(),
        blank=True,
        null=True,
        help_text=_("Gender (optional)"),
    )
    user_type = CharField(
        _("User Type"),
        max_length=50,
        choices=UserType.get_choices(),
        blank=True,
        null=True,
        help_text=_("Type of user in the system"),
    )
    user_type_alias = CharField(
        _("User Type Alias"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Optional display label for IITR Student only (e.g. Guest Student). Shown in UI; internally user remains IITR Student."),
    )
    emp_id = CharField(
        _("Employee ID"),
        max_length=50,
        blank=True,
        null=True,
        unique=True,
        help_text=_("Employee/Student ID"),
    )
    phone_number = CharField(
        _("Phone Number"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Contact phone number"),
    )
    secondary_phone_number = CharField(
        _("Secondary Phone Number"),
        max_length=20,
        blank=True,
        null=True,
        help_text=_("Secondary contact phone number"),
    )
    internal_id = CharField(
        _("Internal ID"),
        max_length=100,
        blank=True,
        null=True,
        help_text=_("Internal user ID from external system (e.g., Omniport)"),
    )
    date_of_birth = DateField(
        _("Date of Birth"),
        blank=True,
        null=True,
        help_text=_("User's date of birth"),
    )
    branch_name = CharField(
        _("Branch Name"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Branch name for students"),
    )
    degree_name = CharField(
        _("Degree Name"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Degree name for students"),
    )
    designation = CharField(
        _("Designation"),
        max_length=255,
        blank=True,
        null=True,
        help_text=_("Designation for faculty members"),
    )
    profile_picture = ImageField(
        _("Profile Picture"),
        upload_to="profile_pictures/",
        blank=True,
        null=True,
        help_text=_("User profile picture"),
    )
    department = ForeignKey(
        Department,
        on_delete=SET_NULL,
        blank=True,
        null=True,
        related_name="users",
        verbose_name=_("Department"),
        help_text=_("Department the user belongs to"),
    )
    organization_request = ForeignKey(
        OrganizationRequest,
        on_delete=SET_NULL,
        blank=True,
        null=True,
        related_name="users",
        verbose_name=_("Pending organization request"),
        help_text=_("For Govt R&D: set when user signed up with a requested organization pending admin approval."),
    )
    supervisor = ForeignKey(
        "self",
        on_delete=SET_NULL,
        blank=True,
        null=True,
        related_name="supervised_users",
        verbose_name=_("Supervisor (IITR Faculty)"),
        help_text=_("IITR Faculty supervisor for Post Doctoral Fellows and Research Associates in Projects"),
        limit_choices_to={
            "user_type": UserType.FACULTY,
            "department__department_type": DepartmentType.INTERNAL,
        },
    )
    email_verified = BooleanField(
        _("Email Verified"),
        default=False,
        help_text=_("Whether the user's email address has been verified"),
    )
    admin_approved = BooleanField(
        _("Admin Approved"),
        default=False,
        help_text=_("Whether the user has been approved by an administrator"),
    )
    force_inactive = BooleanField(
        _("Force Inactive"),
        default=False,
        help_text=_("When True, the account is deactivated even if verification/approvals are complete."),
    )
    verification_email_sent_at = DateTimeField(
        _("Verification Email Sent At"),
        blank=True,
        null=True,
        help_text=_("Timestamp of the last registration verification email sent. Used to enforce verification link expiry."),
    )
    supervisor_approved = BooleanField(
        _("Supervisor Approved"),
        default=False,
        help_text=_("For IITR Post Doctoral Fellows and Research Associates: supervisor must approve before admin approval."),
    )
    program_start_date = DateField(
        _("Program/Course Start Date"),
        blank=True,
        null=True,
        help_text=_("Start date of program, course, or position. Used with end date to enforce access duration and revalidation if > 1 year."),
    )
    program_end_date = DateField(
        _("Current Program/Employment Validity"),
        blank=True,
        null=True,
        help_text=_("Current program or employment validity. Access is disabled after this date."),
    )
    access_on_hold = BooleanField(
        _("Access On Hold"),
        default=False,
        help_text=_("When True, access is blocked until user revalidates by submitting documentary evidence (e.g. when program duration > 1 year)."),
    )
    
    auto_slot_selection = BooleanField(
        _("Auto Slot Selection"),
        default=False,
        help_text=_("If enabled, the system will automatically select the required consecutive slots when booking equipment"),
    )

    wallet_low_balance_alert_enabled = BooleanField(
        _("Wallet low balance alert enabled"),
        default=False,
        help_text=_("When enabled, an email is sent daily at 11:00 AM if wallet balance falls below the threshold."),
    )
    wallet_low_balance_alert_threshold = DecimalField(
        _("Wallet low balance alert threshold (₹)"),
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Alert is sent when balance is below this amount. Required when low balance alert is enabled."),
    )

    use_discounted_charge_profile = BooleanField(
        _("Use discounted charge profile (waive charges)"),
        default=False,
        help_text=_(
            "When enabled, the user will use the 'Discounted Charge Profile' for equipment bookings "
            "to get ₹0 charges."
        ),
    )
    istem_portal_acknowledged = BooleanField(
        _("I-STEM portal registration confirmed"),
        default=False,
        help_text=_(
            "External users must confirm they have an account on the national I-STEM portal "
            "(https://www.istem.gov.in/) before booking equipment."
        ),
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects: ClassVar[UserManager] = UserManager()

    def get_absolute_url(self) -> str:
        """Get URL for user's detail view.

        Returns:
            str: URL for user detail.

        """
        return reverse("users:detail", kwargs={"pk": self.id})

    def get_profile_picture_url_or_none(self):
        """Return profile picture URL only if the file exists in storage (avoids S3 NoSuchKey in frontend)."""
        if not self.profile_picture or not self.profile_picture.name:
            return None
        try:
            if default_storage.exists(self.profile_picture.name):
                return self.profile_picture.url
        except (ValueError, AttributeError, OSError):
            pass
        return None

    def get_user_type_code(self) -> str:
        """Get user type code for backward compatibility.

        Returns:
            str: User type code
        """
        return self.user_type or UserType.EXTERNAL

    def get_user_type_display_label(self) -> str | None:
        """Get the label to show in UI. For IITR Student, returns user_type_alias if set, else 'IITR Student'.
        For Individual Student, returns user_type_alias if set, else 'Individual Student'."""
        if not self.user_type:
            return None
        if self.user_type == UserType.STUDENT and self.user_type_alias and str(self.user_type_alias).strip():
            return str(self.user_type_alias).strip()
        if self.user_type == UserType.INDIVIDUAL_STUDENT and self.user_type_alias and str(self.user_type_alias).strip():
            return str(self.user_type_alias).strip()
        return dict(UserType.get_choices()).get(self.user_type, self.user_type)

    def uses_admin_panel(self) -> bool:
        """Check if user accesses the system via admin panel.

        Returns:
            bool: True if user uses admin panel, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type in UserType.get_admin_panel_codes()

    def uses_omniport_auth(self) -> bool:
        """Check if user authenticates via Omniport.

        Returns:
            bool: True if user uses Omniport authentication, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type in UserType.get_omniport_codes()

    def uses_email_auth(self) -> bool:
        """Check if user authenticates via email.

        Returns:
            bool: True if user uses email authentication, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type in UserType.get_email_auth_codes()

    def is_admin(self) -> bool:
        """Check if user is an admin.

        Returns:
            bool: True if user is admin, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type == UserType.ADMIN

    def is_student(self) -> bool:
        """Check if user is a student.

        Returns:
            bool: True if user is student, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type == UserType.STUDENT

    def is_faculty(self) -> bool:
        """Check if user is faculty.

        Returns:
            bool: True if user is faculty, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type == UserType.FACULTY

    def is_individual_student(self) -> bool:
        """Check if user is an individual student.

        Returns:
            bool: True if user is an individual student, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type == UserType.INDIVIDUAL_STUDENT

    def is_external(self) -> bool:
        """Check if user is external.

        Returns:
            bool: True if user is external, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type == UserType.EXTERNAL

    def can_have_wallet(self) -> bool:
        """Check if user can have their own individual wallet.

        Note: Regular STUDENT uses faculty wallet (not their own), so they return False here.
        Individual Student, Faculty, and all external users can have their own wallet.
        
        Returns:
            bool: True if user can have their own wallet, False otherwise.
        """
        if not self.user_type:
            return False
        return self.user_type in UserType.get_wallet_eligible_codes()
    
    def get_accessible_wallet(self):
        """Get the wallet this user can access.
        
        For regular STUDENT and OTHER: Returns the faculty wallet they're joined to (if approved), 
            otherwise their own wallet if they can have one.
        For Individual Student, Faculty, and other external users: Returns their own individual wallet.
        
        Returns:
            Wallet or None: The wallet the user can access
        """
        from .wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
        
        # For STUDENT and OTHER users, check if they have joined a faculty wallet first
        if self.user_type in {UserType.STUDENT, UserType.OTHER}:
            approved_request = WalletJoinRequest.objects.filter(
                student=self,
                status=WalletJoinRequestStatus.APPROVED
            ).select_related('wallet').first()
            
            if approved_request and approved_request.wallet:
                return approved_request.wallet
        
        # If user can have their own wallet, return it
        if self.can_have_wallet():
            try:
                return self.wallet
            except Wallet.DoesNotExist:
                return None
        
        return None

    def is_ready_for_activation(self) -> bool:
        """Check if user is ready for admin approval.
        
        Returns:
            bool: True if email is verified but not yet approved by admin.
        """
        return self.email_verified and not self.admin_approved

    def needs_supervisor_approval(self) -> bool:
        """True if this user type requires supervisor approval before admin (Post Doc, Research Associates)."""
        if not self.user_type_alias:
            return False
        alias = (self.user_type_alias or "").strip()
        return alias in ("IITR Post Doctoral Fellows", "IITR Research Associates in Projects")

    def can_login(self) -> bool:
        """Check if user can login.
        
        Returns:
            bool: True if admin approved and not force-deactivated (active).
        """
        if not self.is_active:
            return False
        return self.admin_approved

    def update_active_status(self) -> bool:
        """Update is_active status based on email_verified, approvals, program end date, and access_on_hold.
        
        Business rule: once Admin Approved, user is deemed Active (unless force-deactivated).
        
        Returns:
            bool: True if status was changed, False otherwise.
        """
        should_be_active = bool(self.admin_approved) and not bool(self.force_inactive)
        if self.is_active != should_be_active:
            self.is_active = should_be_active
            return True
        return False

    def save(self, *args, **kwargs):
        """Override save to automatically update is_active status and create wallet if eligible."""
        # Track if this is a new user
        is_new = self.pk is None
        
        # Update active status before saving
        status_changed = self.update_active_status()
        
        # If status changed, ensure it's included in update_fields if specified
        if status_changed:
            if "update_fields" in kwargs and kwargs["update_fields"] is not None:
                # Add is_active to update_fields if not already present
                update_fields = list(kwargs["update_fields"])
                if "is_active" not in update_fields:
                    update_fields.append("is_active")
                kwargs["update_fields"] = update_fields
            # If update_fields is not specified, is_active will be saved automatically
        
        super().save(*args, **kwargs)
        
        # Create wallet automatically for eligible users if they don't have one
        if self.can_have_wallet():
            try:
                from .wallet import Wallet
                # Use get_or_create to avoid creating duplicate wallets
                Wallet.objects.get_or_create(user=self, defaults={"balance": 0.00})
            except Exception:
                # Silently fail if wallet creation fails (e.g., during migrations or if user_type is not set)
                pass


