from allauth.account.decorators import secure_admin_login
from django.conf import settings
from django.contrib import admin
from django.contrib.admin import SimpleListFilter
from django.contrib.auth import admin as auth_admin
from iic_booking.communication.service import CommunicationService
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
import logging

logger = logging.getLogger(__name__)

# Django admin .button CSS often floats / stacks poorly in changelist cells.
_ADMIN_ACTION_BTN = (
    "display:inline-block;float:none;white-space:nowrap;"
    "padding:4px 10px;text-decoration:none;border-radius:3px;margin:0;line-height:1.3;"
)
_ADMIN_ACTION_WRAP = (
    "display:inline-flex;flex-wrap:wrap;gap:6px;align-items:center;"
    "max-width:22rem;white-space:normal;"
)


def _admin_action_buttons(*html_buttons):
    """Join pre-built button HTML into a non-overlapping flex row/wrap."""
    parts = [b for b in html_buttons if b]
    if not parts:
        return format_html('<span style="color:#666;">-</span>')
    return format_html(
        '<span class="iic-admin-actions" style="{}">{}</span>',
        _ADMIN_ACTION_WRAP,
        mark_safe("".join(str(p) for p in parts)),
    )

from .forms import UserAdminChangeForm
from .forms import UserAdminCreationForm
from .models.wallet_sric_settings import WalletSricSettings
from .models.test_account_email_settings import TestAccountEmailSettings
from .models.wallet_credit_facility_settings import WalletCreditFacilitySettings
from .models.wallet_student_recharge_settings import WalletStudentRechargeSettings
from .models.department_faculty_credit_facility import (
    DepartmentFacultyCreditFacilitySettings,
    FacultyDepartmentCreditFacility,
    FacultyDepartmentCreditFacilityAuditLog,
)
from .models import (
    User,
    Department,
    DepartmentType,
    OrganizationRequest,
    Wallet,
    SubWallet,
    SubWalletTransaction,
    WalletRazorpayOrder,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
    ExternalUserBankDetails,
    WalletWithdrawalRequest,
    WalletWithdrawalRequestStatus,
    WalletPeerTransfer,
    UserType,
    UserDocument,
    UserGroup,
    UserGroupMember,
    Project,
    AuthSettings,
    UserTypeInactivityTimeout,
    UserEquipmentSupplyChainRole,
)
from rest_framework.authtoken.models import Token

if settings.DJANGO_ADMIN_FORCE_ALLAUTH:
    # Force the `admin` sign in process to go through the `django-allauth` workflow:
    # https://docs.allauth.org/en/latest/common/admin.html#admin
    admin.autodiscover()
    admin.site.login = secure_admin_login(admin.site.login)  # type: ignore[method-assign]


class UserTypeFilter(SimpleListFilter):
    """Custom filter for user type that displays just the name."""
    title = _("User Type")
    parameter_name = "user_type"

    def lookups(self, request, model_admin):
        """Return list of user types for filtering."""
        return UserType.get_choices()

    def queryset(self, request, queryset):
        """Filter queryset based on selected user type."""
        if self.value():
            return queryset.filter(user_type=self.value())
        return queryset


class UserDocumentInline(admin.StackedInline):
    """Inline admin for user documents."""
    model = UserDocument
    extra = 0
    fields = ('file_preview', 'file', 'document_type', 'description', 'uploaded_at')
    readonly_fields = ('file_preview', 'uploaded_at')
    can_delete = True
    
    def file_preview(self, obj):
        """Display document preview (image thumbnail or file icon) - clickable to view document."""
        if obj and obj.file:
            file_url = obj.file.url
            file_name = obj.file.name.split('/')[-1] if '/' in obj.file.name else obj.file.name
            file_ext = file_name.split('.')[-1].lower() if '.' in file_name else ''
            
            # Check if it's an image
            image_extensions = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg']
            if file_ext in image_extensions:
                return format_html(
                    '<div style="margin: 10px 0;">'
                    '<a href="{}" target="_blank" style="display: inline-block; cursor: pointer;">'
                    '<img src="{}" style="max-height: 150px; max-width: 200px; border: 1px solid #ddd; border-radius: 4px; padding: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); display: block; transition: transform 0.2s;" '
                    'onmouseover="this.style.transform=\'scale(1.05)\'; this.style.borderColor=\'#007bff\';" '
                    'onmouseout="this.style.transform=\'scale(1)\'; this.style.borderColor=\'#ddd\';" />'
                    '</a>'
                    '<small style="color: #666; display: block; margin-top: 5px;">{} (Click to view)</small>'
                    '</div>',
                    file_url,
                    file_url,
                    file_name
                )
            else:
                # Show file icon for non-image files - clickable
                icon_color = {
                    'pdf': '#dc3545',
                    'doc': '#007bff',
                    'docx': '#007bff',
                    'xls': '#28a745',
                    'xlsx': '#28a745',
                    'txt': '#6c757d',
                }.get(file_ext, '#6c757d')
                
                return format_html(
                    '<div style="margin: 10px 0;">'
                    '<a href="{}" target="_blank" style="display: inline-block; cursor: pointer; text-decoration: none;">'
                    '<div style="width: 100px; height: 100px; background-color: {}; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; box-shadow: 0 2px 4px rgba(0,0,0,0.1); transition: transform 0.2s;" '
                    'onmouseover="this.style.transform=\'scale(1.1)\'; this.style.boxShadow=\'0 4px 8px rgba(0,0,0,0.2)\';" '
                    'onmouseout="this.style.transform=\'scale(1)\'; this.style.boxShadow=\'0 2px 4px rgba(0,0,0,0.1)\';" >'
                    '<span style="font-size: 40px; color: white;">📄</span>'
                    '</div>'
                    '</a>'
                    '<br><small style="color: #666; display: block; margin-top: 5px;">{} (Click to view)</small>'
                    '</div>',
                    file_url,
                    icon_color,
                    file_name
                )
        return format_html('<span style="color: #999;">No file</span>')
    
    file_preview.short_description = _("Document Preview (Click to view)")


@admin.register(User)
class UserAdmin(auth_admin.UserAdmin):
    form = UserAdminChangeForm
    add_form = UserAdminCreationForm
    view_on_site = False
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("name", "gender", "user_type", "user_type_alias", "emp_id", "phone_number", "profile_picture", "profile_picture_preview", "department", "designation", "branch_name", "degree_name", "joining_date", "graduation_date")}),
        (
            _("Verification & Approval"),
            {
                "fields": (
                    "email_verified",
                    "admin_approved",
                    "supervisor_approved",
                    "program_start_date",
                    "program_end_date",
                    "access_on_hold",
                    "is_test_account",
                ),
            },
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active_readonly",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
                "description": _(
                    "Note: 'is_active' is automatically managed based on 'email_verified' and 'admin_approved'. "
                    "It will be set to True only when both conditions are met. To change active status, "
                    "update 'email_verified' or 'admin_approved' fields above."
                ),
            },
        ),
        (
            _("OIC dashboard cards"),
            {
                "fields": (
                    "oic_enable_ta_nomination",
                    "oic_enable_ta_duty_assignments",
                    "oic_enable_leave_management",
                    "oic_enable_reward_config",
                ),
                "description": _(
                    "For Officer In Charge (manager) users only. Enable which dashboard cards this OIC can see. "
                    "Admins always see these cards."
                ),
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    list_display = [
        "email",
        "name",
        "user_type_display",
        "department_code",
        "email_verified",
        "admin_approved",
        "supervisor_approved_display",
        "program_end_date",
        "access_on_hold",
        "is_test_account",
        "is_active",
        "is_superuser",
        "approve_actions",
    ]
    list_filter = [
        UserTypeFilter,
        "email_verified",
        "admin_approved",
        "access_on_hold",
        "is_test_account",
        "is_staff",
        "is_superuser",
        "is_active",
    ]
    search_fields = ["name", "email", "emp_id", "phone_number"]
    readonly_fields = ["profile_picture_preview", "is_active_readonly"]
    ordering = ["id"]
    actions = ["approve_users", "reject_users", "approve_supervisor_users", "clear_access_on_hold", "force_logout_users"]
    inlines = [UserDocumentInline]
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "name", "gender", "password1", "password2", "user_type", "user_type_alias", "emp_id", "phone_number", "profile_picture", "department"),
            },
        ),
    )

    def profile_picture_preview(self, obj):
        """Display profile picture preview in admin."""
        if obj and obj.profile_picture:
            return format_html(
                '<div style="margin-top: 10px;">'
                '<img src="{}" style="max-height: 200px; max-width: 200px; border: 1px solid #ddd; border-radius: 4px; padding: 5px; box-shadow: 0 2px 4px rgba(0,0,0,0.1);" />'
                '<br><a href="{}" target="_blank" style="margin-top: 5px; display: inline-block;">View Full Size</a>'
                '</div>',
                obj.profile_picture.url,
                obj.profile_picture.url
            )
        return format_html('<span style="color: #999;">No profile picture uploaded</span>')
    
    profile_picture_preview.short_description = _("Profile Picture Preview")

    def is_active_readonly(self, obj):
        """Display is_active as read-only with explanation."""
        if obj:
            return format_html(
                '<strong>{}</strong><br><small style="color: #666;">Automatically managed based on email_verified and admin_approved</small>',
                "Yes" if obj.is_active else "No"
            )
        return "-"
    
    is_active_readonly.short_description = _("Is Active (Auto-managed)")

    def department_code(self, obj):
        """Display department code."""
        if obj.department and obj.department.code:
            return obj.department.code
        return "-"
    
    department_code.short_description = _("Dept Code")
    department_code.admin_order_field = "department__code"

    def user_type_display(self, obj):
        """Display user type name (or alias for IITR Student)."""
        return obj.get_user_type_display_label() or "-"

    user_type_display.short_description = _("User Type")
    user_type_display.admin_order_field = "user_type"

    def supervisor_approved_display(self, obj):
        """Show supervisor_approved only for Post Doc/RA; '-' otherwise."""
        if not obj:
            return "-"
        if obj.needs_supervisor_approval():
            return "Yes" if obj.supervisor_approved else "No"
        return "-"
    supervisor_approved_display.short_description = _("Supervisor OK")

    def send_approval_email(self, user):
        """Send confirmation email when user account is approved (with web address for online booking)."""
        try:
            web_address = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/") or "/"
            CommunicationService.send_email(
                recipient=user,
                template="registration_approval_confirmation_email",
                template_context={
                    "name": user.name or user.email,
                    "web_address": web_address,
                },
            )
            logger.info(f"Approval confirmation email sent to {user.email}")
        except Exception as e:
            logger.error(f"Failed to send approval email to {user.email}: {str(e)}")

    def approve_actions(self, obj):
        """Display approve/reject action buttons."""
        if not obj:
            return "-"

        buttons = []

        # Show approve button if email is verified but not approved
        if obj.email_verified and not obj.admin_approved:
            approve_url = reverse("admin:users_user_approve", args=[obj.pk])
            buttons.append(
                format_html(
                    '<a class="button" href="{}" style="{}background-color:#28a745;color:#fff;">Approve</a>',
                    approve_url,
                    _ADMIN_ACTION_BTN,
                )
            )
        # Show supervisor approve for Post Doc/RA when not yet supervisor-approved
        if obj.needs_supervisor_approval() and not obj.supervisor_approved:
            sup_approve_url = reverse("admin:users_user_supervisor_approve", args=[obj.pk])
            buttons.append(
                format_html(
                    '<a class="button" href="{}" style="{}background-color:#17a2b8;color:#fff;">Approve (Supervisor)</a>',
                    sup_approve_url,
                    _ADMIN_ACTION_BTN,
                )
            )
        # Show clear access on hold when access_on_hold is True
        if obj.access_on_hold:
            clear_hold_url = reverse("admin:users_user_clear_access_hold", args=[obj.pk])
            buttons.append(
                format_html(
                    '<a class="button" href="{}" style="{}background-color:#ffc107;color:#212529;">Clear access on hold</a>',
                    clear_hold_url,
                    _ADMIN_ACTION_BTN,
                )
            )

        # Show reject button if user is approved
        if obj.admin_approved:
            reject_url = reverse("admin:users_user_reject", args=[obj.pk])
            buttons.append(
                format_html(
                    '<a class="button" href="{}" style="{}background-color:#dc3545;color:#fff;">Reject</a>',
                    reject_url,
                    _ADMIN_ACTION_BTN,
                )
            )
        # Force logout: invalidate session so user must sign in again
        force_logout_url = reverse("admin:users_user_force_logout", args=[obj.pk])
        buttons.append(
            format_html(
                '<a class="button" href="{}" style="{}background-color:#6c757d;color:#fff;" title="Invalidate this user\'s session">Force logout</a>',
                force_logout_url,
                _ADMIN_ACTION_BTN,
            )
        )

        return _admin_action_buttons(*buttons)

    approve_actions.short_description = _("Actions")

    def get_urls(self):
        """Add custom URLs for approve/reject actions."""
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:user_id>/approve/",
                self.admin_site.admin_view(self.approve_user_view),
                name="users_user_approve",
            ),
            path(
                "<int:user_id>/reject/",
                self.admin_site.admin_view(self.reject_user_view),
                name="users_user_reject",
            ),
            path(
                "<int:user_id>/supervisor-approve/",
                self.admin_site.admin_view(self.supervisor_approve_user_view),
                name="users_user_supervisor_approve",
            ),
            path(
                "<int:user_id>/clear-access-hold/",
                self.admin_site.admin_view(self.clear_access_hold_view),
                name="users_user_clear_access_hold",
            ),
            path(
                "<int:user_id>/force-logout/",
                self.admin_site.admin_view(self.force_logout_view),
                name="users_user_force_logout",
            ),
        ]
        return custom_urls + urls

    def approve_user_view(self, request, user_id):
        """Handle approve action for a single user."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages
        
        user = get_object_or_404(User, pk=user_id)
        
        if not user.admin_approved:
            # Once admin approves, user is deemed active; mark email verified too.
            if not user.email_verified:
                user.email_verified = True
            user.admin_approved = True
            user.save(update_fields=["email_verified", "admin_approved"])
            # Send approval email
            self.send_approval_email(user)
            messages.success(request, f"User {user.email} has been approved and activated. Approval email sent.")
        else:
            messages.info(request, f"User {user.email} is already approved.")
        
        return redirect("admin:users_user_changelist")

    def supervisor_approve_user_view(self, request, user_id):
        """Set supervisor_approved=True for Post Doc/RA users."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages

        user = get_object_or_404(User, pk=user_id)
        if not user.needs_supervisor_approval():
            messages.warning(request, f"User {user.email} does not require supervisor approval.")
            return redirect("admin:users_user_changelist")
        if user.supervisor_approved:
            messages.info(request, f"User {user.email} is already supervisor-approved.")
            return redirect("admin:users_user_changelist")
        user.supervisor_approved = True
        user.save(update_fields=["supervisor_approved"])
        user.update_active_status()
        messages.success(request, f"Supervisor approval recorded for {user.email}.")
        return redirect("admin:users_user_changelist")

    def clear_access_hold_view(self, request, user_id):
        """Clear access_on_hold after revalidation (documentary evidence)."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages

        user = get_object_or_404(User, pk=user_id)
        if not user.access_on_hold:
            messages.info(request, f"User {user.email} is not on access hold.")
            return redirect("admin:users_user_changelist")
        user.access_on_hold = False
        user.save(update_fields=["access_on_hold"])
        user.update_active_status()
        messages.success(request, f"Access on hold cleared for {user.email}. Ensure documentary evidence was verified.")
        return redirect("admin:users_user_changelist")

    def force_logout_view(self, request, user_id):
        """Invalidate auth token for this user so they must sign in again."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages
        from django.core.cache import cache
        from iic_booking.users.api.token_auth import CACHE_KEY_PREFIX

        user = get_object_or_404(User, pk=user_id)
        tokens = list(Token.objects.filter(user=user).values_list("key", flat=True))
        if tokens:
            for key in tokens:
                cache.delete(f"{CACHE_KEY_PREFIX}{key}")
            Token.objects.filter(user=user).delete()
            messages.success(request, f"User {user.email} has been signed out. They must sign in again.")
        else:
            messages.info(request, f"User {user.email} had no active session.")
        return redirect("admin:users_user_changelist")

    def reject_user_view(self, request, user_id):
        """Handle reject action for a single user."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages
        
        user = get_object_or_404(User, pk=user_id)
        
        if user.admin_approved:
            user.admin_approved = False
            user.save(update_fields=["admin_approved"])
            messages.success(request, f"User {user.email} has been rejected and deactivated.")
        else:
            messages.warning(request, f"User {user.email} is not approved.")
        
        return redirect("admin:users_user_changelist")

    def get_queryset(self, request):
        """Optimize queryset."""
        queryset = super().get_queryset(request)
        return queryset.select_related("department")

    def get_list_display(self, request):
        """Customize list display based on user permissions."""
        display = list(self.list_display)
        if not request.user.is_superuser:
            # Remove sensitive fields for non-superusers if needed
            pass
        return display

    @admin.action(description="Approve selected users")
    def approve_users(self, request, queryset):
        """Approve selected users."""
        count = 0
        email_sent_count = 0
        for user in queryset:
            if not user.admin_approved:
                if not user.email_verified:
                    user.email_verified = True
                user.admin_approved = True
                # is_active will be automatically set to True by save() method
                user.save(update_fields=["email_verified", "admin_approved"])
                # Send approval email
                try:
                    self.send_approval_email(user)
                    email_sent_count += 1
                except Exception as e:
                    logger.error(f"Failed to send approval email to {user.email}: {str(e)}")
                count += 1
        
        if count > 0:
            message = f"Successfully approved {count} user(s). Accounts have been activated."
            if email_sent_count < count:
                message += f" Approval emails sent to {email_sent_count} user(s)."
            else:
                message += " Approval emails sent to all users."
            self.message_user(request, message)
        else:
            self.message_user(
                request,
                "No users were approved. Only users with verified emails can be approved.",
                level="warning",
            )
    
    approve_users.allowed_permissions = ("change",)

    @admin.action(description="Reject selected users")
    def reject_users(self, request, queryset):
        """Reject selected users."""
        count = 0
        for user in queryset:
            if user.admin_approved:
                user.admin_approved = False
                # is_active will be automatically set to False by save() method
                user.save(update_fields=["admin_approved"])
                count += 1
        
        if count > 0:
            self.message_user(
                request,
                f"Successfully rejected {count} user(s). Accounts have been deactivated.",
            )
        else:
            self.message_user(
                request,
                "No users were rejected.",
                level="warning",
            )
    
    reject_users.allowed_permissions = ("change",)

    @admin.action(description="Approve (supervisor) selected Post Doc/RA users")
    def approve_supervisor_users(self, request, queryset):
        """Set supervisor_approved=True for selected users that need it."""
        count = 0
        for user in queryset:
            if user.needs_supervisor_approval() and not user.supervisor_approved:
                user.supervisor_approved = True
                user.save(update_fields=["supervisor_approved"])
                user.update_active_status()
                count += 1
        if count > 0:
            self.message_user(request, f"Supervisor approval set for {count} user(s).")
        else:
            self.message_user(
                request,
                "No users were updated. Only Post Doc/RA users without supervisor approval were processed.",
                level="warning",
            )
    approve_supervisor_users.allowed_permissions = ("change",)

    @admin.action(description="Clear access on hold (after revalidation)")
    def clear_access_on_hold(self, request, queryset):
        """Clear access_on_hold for selected users."""
        count = 0
        for user in queryset:
            if user.access_on_hold:
                user.access_on_hold = False
                user.save(update_fields=["access_on_hold"])
                user.update_active_status()
                count += 1
        if count > 0:
            self.message_user(
                request,
                f"Access on hold cleared for {count} user(s). Ensure documentary evidence was verified.",
            )
        else:
            self.message_user(
                request,
                "No users had access on hold.",
                level="warning",
            )
    clear_access_on_hold.allowed_permissions = ("change",)

    @admin.action(description="Force logout selected users")
    def force_logout_users(self, request, queryset):
        """Invalidate all auth tokens for selected users so they must sign in again."""
        from django.core.cache import cache
        from iic_booking.users.api.token_auth import CACHE_KEY_PREFIX

        count = 0
        for user in queryset:
            tokens = list(Token.objects.filter(user=user).values_list("key", flat=True))
            if tokens:
                for key in tokens:
                    cache.delete(f"{CACHE_KEY_PREFIX}{key}")
                Token.objects.filter(user=user).delete()
                count += 1
        if count > 0:
            self.message_user(
                request,
                f"Force logout: {count} user(s) have been signed out. They must sign in again.",
            )
        else:
            self.message_user(
                request,
                "No selected users had an active session to invalidate.",
                level="warning",
            )
    force_logout_users.allowed_permissions = ("change",)

    def changelist_view(self, request, extra_context=None):
        """Add pending users count to context."""
        extra_context = extra_context or {}
        pending_count = User.objects.filter(email_verified=True, admin_approved=False).count()
        extra_context["pending_count"] = pending_count
        return super().changelist_view(request, extra_context)


@admin.register(AuthSettings)
class AuthSettingsAdmin(admin.ModelAdmin):
    """Singleton-style admin for auth settings (e.g. inactivity timeout)."""

    list_display = ["inactivity_timeout_seconds_display"]
    fieldsets = (
        (
            _("Auto-logout (inactivity)"),
            {
                "fields": ("inactivity_timeout_seconds",),
                "description": _(
                    "Users are automatically logged out after this many seconds without any activity. "
                    "The frontend will log out slightly before this so the session is cleared in time. "
                    "Example: 1800 = 30 minutes, 900 = 15 minutes."
                ),
            },
        ),
    )

    def inactivity_timeout_seconds_display(self, obj):
        if not obj:
            return "-"
        sec = obj.inactivity_timeout_seconds
        return _("%(seconds)s seconds (%(minutes)s minutes)") % {"seconds": sec, "minutes": sec // 60}
    inactivity_timeout_seconds_display.short_description = _("Inactivity timeout")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        """Redirect to the single AuthSettings object (id=1) edit page."""
        from django.shortcuts import redirect
        obj = AuthSettings.get_singleton()
        return redirect("admin:users_authsettings_change", object_id=obj.pk)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from django.core.cache import cache
        from iic_booking.users.api.token_auth import CACHE_KEY_GLOBAL_TIMEOUT
        cache.delete(CACHE_KEY_GLOBAL_TIMEOUT)


@admin.register(WalletCreditFacilitySettings)
class WalletCreditFacilitySettingsAdmin(admin.ModelAdmin):
    """Singleton: threshold, credit window days, max credit for faculty recharge overdraft."""

    list_display = ["__str__", "balance_threshold_inr", "credit_window_days", "max_credit_inr"]

    fieldsets = (
        (
            _("Credit facility"),
            {
                "fields": ("balance_threshold_inr", "credit_window_days", "max_credit_inr"),
                "description": _(
                    "When a faculty member’s department sub-wallet balance is below the threshold, "
                    "they may opt into a temporary credit line while a wallet recharge request is pending. "
                    "The line is capped at the lesser of “Maximum credit” and the requested recharge amount. "
                    "If parse credit does not arrive within the window, bookings for that department are blocked."
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        return not WalletCreditFacilitySettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(DepartmentFacultyCreditFacilitySettings)
class DepartmentFacultyCreditFacilitySettingsAdmin(admin.ModelAdmin):
    list_display = ["department", "enabled", "joining_date_cutoff", "max_credit_limit", "updated_at"]
    list_filter = ["enabled"]
    search_fields = ["department__name", "department__code"]
    raw_id_fields = ["department", "updated_by"]


@admin.register(FacultyDepartmentCreditFacility)
class FacultyDepartmentCreditFacilityAdmin(admin.ModelAdmin):
    list_display = ["user", "department", "status", "credit_limit", "availed_at", "closed_at"]
    list_filter = ["status"]
    search_fields = ["user__email", "user__name", "department__name"]
    raw_id_fields = ["user", "department"]
    readonly_fields = ["availed_at", "closed_at", "created_at", "updated_at"]


@admin.register(FacultyDepartmentCreditFacilityAuditLog)
class FacultyDepartmentCreditFacilityAuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "event_type", "department", "faculty_user", "actor"]
    list_filter = ["event_type"]
    search_fields = ["faculty_user__email", "message", "department__name"]
    raw_id_fields = ["facility", "department", "faculty_user", "actor"]
    readonly_fields = ["created_at"]


@admin.register(WalletStudentRechargeSettings)
class WalletStudentRechargeSettingsAdmin(admin.ModelAdmin):
    """Singleton: allow IITR Students to recharge the faculty shared wallet."""

    list_display = ["__str__", "enable_iitr_student_wallet_recharge"]

    fieldsets = (
        (
            _("IITR Student recharge"),
            {
                "fields": ("enable_iitr_student_wallet_recharge",),
                "description": _(
                    "When enabled, IITR Students may recharge via SBIePay or Offline Request "
                    "(payment receipt upload). Funds are parked in the faculty wallet they are "
                    "linked to. Individual Students are not affected by this setting."
                ),
            },
        ),
    )

    def has_add_permission(self, request):
        return not WalletStudentRechargeSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WalletSricSettings)
class WalletSricSettingsAdmin(admin.ModelAdmin):
    """Singleton: SRIC Office emails + per-internal-department grant codes."""

    change_form_template = "admin/users/walletsricsettings/change_form.html"
    list_display = ["recipient_preview"]

    def recipient_preview(self, obj):
        if not obj:
            return "-"
        raw = (obj.recipient_emails or "").strip()
        if not raw:
            return _("(none configured)")
        return raw[:120] + ("…" if len(raw) > 120 else "")

    recipient_preview.short_description = _("Recipients")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect

        obj = WalletSricSettings.get_singleton()
        return redirect("admin:users_walletsricsettings_change", object_id=obj.pk)

    def _internal_department_qs(self):
        return Department.objects.filter(
            department_type=DepartmentType.INTERNAL,
        ).order_by("name", "code")

    def _grant_formset_class(self):
        from django import forms

        class InternalDepartmentGrantForm(forms.ModelForm):
            class Meta:
                model = Department
                fields = ("internal_grant_code",)
                widgets = {
                    "internal_grant_code": forms.TextInput(
                        attrs={
                            "maxlength": 80,
                            "placeholder": "e.g. IIC-000-002",
                            "autocomplete": "off",
                        }
                    ),
                }

        return forms.modelformset_factory(
            Department,
            form=InternalDepartmentGrantForm,
            extra=0,
            can_delete=False,
        )

    def _settings_form_class(self):
        from django import forms

        class WalletSricSettingsForm(forms.ModelForm):
            class Meta:
                model = WalletSricSettings
                fields = ("recipient_emails", "grant_code_for_credit")
                widgets = {
                    "recipient_emails": forms.Textarea(attrs={"rows": 5, "cols": 80}),
                    "grant_code_for_credit": forms.TextInput(attrs={"maxlength": 80}),
                }

        return WalletSricSettingsForm

    def change_view(self, request, object_id, form_url="", extra_context=None):
        from django.contrib import messages
        from django.shortcuts import get_object_or_404, redirect
        from django.template.response import TemplateResponse

        obj = get_object_or_404(WalletSricSettings, pk=object_id)
        qs = self._internal_department_qs()
        FormClass = self._settings_form_class()
        FormSet = self._grant_formset_class()
        prefix = "dept_grants"

        if request.method == "POST":
            form = FormClass(request.POST, instance=obj)
            formset = FormSet(request.POST, queryset=qs, prefix=prefix)
            if form.is_valid() and formset.is_valid():
                form.save()
                formset.save()
                messages.success(
                    request,
                    _("SRIC Office settings and department grant codes were saved."),
                )
                return redirect("admin:users_walletsricsettings_change", object_id=obj.pk)
        else:
            form = FormClass(instance=obj)
            formset = FormSet(queryset=qs, prefix=prefix)

        context = {
            **self.admin_site.each_context(request),
            **(extra_context or {}),
            "opts": self.model._meta,
            "original": obj,
            "title": _("Change %s") % self.model._meta.verbose_name,
            "form": form,
            "formset": formset,
            "has_view_permission": self.has_view_permission(request, obj),
            "has_change_permission": self.has_change_permission(request, obj),
            "has_add_permission": False,
            "has_delete_permission": False,
            "is_popup": False,
            "save_as": False,
            "show_save": True,
        }
        return TemplateResponse(request, self.change_form_template, context)


@admin.register(TestAccountEmailSettings)
class TestAccountEmailSettingsAdmin(admin.ModelAdmin):
    """Singleton: redirect addresses for is_test_account outbound email."""

    list_display = ["recipient_preview"]
    fields = ("recipient_emails",)

    def recipient_preview(self, obj):
        if not obj:
            return "-"
        raw = (obj.recipient_emails or "").strip()
        if not raw:
            return _("(none — using env / default)")
        return raw[:120] + ("…" if len(raw) > 120 else "")

    recipient_preview.short_description = _("Redirect addresses")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        from django.shortcuts import redirect

        obj = TestAccountEmailSettings.get_singleton()
        return redirect("admin:users_testaccountemailsettings_change", object_id=obj.pk)

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        from django import forms

        if db_field.name == "recipient_emails":
            kwargs["widget"] = forms.Textarea(attrs={"rows": 6, "cols": 80})
        return super().formfield_for_dbfield(db_field, request, **kwargs)


@admin.register(UserTypeInactivityTimeout)
class UserTypeInactivityTimeoutAdmin(admin.ModelAdmin):
    """Per-user-type inactivity timeout (overrides global for that type)."""

    list_display = ["user_type", "inactivity_timeout_seconds", "timeout_minutes_display"]
    ordering = ["user_type"]

    def timeout_minutes_display(self, obj):
        if not obj:
            return "-"
        return _("{} min").format(obj.inactivity_timeout_seconds // 60)
    timeout_minutes_display.short_description = _("Minutes")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from django.core.cache import cache
        from iic_booking.users.api.token_auth import CACHE_KEY_TYPE_TIMEOUT_PREFIX
        cache.delete(f"{CACHE_KEY_TYPE_TIMEOUT_PREFIX}{obj.user_type}")


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    """Admin interface for Department model."""

    raw_id_fields = ["head"]
    list_display = ["name", "code", "internal_grant_code", "department_type_display", "internal_subcategory_display", "external_subcategory_display", "external_state_display", "user_count", "equipment_count", "created_at"]
    list_filter = ["department_type", "internal_subcategory", "external_subcategory", "state", "created_at"]
    search_fields = ["name", "code", "internal_grant_code", "description"]
    readonly_fields = ["created_at", "updated_at"]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "code",
                    "internal_grant_code",
                    "department_type",
                    "internal_subcategory",
                    "external_subcategory",
                    "state",
                    "head",
                    "description",
                )
            },
        ),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    def department_type_display(self, obj):
        """Display department type."""
        return obj.get_department_type_display() if obj.department_type else "-"
    
    department_type_display.short_description = _("Department Type")
    department_type_display.admin_order_field = "department_type"

    def internal_subcategory_display(self, obj):
        """Display internal subcategory (only for Internal type)."""
        if not obj or obj.department_type != DepartmentType.INTERNAL:
            return "-"
        return obj.get_internal_subcategory_display() if obj.internal_subcategory else "-"

    internal_subcategory_display.short_description = _("Internal subcategory")
    internal_subcategory_display.admin_order_field = "internal_subcategory"

    def external_subcategory_display(self, obj):
        """Display external subcategory (only for External type)."""
        if not obj or obj.department_type != DepartmentType.EXTERNAL:
            return "-"
        return obj.get_external_subcategory_display() if obj.external_subcategory else "-"
    
    external_subcategory_display.short_description = _("External subcategory")
    external_subcategory_display.admin_order_field = "external_subcategory"

    def external_state_display(self, obj):
        """Display state/UT (only for External type)."""
        if not obj or obj.department_type != DepartmentType.EXTERNAL:
            return "-"
        return obj.get_state_display() if obj.state else "-"

    external_state_display.short_description = _("State / UT")
    external_state_display.admin_order_field = "state"

    def user_count(self, obj):
        """Get count of users in this department."""
        return obj.users.count()

    user_count.short_description = _("User Count")

    def equipment_count(self, obj):
        """Count of equipment mapped to this (internal) department."""
        from iic_booking.equipment.models import Equipment
        return Equipment.objects.filter(internal_department=obj).count()

    equipment_count.short_description = _("Equipment Count")


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    """Admin for Wallet (container; balance is sum of sub-wallets)."""

    list_display = ["user_email", "user_name", "user_type", "total_balance_display", "sub_wallet_count"]
    search_fields = ["user__email", "user__name", "user__emp_id"]
    readonly_fields = ["total_balance_display"]
    fieldsets = ((None, {"fields": ("user", "total_balance_display")}),)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user")

    def user_email(self, obj):
        return obj.user.email if obj.user else "-"
    user_email.short_description = _("User Email")
    user_email.admin_order_field = "user__email"

    def user_name(self, obj):
        return obj.user.name if obj.user and obj.user.name else "-"
    user_name.short_description = _("User Name")
    user_name.admin_order_field = "user__name"

    def user_type(self, obj):
        if obj.user:
            return obj.user.get_user_type_display_label() or "-"
        return "-"
    user_type.short_description = _("User Type")
    user_type.admin_order_field = "user__user_type"

    def total_balance_display(self, obj):
        if obj:
            return format_html('<strong style="color: #28a745;">₹{}</strong>', obj.total_balance)
        return "0.00"
    total_balance_display.short_description = _("Total Balance")

    def sub_wallet_count(self, obj):
        return obj.sub_wallets.count() if obj else 0
    sub_wallet_count.short_description = _("Sub-wallets")


@admin.register(SubWallet)
class SubWalletAdmin(admin.ModelAdmin):
    """Admin interface for SubWallet (department-wise) model."""

    list_display = ["id", "wallet_user", "department", "balance", "created_at", "credit_debit_actions"]
    list_filter = ["department", "created_at"]
    search_fields = ["wallet__user__email", "wallet__user__name", "department__name"]
    readonly_fields = ["created_at", "updated_at", "balance_display"]
    list_select_related = ["wallet", "wallet__user", "department"]
    actions = ["credit_selected", "debit_selected"]

    fieldsets = (
        (None, {"fields": ("wallet", "department", "balance_display")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    def wallet_user(self, obj):
        return obj.wallet.user.email if obj.wallet and obj.wallet.user else "-"
    wallet_user.short_description = _("User")
    wallet_user.admin_order_field = "wallet__user__email"

    def balance_display(self, obj):
        """Display balance with formatting."""
        if obj:
            return format_html('<strong style="color: #28a745; font-size: 1.2em;">₹{}</strong>', obj.balance)
        return "₹0.00"
    balance_display.short_description = _("Balance")

    def credit_debit_actions(self, obj):
        """Display credit/debit action buttons."""
        if not obj:
            return "-"
        
        credit_url = reverse("admin:users_subwallet_credit", args=[obj.pk])
        debit_url = reverse("admin:users_subwallet_debit", args=[obj.pk])
        
        return _admin_action_buttons(
            format_html(
                '<a class="button" href="{}" style="{}background-color:#28a745;color:#fff;">Credit</a>',
                credit_url,
                _ADMIN_ACTION_BTN,
            ),
            format_html(
                '<a class="button" href="{}" style="{}background-color:#dc3545;color:#fff;">Debit</a>',
                debit_url,
                _ADMIN_ACTION_BTN,
            ),
        )

    credit_debit_actions.short_description = _("Actions")

    def get_urls(self):
        """Add custom URLs for credit/debit actions."""
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:subwallet_id>/credit/",
                self.admin_site.admin_view(self.credit_subwallet_view),
                name="users_subwallet_credit",
            ),
            path(
                "<int:subwallet_id>/debit/",
                self.admin_site.admin_view(self.debit_subwallet_view),
                name="users_subwallet_debit",
            ),
        ]
        return custom_urls + urls

    def credit_subwallet_view(self, request, subwallet_id):
        """Handle credit action for a sub-wallet."""
        from django.shortcuts import redirect, get_object_or_404, render
        from django.contrib import messages
        from .forms import WalletCreditForm
        
        sub_wallet = get_object_or_404(SubWallet, pk=subwallet_id)
        
        if request.method == "POST":
            form = WalletCreditForm(request.POST)
            if form.is_valid():
                try:
                    amount = form.cleaned_data["amount"]
                    description = form.cleaned_data.get("description", "") or f"Admin credit - {request.user.email}"
                    transaction = sub_wallet.credit(amount, description)
                    messages.success(
                        request,
                        f"Successfully credited ₹{amount} to {sub_wallet.department.name} sub-wallet for {sub_wallet.wallet.user.email}. Transaction ID: {transaction.id}"
                    )
                    return redirect("admin:users_subwallet_changelist")
                except ValueError as e:
                    messages.error(request, f"Error: {str(e)}")
                except Exception as e:
                    messages.error(request, f"Unexpected error: {str(e)}")
        else:
            form = WalletCreditForm()
        
        context = {
            "title": f"Credit Sub-Wallet: {sub_wallet.department.name}",
            "sub_wallet": sub_wallet,
            "form": form,
            "opts": self.model._meta,
            "has_view_permission": self.has_view_permission(request, sub_wallet),
            "has_change_permission": self.has_change_permission(request, sub_wallet),
        }
        return render(request, "admin/users/subwallet/credit_form.html", context)

    def debit_subwallet_view(self, request, subwallet_id):
        """Handle debit action for a sub-wallet."""
        from django.shortcuts import redirect, get_object_or_404, render
        from django.contrib import messages
        from .forms import WalletDebitForm
        
        sub_wallet = get_object_or_404(SubWallet, pk=subwallet_id)
        
        if request.method == "POST":
            form = WalletDebitForm(request.POST)
            if form.is_valid():
                try:
                    amount = form.cleaned_data["amount"]
                    description = form.cleaned_data.get("description", "") or f"Admin debit - {request.user.email}"
                    transaction = sub_wallet.debit(amount, description)
                    messages.success(
                        request,
                        f"Successfully debited ₹{amount} from {sub_wallet.department.name} sub-wallet for {sub_wallet.wallet.user.email}. Transaction ID: {transaction.id}"
                    )
                    return redirect("admin:users_subwallet_changelist")
                except ValueError as e:
                    messages.error(request, f"Error: {str(e)}")
                except Exception as e:
                    messages.error(request, f"Unexpected error: {str(e)}")
        else:
            form = WalletDebitForm()
        
        context = {
            "title": f"Debit Sub-Wallet: {sub_wallet.department.name}",
            "sub_wallet": sub_wallet,
            "form": form,
            "opts": self.model._meta,
            "has_view_permission": self.has_view_permission(request, sub_wallet),
            "has_change_permission": self.has_change_permission(request, sub_wallet),
        }
        return render(request, "admin/users/subwallet/debit_form.html", context)

    @admin.action(description="Credit selected sub-wallets")
    def credit_selected(self, request, queryset):
        """Credit selected sub-wallets (bulk action)."""
        from django.shortcuts import redirect, render
        from .forms import WalletCreditForm
        
        # Store selected IDs in session for POST request
        if request.method == "GET":
            selected_ids = request.GET.getlist(admin.ACTION_CHECKBOX_NAME)
            request.session['subwallet_selected_ids'] = selected_ids
        
        if request.method == "POST":
            form = WalletCreditForm(request.POST)
            if form.is_valid():
                # Get selected IDs from session or POST
                selected_ids = request.POST.getlist('selected_ids') or request.session.get('subwallet_selected_ids', [])
                if not selected_ids:
                    self.message_user(request, "No sub-wallets selected.", level="error")
                    return redirect("admin:users_subwallet_changelist")
                
                queryset = self.model.objects.filter(id__in=selected_ids)
                amount = form.cleaned_data["amount"]
                description = form.cleaned_data.get("description", "") or f"Bulk admin credit - {request.user.email}"
                count = 0
                errors = []
                
                for sub_wallet in queryset:
                    try:
                        sub_wallet.credit(amount, description)
                        count += 1
                    except Exception as e:
                        errors.append(f"{sub_wallet.department.name}: {str(e)}")
                
                if count > 0:
                    self.message_user(
                        request,
                        f"Successfully credited ₹{amount} to {count} sub-wallet(s).",
                    )
                if errors:
                    self.message_user(
                        request,
                        f"Errors: {'; '.join(errors)}",
                        level="error",
                    )
                # Clear session
                if 'subwallet_selected_ids' in request.session:
                    del request.session['subwallet_selected_ids']
                return redirect("admin:users_subwallet_changelist")
        else:
            form = WalletCreditForm()
        
        context = {
            "title": f"Credit {queryset.count()} Selected Sub-Wallet(s)",
            "sub_wallets": queryset,
            "form": form,
            "opts": self.model._meta,
            "selected_ids": [str(sw.id) for sw in queryset],
        }
        return render(request, "admin/users/subwallet/bulk_credit_form.html", context)

    @admin.action(description="Debit selected sub-wallets")
    def debit_selected(self, request, queryset):
        """Debit selected sub-wallets (bulk action)."""
        from django.shortcuts import redirect, render
        from .forms import WalletDebitForm
        
        # Store selected IDs in session for POST request
        if request.method == "GET":
            selected_ids = request.GET.getlist(admin.ACTION_CHECKBOX_NAME)
            request.session['subwallet_selected_ids'] = selected_ids
        
        if request.method == "POST":
            form = WalletDebitForm(request.POST)
            if form.is_valid():
                # Get selected IDs from session or POST
                selected_ids = request.POST.getlist('selected_ids') or request.session.get('subwallet_selected_ids', [])
                if not selected_ids:
                    self.message_user(request, "No sub-wallets selected.", level="error")
                    return redirect("admin:users_subwallet_changelist")
                
                queryset = self.model.objects.filter(id__in=selected_ids)
                amount = form.cleaned_data["amount"]
                description = form.cleaned_data.get("description", "") or f"Bulk admin debit - {request.user.email}"
                count = 0
                errors = []
                
                for sub_wallet in queryset:
                    try:
                        sub_wallet.debit(amount, description)
                        count += 1
                    except Exception as e:
                        errors.append(f"{sub_wallet.department.name}: {str(e)}")
                
                if count > 0:
                    self.message_user(
                        request,
                        f"Successfully debited ₹{amount} from {count} sub-wallet(s).",
                    )
                if errors:
                    self.message_user(
                        request,
                        f"Errors: {'; '.join(errors)}",
                        level="error",
                    )
                # Clear session
                if 'subwallet_selected_ids' in request.session:
                    del request.session['subwallet_selected_ids']
                return redirect("admin:users_subwallet_changelist")
        else:
            form = WalletDebitForm()
        
        context = {
            "title": f"Debit {queryset.count()} Selected Sub-Wallet(s)",
            "sub_wallets": queryset,
            "form": form,
            "opts": self.model._meta,
            "selected_ids": [str(sw.id) for sw in queryset],
        }
        return render(request, "admin/users/subwallet/bulk_debit_form.html", context)


@admin.register(SubWalletTransaction)
class SubWalletTransactionAdmin(admin.ModelAdmin):
    """Admin interface for SubWalletTransaction model. Supports deletion with balance reversal."""

    list_display = ["id", "sub_wallet_display", "transaction_type", "amount", "description_short", "created_at"]
    list_filter = ["transaction_type", "created_at"]
    search_fields = ["sub_wallet__wallet__user__email", "sub_wallet__department__name", "description"]
    readonly_fields = ["sub_wallet", "transaction_type", "amount", "description", "created_at"]
    list_display_links = ["id", "sub_wallet_display"]

    def sub_wallet_display(self, obj):
        if obj.sub_wallet:
            return f"{obj.sub_wallet.wallet.user.email} – {obj.sub_wallet.department.name}"
        return "-"
    sub_wallet_display.short_description = _("Sub-Wallet")

    def description_short(self, obj):
        if obj.description:
            return obj.description[:50] + "..." if len(obj.description) > 50 else obj.description
        return "-"
    description_short.short_description = _("Description")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return request.user.has_perm("users.delete_subwallettransaction")

    def _reverse_balance_for_transaction(self, txn):
        """Reverse the sub_wallet balance for this transaction, then delete it."""
        from django.db.models import F
        sub_wallet = txn.sub_wallet
        amount = txn.amount
        if txn.transaction_type == SubWalletTransaction.TransactionType.CREDIT:
            sub_wallet.__class__.objects.filter(pk=sub_wallet.pk).update(balance=F("balance") - amount)
        else:
            sub_wallet.__class__.objects.filter(pk=sub_wallet.pk).update(balance=F("balance") + amount)
        txn.delete()

    def delete_model(self, request, obj):
        """Delete a single transaction and reverse its effect on sub_wallet balance."""
        self._reverse_balance_for_transaction(obj)

    def delete_queryset(self, request, queryset):
        """Delete selected transactions and reverse each one's effect on sub_wallet balance."""
        for txn in queryset:
            self._reverse_balance_for_transaction(txn)


@admin.register(WalletRazorpayOrder)
class WalletRazorpayOrderAdmin(admin.ModelAdmin):
    """Admin for Razorpay orders (sub-wallet recharge)."""
    list_display = ["order_id", "wallet_user", "department", "amount_paise", "created_at"]
    list_filter = ["department", "created_at"]
    search_fields = ["order_id", "wallet__user__email"]
    readonly_fields = ["order_id", "amount_paise", "created_at"]

    def wallet_user(self, obj):
        return obj.wallet.user.email if obj.wallet and obj.wallet.user else "-"
    wallet_user.short_description = _("User")


@admin.register(UserDocument)
class UserDocumentAdmin(admin.ModelAdmin):
    """Admin interface for UserDocument model."""
    
    list_display = ["file_preview", "user_email", "user_name", "document_type", "description_short", "uploaded_at"]
    list_filter = ["document_type", "uploaded_at"]
    search_fields = ["user__email", "user__name", "document_type", "description"]
    readonly_fields = ["file_preview", "uploaded_at", "updated_at"]
    fieldsets = (
        (None, {"fields": ("user", "file", "file_preview", "document_type", "description")}),
        (_("Timestamps"), {"fields": ("uploaded_at", "updated_at")}),
    )
    
    def get_queryset(self, request):
        """Optimize queryset."""
        queryset = super().get_queryset(request)
        return queryset.select_related("user")
    
    def user_email(self, obj):
        """Display user email."""
        return obj.user.email if obj.user else "-"
    user_email.short_description = _("User Email")
    user_email.admin_order_field = "user__email"
    
    def user_name(self, obj):
        """Display user name."""
        return obj.user.name if obj.user and obj.user.name else "-"
    user_name.short_description = _("User Name")
    user_name.admin_order_field = "user__name"
    
    def file_preview(self, obj):
        """Display document preview (image thumbnail or file icon) - clickable to view document."""
        if obj and obj.file:
            file_url = obj.file.url
            file_name = obj.file.name.split('/')[-1] if '/' in obj.file.name else obj.file.name
            file_ext = file_name.split('.')[-1].lower() if '.' in file_name else ''
            
            # Check if it's an image
            image_extensions = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg']
            if file_ext in image_extensions:
                return format_html(
                    '<a href="{}" target="_blank" style="display: inline-block; cursor: pointer;">'
                    '<img src="{}" style="max-height: 60px; max-width: 80px; border: 1px solid #ddd; border-radius: 4px; padding: 2px; object-fit: contain; transition: transform 0.2s;" '
                    'onmouseover="this.style.transform=\'scale(1.1)\'; this.style.borderColor=\'#007bff\';" '
                    'onmouseout="this.style.transform=\'scale(1)\'; this.style.borderColor=\'#ddd\';" />'
                    '</a>',
                    file_url,
                    file_url
                )
            else:
                # Show file icon for non-image files - clickable
                icon_color = {
                    'pdf': '#dc3545',
                    'doc': '#007bff',
                    'docx': '#007bff',
                    'xls': '#28a745',
                    'xlsx': '#28a745',
                    'txt': '#6c757d',
                }.get(file_ext, '#6c757d')
                
                return format_html(
                    '<a href="{}" target="_blank" style="display: inline-block; cursor: pointer; text-decoration: none;">'
                    '<div style="width: 60px; height: 60px; background-color: {}; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; transition: transform 0.2s;" '
                    'onmouseover="this.style.transform=\'scale(1.1)\'; this.style.boxShadow=\'0 4px 8px rgba(0,0,0,0.2)\';" '
                    'onmouseout="this.style.transform=\'scale(1)\'; this.style.boxShadow=\'none\';" >'
                    '<span style="font-size: 24px; color: white;">📄</span>'
                    '</div>'
                    '</a>',
                    file_url,
                    icon_color
                )
        return format_html('<span style="color: #999;">-</span>')
    
    file_preview.short_description = _("Preview (Click to view)")
    
    def description_short(self, obj):
        """Display shortened description."""
        if obj.description:
            return obj.description[:50] + "..." if len(obj.description) > 50 else obj.description
        return "-"
    description_short.short_description = _("Description")
    
    def has_add_permission(self, request):
        """Allow adding documents."""
        return True
    
    def has_change_permission(self, request, obj=None):
        """Allow editing documents."""
        return True
    
    def has_delete_permission(self, request, obj=None):
        """Allow deleting documents."""
        return True


class UserGroupMemberInline(admin.TabularInline):
    """Inline admin for User Group members."""
    model = UserGroupMember
    extra = 0
    autocomplete_fields = ["user"]
    verbose_name = _("Member")
    verbose_name_plural = _("Members")


@admin.register(UserGroup)
class UserGroupAdmin(admin.ModelAdmin):
    """Admin interface for UserGroup (equipment visibility)."""
    list_display = ["code", "name", "member_count", "equipment_count", "created_at", "updated_at"]
    list_filter = ["created_at"]
    search_fields = ["name", "code", "description"]
    readonly_fields = ["created_at", "updated_at"]
    inlines = [UserGroupMemberInline]
    fieldsets = (
        (None, {"fields": ("name", "code", "description")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    def member_count(self, obj):
        return obj.members.count() if obj.pk else 0
    member_count.short_description = _("Members")

    def equipment_count(self, obj):
        return obj.equipment.count() if obj.pk else 0
    equipment_count.short_description = _("Equipment")


@admin.register(UserGroupMember)
class UserGroupMemberAdmin(admin.ModelAdmin):
    """Standalone admin for User Group members (optional)."""
    list_display = ["user_group", "user", "created_at"]
    list_filter = ["user_group"]
    search_fields = ["user_group__name", "user_group__code", "user__email", "user__name"]
    autocomplete_fields = ["user_group", "user"]
    readonly_fields = ["created_at"]


@admin.register(WalletRechargeRequest)
class WalletRechargeRequestAdmin(admin.ModelAdmin):
    """Admin interface for Wallet Recharge Request model."""
    
    list_display = [
        "id",
        "user_email",
        "user_name",
        "department",
        "project_display",
        "amount_display",
        "status_badge",
        "created_at",
        "responded_at",
        "resend_notification_action",
    ]
    actions = ["resend_notifications"]
    list_filter = [
        "status",
        "department",
        "project",
        "created_at",
        "responded_at",
    ]
    search_fields = [
        "user__email",
        "user__name",
        "department__name",
        "department__code",
        "project__name",
        "project__project_code",
        "project__agency",
        "project_details",
    ]
    readonly_fields = [
        "user",
        "wallet",
        "department",
        "amount",
        "project",
        "project_details",
        "project_info_display",
        "status",
        "user_otp_code",
        "user_otp_expires_at",
        "user_otp_verified",
        "otp_code",
        "otp_expires_at",
        "approved_by_email",
        "response_message",
        "created_at",
        "updated_at",
        "responded_at",
    ]
    fieldsets = (
        (
            _("Request Information"),
            {
                "fields": (
                    "user",
                    "wallet",
                    "department",
                    "amount",
                    "project",
                    "project_info_display",
                    "project_details",
                ),
            },
        ),
        (
            _("Status"),
            {
                "fields": (
                    "status",
                    "approved_by_email",
                    "response_message",
                ),
            },
        ),
        (
            _("OTP Information"),
            {
                "fields": (
                    "user_otp_code",
                    "user_otp_expires_at",
                    "user_otp_verified",
                    "otp_code",
                    "otp_expires_at",
                ),
                "classes": ("collapse",),
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": (
                    "created_at",
                    "updated_at",
                    "responded_at",
                ),
            },
        ),
    )
    
    def get_queryset(self, request):
        """Optimize queryset."""
        queryset = super().get_queryset(request)
        return queryset.select_related("user", "wallet", "wallet__user", "department", "project")
    
    def user_email(self, obj):
        """Display user email."""
        return obj.user.email if obj.user else "-"
    user_email.short_description = _("User Email")
    user_email.admin_order_field = "user__email"
    
    def user_name(self, obj):
        """Display user name."""
        return obj.user.name if obj.user and obj.user.name else "-"
    user_name.short_description = _("User Name")
    user_name.admin_order_field = "user__name"
    
    def amount_display(self, obj):
        """Display amount with formatting."""
        if obj:
            return format_html('<strong style="color: #28a745;">₹{}</strong>', obj.amount)
        return "₹0.00"
    amount_display.short_description = _("Amount")
    amount_display.admin_order_field = "amount"
    
    def status_badge(self, obj):
        """Display status with badge styling."""
        if not obj:
            return "-"
        
        status_colors = {
            WalletRechargeRequestStatus.PENDING: ("secondary", "#6c757d"),
            WalletRechargeRequestStatus.APPROVED: ("default", "#28a745"),
            WalletRechargeRequestStatus.REJECTED: ("destructive", "#dc3545"),
            WalletRechargeRequestStatus.CANCELLED: ("outline", "#6c757d"),
        }
        
        color_class, bg_color = status_colors.get(obj.status, ("secondary", "#6c757d"))
        return format_html(
            '<span style="background-color: {}; color: white; padding: 3px 8px; border-radius: 3px; font-size: 11px; font-weight: bold;">{}</span>',
            bg_color,
            obj.get_status_display()
        )
    status_badge.short_description = _("Status")
    status_badge.admin_order_field = "status"
    
    def project_display(self, obj):
        """Display project information in list view."""
        if obj.project:
            return format_html(
                '<strong>{}</strong><br/><small style="color: #6c757d;">Code: {} | Agency: {}</small>',
                obj.project.name,
                obj.project.project_code,
                obj.project.agency
            )
        elif obj.project_details:
            return format_html('<span style="color: #6c757d;">{}</span>', obj.project_details[:50] + "..." if len(obj.project_details) > 50 else obj.project_details)
        return "-"
    project_display.short_description = _("Project")
    project_display.admin_order_field = "project__name"
    
    def project_info_display(self, obj):
        """Display detailed project information in change view."""
        if obj.project:
            return format_html(
                '<div style="background-color: #f8f9fa; padding: 15px; border-radius: 5px; border-left: 4px solid #2563eb; margin: 10px 0;">'
                '<h4 style="margin-top: 0; color: #2563eb;">Project Details</h4>'
                '<p style="margin: 5px 0;"><strong>Project Name:</strong> {}</p>'
                '<p style="margin: 5px 0;"><strong>Project Code:</strong> {}</p>'
                '<p style="margin: 5px 0;"><strong>Agency:</strong> {}</p>'
                '<p style="margin: 5px 0;"><strong>Start Date:</strong> {}</p>'
                '<p style="margin: 5px 0;"><strong>End Date:</strong> {}</p>'
                '<p style="margin: 5px 0;"><strong>Status:</strong> <span style="background-color: {}; color: white; padding: 2px 6px; border-radius: 3px; font-size: 11px;">{}</span></p>'
                '</div>',
                obj.project.name,
                obj.project.project_code,
                obj.project.agency,
                obj.project.start_date.strftime('%Y-%m-%d') if obj.project.start_date else 'Not set',
                obj.project.end_date.strftime('%Y-%m-%d') if obj.project.end_date else 'Not set',
                '#28a745' if obj.project.is_active else '#6c757d',
                'Active' if obj.project.is_active else 'Inactive'
            )
        elif obj.project_details:
            return format_html(
                '<div style="background-color: #fff3cd; padding: 10px; border-radius: 5px; border-left: 4px solid #ffc107;">'
                '<strong>Legacy Project Details:</strong><br/>{}'
                '</div>',
                obj.project_details
            )
        return format_html('<span style="color: #6c757d;">No project associated</span>')
    project_info_display.short_description = _("Project Information")
    
    def has_add_permission(self, request):
        """Disable adding recharge requests from admin (use API instead)."""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Allow viewing but not editing (approve/reject via API)."""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Allow deleting only pending requests."""
        if obj:
            return obj.status == WalletRechargeRequestStatus.PENDING
        return True
    
    def resend_notification_action(self, obj):
        """Display resend notification button for pending requests."""
        if not obj:
            return "-"
        
        if obj.status == WalletRechargeRequestStatus.PENDING:
            resend_url = reverse("admin:users_walletrechargerequest_resend_notification", args=[obj.pk])
            return format_html(
                '<a class="button" href="{}" style="background-color: #007bff; color: white; padding: 5px 10px; text-decoration: none; border-radius: 3px;">Resend Notification</a>',
                resend_url
            )
        return format_html('<span style="color: #999;">—</span>')
    resend_notification_action.short_description = _("Actions")
    
    def get_urls(self):
        """Add custom URLs for resend notification action."""
        from django.urls import path
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:request_id>/resend-notification/",
                self.admin_site.admin_view(self.resend_notification_view),
                name="users_walletrechargerequest_resend_notification",
            ),
        ]
        return custom_urls + urls
    
    def resend_notification_view(self, request, request_id):
        """Handle resend notification action for a recharge request."""
        from django.shortcuts import redirect, get_object_or_404
        from django.contrib import messages
        from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
        
        recharge_request = get_object_or_404(WalletRechargeRequest, pk=request_id)
        
        if recharge_request.status != WalletRechargeRequestStatus.PENDING:
            messages.error(request, "Can only resend notifications for pending recharge requests.")
            return redirect("admin:users_walletrechargerequest_changelist")
        
        try:
            # Resend notification to user
            send_wallet_recharge_request_notifications(recharge_request, "PENDING")
            
            # Resend notification to accounts team
            from django.conf import settings
            from django.core.mail import send_mail
            from django.urls import reverse
            
            accounts_email = getattr(settings, 'ACCOUNTS_EMAIL', 'accounts@iicbooking.iitr.ac.in')
            amount = recharge_request.amount
            
            # Build approve and reject URLs (web forms)
            approve_url = request.build_absolute_uri(
                reverse('users:approve-recharge-request', kwargs={'request_id': recharge_request.id})
            )
            reject_url = request.build_absolute_uri(
                reverse('users:reject-recharge-request', kwargs={'request_id': recharge_request.id})
            )
            
            department_info = ""
            if recharge_request.department:
                dept_code = f" ({recharge_request.department.code})" if recharge_request.department.code else ""
                department_info = f"- Department: {recharge_request.department.name}{dept_code}\n"
            
            subject = f"Wallet Recharge Request - ₹{amount} - {recharge_request.user.email}"
            message = f"""
Wallet Recharge Request (Resent)

You have received a wallet recharge request that requires your approval.

Request Details:
- User: {recharge_request.user.name or recharge_request.user.email}
- Email: {recharge_request.user.email}
- Amount: ₹{amount}
{department_info}- Request ID: #{recharge_request.id}
- Request Date: {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
{f'- Project Details: {recharge_request.project_details}' if recharge_request.project_details else ''}

To approve this request, use the API endpoint: {approve_url}
To reject this request, use the API endpoint: {reject_url}

Note: You will need to provide the OTP code when calling the API endpoints. Please contact the system administrator for the OTP code.

This is an automated email from IIC Booking System.
Please do not reply to this email.
            """.strip()
            
            send_mail(
                subject=subject,
                message=message,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[accounts_email],
                fail_silently=False,
            )
            
            messages.success(
                request,
                f"Notifications resent successfully to user ({recharge_request.user.email}) and accounts team ({accounts_email})."
            )
        except Exception as e:
            messages.error(request, f"Failed to resend notifications: {str(e)}")
        
        return redirect("admin:users_walletrechargerequest_changelist")
    
    @admin.action(description="Resend notifications for selected pending recharge requests")
    def resend_notifications(self, request, queryset):
        """Resend notifications for selected pending recharge requests."""
        from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
        from django.conf import settings
        from django.core.mail import send_mail
        from django.urls import reverse
        
        pending_requests = queryset.filter(status=WalletRechargeRequestStatus.PENDING)
        if not pending_requests.exists():
            self.message_user(request, "No pending recharge requests selected.", level="warning")
            return
        
        accounts_email = getattr(settings, 'ACCOUNTS_EMAIL', 'accounts@iicbooking.iitr.ac.in')
        success_count = 0
        error_count = 0
        
        for recharge_request in pending_requests:
            try:
                # Resend notification to user
                send_wallet_recharge_request_notifications(recharge_request, "PENDING")
                
                # Resend notification to accounts team
                amount = recharge_request.amount
                approve_url = request.build_absolute_uri(
                    reverse('users:approve-recharge-request', kwargs={'request_id': recharge_request.id})
                )
                reject_url = request.build_absolute_uri(
                    reverse('users:reject-recharge-request', kwargs={'request_id': recharge_request.id})
                )
                
                department_info = ""
                if recharge_request.department:
                    dept_code = f" ({recharge_request.department.code})" if recharge_request.department.code else ""
                    department_info = f"- Department: {recharge_request.department.name}{dept_code}\n"
                
                subject = f"Wallet Recharge Request - ₹{amount} - {recharge_request.user.email}"
                message = f"""
Wallet Recharge Request (Resent)

You have received a wallet recharge request that requires your approval.

Request Details:
- User: {recharge_request.user.name or recharge_request.user.email}
- Email: {recharge_request.user.email}
- Amount: ₹{amount}
{department_info}- Request ID: #{recharge_request.id}
- Request Date: {recharge_request.created_at.strftime('%Y-%m-%d %H:%M:%S')}
{f'- Project Details: {recharge_request.project_details}' if recharge_request.project_details else ''}

To approve this request, use the API endpoint: {approve_url}
To reject this request, use the API endpoint: {reject_url}

Note: You will need to provide the OTP code when calling the API endpoints. Please contact the system administrator for the OTP code.

This is an automated email from IIC Booking System.
Please do not reply to this email.
                """.strip()
                
                send_mail(
                    subject=subject,
                    message=message,
                    from_email=settings.DEFAULT_FROM_EMAIL,
                    recipient_list=[accounts_email],
                    fail_silently=False,
                )
                
                success_count += 1
            except Exception as e:
                error_count += 1
                logger.error(f"Failed to resend notification for recharge request {recharge_request.id}: {str(e)}")
        
        if success_count > 0:
            self.message_user(
                request,
                f"Successfully resent notifications for {success_count} recharge request(s).",
            )
        if error_count > 0:
            self.message_user(
                request,
                f"Failed to resend notifications for {error_count} recharge request(s).",
                level="error",
            )


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    """Admin interface for Project model."""

    list_display = [
        "name",
        "project_code",
        "agency",
        "faculty",
        "start_date",
        "end_date",
        "is_active",
        "created_at",
    ]
    list_filter = [
        "is_active",
        "agency",
        "start_date",
        "end_date",
        "created_at",
    ]
    search_fields = [
        "name",
        "project_code",
        "agency",
        "faculty__name",
        "faculty__email",
    ]
    readonly_fields = [
        "created_at",
        "updated_at",
    ]
    fieldsets = (
        (
            _("Project Information"),
            {
                "fields": (
                    "faculty",
                    "name",
                    "project_code",
                    "agency",
                )
            },
        ),
        (
            _("Dates"),
            {
                "fields": (
                    "start_date",
                    "end_date",
                )
            },
        ),
        (
            _("Status"),
            {
                "fields": (
                    "is_active",
                )
            },
        ),
        (
            _("Timestamps"),
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )
    ordering = ["-created_at"]

    def get_queryset(self, request):
        """Optimize queryset with select_related."""
        queryset = super().get_queryset(request)
        return queryset.select_related("faculty")


@admin.register(ExternalUserBankDetails)
class ExternalUserBankDetailsAdmin(admin.ModelAdmin):
    list_display = ("user", "bank_name", "ifsc_code", "masked_account_number_display", "updated_at")
    search_fields = ("user__email", "account_holder_name", "bank_name", "ifsc_code", "account_number", "upi_id")
    ordering = ("-updated_at",)

    def masked_account_number_display(self, obj: ExternalUserBankDetails):
        return obj.masked_account_number()

    masked_account_number_display.short_description = _("Account (masked)")


@admin.register(WalletWithdrawalRequest)
class WalletWithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "amount", "status", "created_at", "responded_at", "completed_at")
    list_filter = ("status", "created_at", "responded_at", "completed_at")
    search_fields = ("user__email", "approved_by_email", "response_message", "utr_reference")
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at", "responded_at", "completed_at")


@admin.register(WalletPeerTransfer)
class WalletPeerTransferAdmin(admin.ModelAdmin):
    list_display = (
        "transaction_id",
        "sender",
        "recipient",
        "amount",
        "grant_code",
        "status",
        "otp_verified",
        "created_at",
        "completed_at",
    )
    list_filter = ("status", "otp_verified", "created_at", "completed_at")
    search_fields = (
        "transaction_id",
        "sender__email",
        "recipient__email",
        "grant_code",
        "remarks",
    )
    ordering = ("-created_at",)
    readonly_fields = (
        "transaction_id",
        "otp_verified",
        "otp_verified_at",
        "sender_balance_after",
        "recipient_balance_after",
        "created_at",
        "updated_at",
        "completed_at",
    )


@admin.register(UserEquipmentSupplyChainRole)
class UserEquipmentSupplyChainRoleAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "role")
    list_filter = ("role",)
    search_fields = ("user__email", "user__name")
    autocomplete_fields = ("user",)
