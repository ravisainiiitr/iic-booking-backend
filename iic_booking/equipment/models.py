import logging
import os
import re
import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import connection, models, transaction
from django.utils.translation import gettext_lazy as _
from django.utils import timezone
from django.utils.text import get_valid_filename
from django.core.files.storage import storages
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.user_group import UserGroup
from iic_booking.users.models.department import Department, DepartmentType, InternalDepartmentSubcategory

logger = logging.getLogger(__name__)


def get_equipment_image_storage():
    """
    Use Django's default storage backend.

    Production: S3 (durable across deploys).
    Local (USE_S3_MEDIA off): filesystem under MEDIA_ROOT.
    """
    return storages["default"]


# Alias used by admin/imports — call get_equipment_image_storage() at use time.
# Kept as a callable so ImageField(storage=...) resolves lazily after settings load.
equipment_image_storage = get_equipment_image_storage


class EquipmentCategory(models.Model):
    """Category for grouping equipment."""
    name = models.CharField(
        max_length=255,
        verbose_name=_('Category Name'),
        help_text=_('Name of the equipment category'),
    )
    code = models.CharField(
        max_length=100,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_('Category Code'),
        help_text=_('Short code for the category'),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Description'),
        help_text=_('Optional description of the category'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Equipment Category')
        verbose_name_plural = _('Equipment Categories')
        ordering = ['name']

    def __str__(self):
        return self.name or self.code or str(self.pk)


class EquipmentGroup(models.Model):
    """Group for equipment with quota configuration at group level."""
    equipment_group_id = models.AutoField(primary_key=True)
    name = models.CharField(
        max_length=255,
        verbose_name=_('Group Name'),
        help_text=_('Name of the equipment group'),
    )
    code = models.CharField(
        max_length=255,
        unique=True,
        verbose_name=_('Group Code'),
        help_text=_('Unique code for the equipment group'),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Description'),
        help_text=_('Optional description of the equipment group'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Equipment Group')
        verbose_name_plural = _('Equipment Groups')
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.code})"

class EquipmentStatus(models.TextChoices):
    """
    Equipment lifecycle status (drives booking availability + UI).

    IMPORTANT: We keep legacy stored values for backward compatibility with existing DB rows
    and API consumers, but update the user-facing labels to the 3 required states.
    """

    # User-required states (stored as legacy values to avoid a data migration):
    # - Operational => ACTIVE
    # - Maintenance Scheduled => MAINTENANCE
    # - Under Maintenance => REPAIR
    ACTIVE = 'ACTIVE', _('Operational')
    MAINTENANCE = 'MAINTENANCE', _('Maintenance Scheduled')
    REPAIR = 'REPAIR', _('Under Maintenance')

    # Legacy/extra states. These are treated as non-operational for booking purposes.
    INACTIVE = 'INACTIVE', _('Under Maintenance')
    DISPOSED = 'DISPOSED', _('Disposed')
    OTHER = 'OTHER', _('Other')
    
class EquipmentProfileType(models.TextChoices):
    """Charge profile types."""
    SAMPLE = 'SAMPLE', _('Sample-based')
    HOUR = 'HOUR', _('Hour-based')
    SAMPLE_ELEMENT = 'SAMPLE_ELEMENT', _('Sample + Element')
    MULTI_PARAM = 'MULTI_PARAM', _('Multi-parameter')
    PRINT_3D = 'PRINT_3D', _('3D Print')

# Alias for backward compatibility with calculators
ChargeProfileType = EquipmentProfileType


def equipment_image_upload_to(instance, filename):
    """Store equipment photos under S3 media prefix: media/equipment_images/..."""
    ext = os.path.splitext(filename)[1].lower() or ".jpg"
    if ext and not ext.startswith("."):
        ext = "." + ext
    safe_code = get_valid_filename((instance.code or "equipment")[:100]) or "equipment"
    eid = instance.pk or "new"
    unique = uuid.uuid4().hex[:12]
    return f"equipment_images/equipment_{eid}_{safe_code}_{unique}{ext}"


class Equipment(models.Model):
    equipment_id = models.AutoField(primary_key=True)
    name = models.CharField(max_length=255, help_text='Name of the equipment')
    code = models.CharField(max_length=255, help_text='Code of the equipment', unique=True)
    description = models.TextField(help_text='Description of the equipment', blank=True, null=True)
    status = models.CharField(max_length=255, help_text='Status of the equipment', blank=True, null=True, choices=EquipmentStatus.choices)
    location = models.TextField(help_text='Location of the equipment', blank=True, null=True)
    image = models.ImageField(
        storage=get_equipment_image_storage,
        upload_to=equipment_image_upload_to,
        max_length=512,
        blank=True,
        null=True,
        verbose_name=_("Equipment image"),
        help_text=_(
            "Photo shown when booking. Stored via default media storage "
            "(S3 in production) so images survive deploys."
        ),
    )
    video_file = models.FileField(
        upload_to='equipment_videos/%Y/%m/%d/',
        help_text='Video file for the equipment',
        blank=True,
        null=True,
        verbose_name=_('Equipment Video'),
    )

    slot_duration_minutes = models.IntegerField(help_text='Duration of the slot in minutes', default=30)
    slots_per_day = models.IntegerField(help_text='Number of slots per day', default=12)

    internal_weekly_quota = models.IntegerField(help_text='Internal weekly quota', default=10)
    external_weekly_quota = models.IntegerField(help_text='External weekly quota', default=10)
    internal_monthly_quota = models.IntegerField(help_text='Internal monthly quota', default=10)
    external_monthly_quota = models.IntegerField(help_text='External monthly quota', default=10)

    skip_quota_check = models.BooleanField(
        default=False,
        help_text=_(
            "When enabled, weekly/monthly quota checks are skipped for this equipment only "
            "(booking, reschedule, and waitlist auto-book). Global SKIP_BOOKING_QUOTA_CHECK still applies site-wide."
        ),
        verbose_name=_("Skip quota check for this equipment"),
    )

    reschedule_hours_threshold = models.IntegerField(
        help_text='Hours before booking start time when reschedule option becomes unavailable (default: 48)',
        default=48,
        verbose_name=_('Reschedule Hours Threshold')
    )
    results_base_location = models.CharField(
        max_length=500,
        default=r'D:\Results',
        help_text=_('Base folder where sample-analysis folders are created when Sample Lifecycle moves to "In Analysis".'),
        verbose_name=_('Results Base Location'),
    )

    repeat_sample_request_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_('Number of days after booking completion within which user can request a repeat sample. Leave empty to disable.'),
        verbose_name=_('Repeat sample request window (days)')
    )
    repeat_sample_disclaimer = models.TextField(
        blank=True,
        default='',
        help_text=_('Disclaimer text shown to user in a popup when they request to repeat the sample.'),
        verbose_name=_('Repeat sample disclaimer')
    )

    split_booking_enabled = models.BooleanField(
        default=False,
        help_text=_('If enabled, users can select non-consecutive slots, but only when continuous slots are not available'),
        verbose_name=_('Split Booking Enabled')
    )

    auto_slot_selection_default = models.BooleanField(
        null=True,
        blank=True,
        default=None,
        help_text=_(
            "Default state of the 'Auto Slot Selection' toggle on the booking page for this equipment. "
            "When unset, the user's preference is used."
        ),
        verbose_name=_('Auto Slot Selection default (override)'),
    )

    enable_charge_recalculation = models.BooleanField(
        default=False,
        help_text=_(
            'When checked, the user can edit any input field on a confirmed (BOOKED) booking before completion, '
            'regardless of "editing required" status. On save, charges are recalculated; if the new charge is higher '
            'than the amount already deducted, the difference is debited from the wallet; if lower, the difference '
            'is credited. An email notification is sent to the user.'
        ),
        verbose_name=_('Enable charge recalculation'),
    )

    user_rating_enabled = models.BooleanField(
        default=True,
        help_text=_('When unchecked, users cannot submit a star rating or feedback for completed bookings of this equipment. Only admin and OIC can change this setting.'),
        verbose_name=_('User rating enabled'),
    )

    sample_preparation_by_user = models.BooleanField(
        default=False,
        help_text=_(
            'When enabled, internal users (student / individual student / faculty) receive sample preparation '
            'guidance in booking confirmation and reminder emails. External user emails are unchanged.'
        ),
        verbose_name=_('Sample preparation by user (notify internal users in email)'),
    )

    booking_email_extra_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Optional plain text appended after the standard booking message in booking confirmation emails "
            "(and in same-day reminder emails) for this equipment. Leave empty for no extra text."
        ),
        verbose_name=_("Extra text for booking emails (plain text)"),
    )

    istem_portal_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text=_(
            "Optional hyperlink to this equipment's booking page on the I-STEM portal. "
            "Shown to booking users when I-STEM FBR is required for their charge profile."
        ),
        verbose_name=_("I-STEM portal URL"),
    )

    istem_fbr_status_url = models.URLField(
        max_length=500,
        blank=True,
        default="",
        help_text=_(
            "Optional hyperlink for Officer In-charge / Admins to verify FBR status on I-STEM "
            "(separate from the user booking page URL)."
        ),
        verbose_name=_("I-STEM FBR status check URL"),
    )

    completion_email_extra_text = models.TextField(
        blank=True,
        default="",
        help_text=_(
            "Optional text appended to booking completion emails for this equipment. "
            "Plain text; URLs (http/https) are turned into clickable links in the HTML email."
        ),
        verbose_name=_("Extra text for completion emails"),
    )

    print_3d_stl_notification_email = models.EmailField(
        blank=True,
        default="",
        help_text=_(
            "For 3D printing equipment only: when a booking is confirmed, the user's STL file(s) "
            "and booking details are emailed to this address. Leave blank to disable."
        ),
        verbose_name=_("3D print STL notification email"),
    )

    make = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Make"),
        help_text=_('Manufacturer or make (e.g. "Zeiss", "Thermo Fisher"). Shown on equipment catalog cards when enabled.'),
    )
    show_make_on_card = models.BooleanField(
        default=False,
        verbose_name=_("Show Make on equipment card"),
        help_text=_("When enabled, Make is displayed on catalog cards below department information."),
    )
    model_information = models.CharField(
        max_length=255,
        blank=True,
        default="",
        verbose_name=_("Model"),
        help_text=_('Model information (e.g. "Sigma 300", "FE-SEM"). Shown on equipment catalog cards when enabled.'),
    )
    show_model_on_card = models.BooleanField(
        default=False,
        verbose_name=_("Show Model on equipment card"),
        help_text=_("When enabled, Model is displayed on catalog cards below Make / department information."),
    )

    class WeeklyViewDisplay(models.TextChoices):
        """Vertical axis of the weekly calendar: show time or hide time."""
        TIME = 'TIME', _('Show time')
        SLOT_ID = 'SLOT_ID', _('Hide time')

    weekly_view_display = models.CharField(
        max_length=20,
        choices=WeeklyViewDisplay.choices,
        default=WeeklyViewDisplay.TIME,
        help_text=_(
            'Weekly view vertical axis: Show time = time on axis; Hide time = time hidden (display only). '
            'Only admin and OIC can change this.'
        ),
        verbose_name=_('Weekly view display'),
    )

    weekly_view_time_from = models.TimeField(
        null=True,
        blank=True,
        verbose_name=_('Slot window time from'),
        help_text=_('Slot window start (24h). Only slots with start time at or after this time are shown and bookable. Leave empty for no limit.'),
    )
    weekly_view_time_to = models.TimeField(
        null=True,
        blank=True,
        verbose_name=_('Slot window time to'),
        help_text=_('Slot window end (24h). Only slots with end time at or before this time are shown and bookable. Leave empty for no limit.'),
    )
    weekly_view_max_rows = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Weekly view max rows'),
        help_text=_('Maximum number of time/slot rows to show in the weekly view. Leave empty for no limit. Admin and OIC can edit.'),
    )
    weekly_view_default_days = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        default=7,
        verbose_name=_('Weekly view default days'),
        help_text=_('Default number of days to show in the weekly view (e.g. 7 for one week). Admin and OIC can edit.'),
    )

    # Slot window: when the next week becomes visible to internal users (e.g. Wed 21:00).
    # 0=Monday, 6=Sunday. Before this day+time only current week is visible; on or after it, current + next week.
    slot_window_reference_weekday = models.SmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Slot window reference weekday'),
        help_text=_('Weekday (0=Monday … 6=Sunday) at which the next week becomes visible. Leave empty for no restriction.'),
        validators=[MinValueValidator(0), MaxValueValidator(6)],
    )
    slot_window_reference_time = models.TimeField(
        null=True,
        blank=True,
        verbose_name=_('Slot window reference time'),
        help_text=_('Time (24h) on that weekday when the next week opens. Used with slot window reference weekday.'),
    )

    urgent_peak_window_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Urgent booking peak window (minutes)'),
        help_text=_(
            'For "Unable to get slot despite repeated trials", only failed attempts within this many minutes '
            'after the slot window (internal users) time on the reference weekday are shown in the log. '
            'Configurable by Admin and OIC. Leave empty to show all non-quota failures in the past 2 weeks.'
        ),
    )

    max_urgent_requests = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Max urgent requests'),
        help_text=_(
            'Maximum number of PENDING urgent requests allowed for this equipment at a time. '
            'Configurable by Admin and OIC. Leave empty for no cap.'
        ),
    )

    waitlist_queue_depth = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=0,
        help_text=_(
            'Maximum number of users in the waitlist for this equipment. '
            'When a booking attempt fails, the user is added to the waitlist and notified of their position. '
            'Set to 0 or leave empty to disable waitlist.'
        ),
        verbose_name=_('Waitlist queue depth'),
    )
    booking_not_utilize_window_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_('Booking Not Utilize Window (hours)'),
        help_text=_(
            'Hours after the last slot end before staff may mark "Booking Not Utilized" (no refund) '
            'when the user did not attend or samples were not submitted, and sample lifecycle has no '
            'update or only "Sample Sent". Set to 0 to hide/disable this action for this equipment.'
        ),
    )
    operator_unavailable_after_booking_end_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_('Auto Operator Unavailable (hours after booking end)'),
        help_text=_(
            'After the last slot end, if the booking is still open and sample lifecycle shows staff '
            'activity beyond "Sample Sent" but the run is not finished (no completed/returned/archived/'
            'disposed lifecycle), the system automatically marks Operator Unavailable (full refund) once '
            'this many hours have passed. Set to 0 to disable. User no-show / sample-not-submitted cases '
            'use manual Booking Not Utilized instead.'
        ),
    )
    operator_absent_disruption_after_booking_end_hours = models.PositiveIntegerField(
        default=48,
        verbose_name=_('Auto Operator Absent Disruption (hours after booking end)'),
        help_text=_(
            'After the last slot end time, if the booking is still PENDING or BOOKED and the sample lifecycle '
            'status remains stuck at "Sample Accepted" or "Processing" for this many hours, the system treats it '
            'as an Operator Absent disruption and triggers the usual disruption flow (refund vs reschedule choice). '
            'Set to 0 to disable.'
        ),
    )
    show_lifecycle_countdowns = models.BooleanField(
        default=True,
        verbose_name=_('Show sample lifecycle countdowns'),
        help_text=_(
            'When enabled, booking details show live countdowns: time to submit sample (before Sample Accepted), '
            'booking time remaining (after Sample Accepted until slot end), and time to collect sample '
            '(after booking completed until discard deadline).'
        ),
    )
    sample_submission_lead_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_('Sample submission lead time (hours before slot start)'),
        help_text=_(
            'Users must submit samples this many hours before the booked slot starts. '
            'Atmosphere-sensitive bookings may submit up to slot start instead. Set to 0 to use slot start as the deadline.'
        ),
    )
    sample_collect_deadline_hours = models.PositiveIntegerField(
        default=72,
        verbose_name=_('Sample collect / discard deadline (hours after completion)'),
        help_text=_(
            'After the booking is completed, users have this many hours to collect the sample before the lab may discard it. '
            'Set to 0 to hide the collect-sample countdown.'
        ),
    )

    profile_type = models.CharField(
        max_length=20,
        choices=EquipmentProfileType.choices,
        help_text=_('Type of equipment profile'),
        null=True,
        blank=True,
    )

    category = models.ForeignKey(
        EquipmentCategory,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='equipment',
        verbose_name=_('Category'),
        help_text=_('Category this equipment belongs to'),
    )
    internal_department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='equipment',
        verbose_name=_('Internal Department'),
        help_text=_('Internal department this equipment is mapped to'),
        limit_choices_to={'department_type': DepartmentType.INTERNAL},
    )
    visibility_group = models.ForeignKey(
        UserGroup,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='equipment',
        verbose_name=_('Visibility Group'),
        help_text=_('If set, only members of this group can see this equipment. Leave empty for public visibility.'),
    )
    equipment_group = models.ForeignKey(
        'EquipmentGroup',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='equipment',
        verbose_name=_('Equipment Group'),
        help_text=_('Equipment group this equipment belongs to. Quota configuration is applied at group level.'),
    )
    enable_multi_mode = models.BooleanField(
        default=False,
        verbose_name=_('Enable Multi-Mode Equipment'),
        help_text=_(
            'When enabled, this base instrument can have alternate operating modes (child equipment) '
            'and date-based mode schedules. Default is off; equipment behaves as a standard instrument.'
        ),
    )
    parent_equipment = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='mode_children',
        verbose_name=_('Parent Equipment (multi-mode)'),
        help_text=_(
            'When set, this equipment is an alternate operating mode of the parent (base) instrument. '
            'The parent must have Multi-Mode Equipment enabled. '
            'Leave empty for standalone equipment or for the base/parent mode itself.'
        ),
    )

    important_instruction = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Important Instruction'),
        help_text=_('Important instructions shown prominently on the equipment page (above specifications).'),
    )

    # -------------------------------------------------------------------------
    # Asset lifecycle (procurement entry, warranty, supplier) — optional fields
    # -------------------------------------------------------------------------
    supplier_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Supplier name'))
    supplier_contact = models.TextField(blank=True, default='', verbose_name=_('Supplier contact'))
    purchase_order_ref = models.CharField(max_length=120, blank=True, default='', verbose_name=_('Purchase order reference'))
    purchase_invoice_ref = models.CharField(max_length=120, blank=True, default='', verbose_name=_('Purchase invoice reference'))
    purchase_date = models.DateField(null=True, blank=True, verbose_name=_('Purchase date'))
    warranty_start = models.DateField(null=True, blank=True, verbose_name=_('Warranty start'))
    warranty_end = models.DateField(null=True, blank=True, verbose_name=_('Warranty end'))
    commissioning_date = models.DateField(null=True, blank=True, verbose_name=_('Commissioning date'))
    asset_serial_number = models.CharField(max_length=120, blank=True, default='', verbose_name=_('Asset / manufacturer serial'))
    lifecycle_notes = models.TextField(blank=True, default='', verbose_name=_('Lifecycle notes'))

    created_at = models.DateTimeField(auto_now_add=True, help_text='Date and time the equipment was created')
    updated_at = models.DateTimeField(auto_now=True, help_text='Date and time the equipment was updated')

    def __str__(self):
        return self.code

    def clean(self):
        super().clean()
        parent = self.parent_equipment
        if parent is None:
            return
        if self.pk and parent.pk == self.pk:
            raise ValidationError({'parent_equipment': _('Equipment cannot be its own parent.')})
        if parent.parent_equipment_id:
            raise ValidationError({
                'parent_equipment': _('Parent must be a base instrument (it cannot itself be a child mode).'),
            })
        if self.pk and self.mode_children.exists():
            raise ValidationError({
                'parent_equipment': _('This equipment already has child modes; it cannot become a child of another parent.'),
            })

    class Meta:
        verbose_name = 'Equipment'
        verbose_name_plural = 'Equipment'
        ordering = ['name']


class ModeScheduleBehavior(models.TextChoices):
    """How an alternate mode interacts with the base/parent mode on a date range."""
    PARALLEL = 'PARALLEL', _('Parallel')
    EXCLUSIVE = 'EXCLUSIVE', _('Mutually Exclusive')


class EquipmentModeSchedule(models.Model):
    """
    Date-ranged activation of a child mode under a multi-mode parent instrument.

    PARALLEL: child is bookable alongside the parent (no cross-mode time conflict).
    EXCLUSIVE: child replaces the parent in the catalog for those dates; family shares
    the physical instrument for availability/conflict checks.
    """
    id = models.AutoField(primary_key=True)
    parent_equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='mode_schedules',
        verbose_name=_('Parent Equipment'),
        help_text=_('Base/parent instrument this schedule applies to.'),
    )
    mode_equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='as_mode_schedules',
        verbose_name=_('Mode Equipment'),
        help_text=_('Child mode equipment being enabled for the date range.'),
    )
    start_date = models.DateField(verbose_name=_('Start Date'))
    end_date = models.DateField(verbose_name=_('End Date'))
    start_time = models.TimeField(
        blank=True,
        null=True,
        verbose_name=_('Start Time'),
        help_text=_('Optional. If set with end time, only slots within this daily window are mode-active.'),
    )
    end_time = models.TimeField(
        blank=True,
        null=True,
        verbose_name=_('End Time'),
        help_text=_('Optional. If set with start time, only slots within this daily window are mode-active.'),
    )
    behavior = models.CharField(
        max_length=20,
        choices=ModeScheduleBehavior.choices,
        default=ModeScheduleBehavior.PARALLEL,
        verbose_name=_('Behavior'),
    )
    # Child outside active schedule window
    unavailable_label = models.CharField(
        max_length=120,
        blank=True,
        default='Mode not scheduled',
        verbose_name=_('Unavailable Status Label'),
        help_text=_('Shown on child mode slots outside the configured schedule window.'),
    )
    unavailable_color = models.CharField(
        max_length=20,
        blank=True,
        default='#9ca3af',
        verbose_name=_('Unavailable Background Color'),
        help_text=_('Background color for child slots outside the schedule (default grey).'),
    )
    # Parent during mutually exclusive child window
    exclusive_blocked_label = models.CharField(
        max_length=120,
        blank=True,
        default='Alternate mode active',
        verbose_name=_('Blocked Slot Label (exclusive)'),
        help_text=_('Shown on parent/base slots while a mutually exclusive child mode is active.'),
    )
    exclusive_blocked_color = models.CharField(
        max_length=20,
        blank=True,
        default='#9ca3af',
        verbose_name=_('Blocked Background Color (exclusive)'),
        help_text=_('Background color for parent slots during exclusive mode (default grey).'),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='created_mode_schedules',
        verbose_name=_('Created By'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Equipment Mode Schedule')
        verbose_name_plural = _('Equipment Mode Schedules')
        ordering = ['-start_date', '-end_date', 'id']
        indexes = [
            models.Index(fields=['parent_equipment', 'start_date', 'end_date']),
            models.Index(fields=['mode_equipment', 'start_date', 'end_date']),
        ]

    def __str__(self):
        return (
            f"{self.mode_equipment_id} @ {self.parent_equipment_id} "
            f"{self.start_date}–{self.end_date} ({self.behavior})"
        )

    def clean(self):
        super().clean()
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError({'end_date': _('End date must be on or after start date.')})
        if self.start_time and self.end_time and self.start_time > self.end_time:
            raise ValidationError({'end_time': _('End time must be on or after start time.')})
        parent = self.parent_equipment
        mode = self.mode_equipment
        if parent is None or mode is None:
            return
        if parent.parent_equipment_id:
            raise ValidationError({
                'parent_equipment': _('Parent must be a base instrument (no parent_equipment set).'),
            })
        if mode.pk == parent.pk:
            raise ValidationError({'mode_equipment': _('Mode cannot be the same as the parent.')})
        if mode.parent_equipment_id != parent.pk:
            raise ValidationError({
                'mode_equipment': _('Mode equipment must be linked as a child of the parent first.'),
            })
        # No overlapping EXCLUSIVE schedules for the same parent on overlapping dates
        if self.behavior == ModeScheduleBehavior.EXCLUSIVE and self.start_date and self.end_date:
            qs = EquipmentModeSchedule.objects.filter(
                parent_equipment_id=parent.pk,
                behavior=ModeScheduleBehavior.EXCLUSIVE,
                start_date__lte=self.end_date,
                end_date__gte=self.start_date,
            )
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({
                    'behavior': _(
                        'Another mutually exclusive schedule already covers part of this date range '
                        'for this parent instrument.'
                    ),
                })


class EquipmentUserGroupPurpose(models.TextChoices):
    """Purpose of an equipment-linked user group."""
    BOOKING_REQUESTERS = 'BOOKING_REQUESTERS', _('Booking Requesters')


class EquipmentUserGroup(models.Model):
    """
    Link an Equipment to a UserGroup for a specific purpose.

    Used to maintain per-equipment user groups (e.g. all users who requested a booking).
    """
    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='user_groups',
        verbose_name=_('Equipment'),
    )
    purpose = models.CharField(
        max_length=50,
        choices=EquipmentUserGroupPurpose.choices,
        default=EquipmentUserGroupPurpose.BOOKING_REQUESTERS,
        verbose_name=_('Purpose'),
    )
    user_group = models.ForeignKey(
        UserGroup,
        on_delete=models.PROTECT,
        related_name='equipment_links',
        verbose_name=_('User Group'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Equipment user group')
        verbose_name_plural = _('Equipment user groups')
        unique_together = [['equipment', 'purpose']]
        indexes = [
            models.Index(fields=['equipment', 'purpose']),
        ]

    def __str__(self):
        return f"{self.equipment.code} - {self.purpose} - {self.user_group.code}"

class EquipmentManager(models.Model):
    equipment_manager_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_managers')
    manager = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='equipment_manager',
        verbose_name=_('Officer in Charge'),
        help_text=_('Officer in Charge for this equipment'),
        limit_choices_to={'user_type': UserType.MANAGER},
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text=_('Date and time this assignment was created'))
    updated_at = models.DateTimeField(auto_now=True, help_text=_('Date and time this assignment was last updated'))

    class Meta:
        verbose_name = _('Officer in Charge')
        verbose_name_plural = _('Equipment Office in Charge')

    def __str__(self):
        return f"{self.equipment.code} - {self.manager.name or self.manager.email}"

class EquipmentOperator(models.Model):
    class Role(models.TextChoices):
        PRIMARY = "PRIMARY", _("Primary operator")
        SECONDARY = "SECONDARY", _("Secondary operator")

    equipment_operator_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_operators')
    operator = models.ForeignKey(User, on_delete=models.PROTECT, related_name='equipment_operator', help_text='Operator of the equipment', limit_choices_to={'user_type': UserType.OPERATOR})
    role = models.CharField(
        max_length=16,
        choices=Role.choices,
        default=Role.PRIMARY,
        help_text=_("Operator role for this instrument (primary or secondary)."),
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text='Date and time the equipment operator was created')
    updated_at = models.DateTimeField(auto_now=True, help_text='Date and time the equipment operator was updated')

    def __str__(self):
        r = (self.role or "").lower()
        return f"{self.equipment.code} - {self.operator.name or self.operator.email} ({r})"

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["equipment", "role"],
                name="uniq_equipment_operator_role",
            ),
        ]


class EquipmentTemporaryOIC(models.Model):
    """
    Temporary OIC delegation: when the primary OIC goes on leave, they can assign
    another OIC-type user to manage the equipment until resume_at. After resume_at,
    the temporary OIC loses management access and the primary OIC resumes.
    """
    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='temporary_oic_delegations')
    primary_oic = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='temporary_oic_delegations_as_primary',
        limit_choices_to={'user_type': UserType.MANAGER},
        help_text='The OIC who is on leave and has delegated management.',
    )
    temporary_oic = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='temporary_oic_delegations_as_temp',
        limit_choices_to={'user_type': UserType.MANAGER},
        help_text='The OIC who will temporarily manage the equipment until resume_at.',
    )
    resume_at = models.DateTimeField(help_text='Date and time after which the primary OIC resumes and the temporary OIC loses access.')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Temporary OIC delegation')
        verbose_name_plural = _('Temporary OIC delegations')
        ordering = ['resume_at']

    def __str__(self):
        return f"{self.equipment.code}: {self.temporary_oic} until {self.resume_at}"


class EquipmentOperatorCoverage(models.Model):
    """
    Time-bounded operator coverage per equipment during an approved leave window.

    This model does NOT replace the canonical primary/secondary operator assignments
    in EquipmentOperator (which are unique per equipment+role). Instead, it temporarily
    alters equipment visibility and operational responsibility for a specific period.
    """

    class Mode(models.TextChoices):
        SECONDARY_OPERATOR = "SECONDARY_OPERATOR", _("Secondary operator covers")
        OIC_SELF_OPERATE = "OIC_SELF_OPERATE", _("OIC self-operates")
        OPERATOR_ON_LEAVE = "OPERATOR_ON_LEAVE", _("Operator on leave (disruption policy)")

    id = models.BigAutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="operator_coverages",
    )
    primary_operator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="primary_operator_coverages",
        limit_choices_to={"user_type": UserType.OPERATOR},
    )
    acting_operator = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="acting_operator_coverages",
        limit_choices_to={"user_type": UserType.OPERATOR},
        help_text=_("Operator who will temporarily handle this equipment (for SECONDARY_OPERATOR mode)."),
    )
    mode = models.CharField(max_length=32, choices=Mode.choices)
    starts_at = models.DateTimeField(db_index=True)
    ends_at = models.DateTimeField(db_index=True)
    ended_early_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_early_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_coverages_ended",
    )
    source_leave_request = models.ForeignKey(
        "equipment.OperatorLeaveRequest",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_coverages",
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_coverages_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-starts_at"]
        indexes = [
            models.Index(fields=["equipment", "starts_at", "ends_at"]),
            models.Index(fields=["acting_operator", "starts_at", "ends_at"]),
            models.Index(fields=["primary_operator", "starts_at", "ends_at"]),
            models.Index(fields=["mode", "starts_at", "ends_at"]),
        ]

    def is_active(self, at_time=None) -> bool:
        check_time = at_time or timezone.now()
        if self.ended_early_at and self.ended_early_at <= check_time:
            return False
        return self.starts_at <= check_time <= self.ends_at


class ICPMSStandardSample(models.Model):
    """ICPMS Standard Sample Database (seeded from icpms_standards.pdf)."""

    id = models.BigAutoField(primary_key=True)
    s_no = models.CharField(max_length=50, verbose_name="S.NO.")
    part_no = models.CharField(max_length=100, verbose_name="Part No.", blank=True, default="")
    name_of_std = models.CharField(max_length=255, verbose_name="Name of Std")
    list_of_elements = models.TextField(verbose_name="List of Element", blank=True, default="")
    concentration = models.CharField(max_length=100, verbose_name="Concentration", blank=True, default="")
    status = models.PositiveSmallIntegerField(verbose_name="Status", default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "ICPMS Standard Sample"
        verbose_name_plural = "ICPMS Standard Sample Database"
        ordering = ["id"]
        db_table = "icpms_standard_sample_database"

    def __str__(self) -> str:
        return f"{self.s_no} - {self.name_of_std}"

class EquipmentSpecification(models.Model):
    equipment_specification_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_specifications')
    spec_key = models.CharField(max_length=255, help_text='Key of the specification')
    spec_value = models.TextField(help_text='Value of the specification')
    created_at = models.DateTimeField(auto_now_add=True, help_text='Date and time the equipment specification was created')

    def __str__(self):
        return f"{self.equipment.code} - {self.spec_key}"
    
class EquipmentAccessory(models.Model):
    equipment_accessory_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_accessories')
    accessory_name = models.CharField(max_length=255, help_text='Name of the accessory')
    is_optional = models.BooleanField(default=False, help_text='Whether this accessory is optional')
    is_enabled = models.BooleanField(
        default=True,
        verbose_name=_('Enabled'),
        help_text=_(
            'When disabled, this accessory is shown as Unavailable on booking and equipment views. '
            'OIC can toggle this.'
        ),
    )
    quantity = models.PositiveIntegerField(default=1, help_text=_('Quantity supplied with the equipment'))
    serial_number = models.CharField(max_length=120, blank=True, default='', verbose_name=_('Serial / tag'))
    notes = models.TextField(blank=True, default='', verbose_name=_('Notes'))
    created_at = models.DateTimeField(auto_now_add=True, help_text='Date and time the equipment accessory was created')

    class Meta:
        verbose_name = _('Equipment accessory')
        verbose_name_plural = _('Equipment accessories')

    def __str__(self):
        return f"{self.equipment.code} - {self.accessory_name} - {'Optional' if self.is_optional else 'Required'}"

class EquipmentAdditionalAccessory(models.Model):
    equipment_additional_accessory_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_additional_accessories')
    additional_accessory_name = models.CharField(max_length=255, help_text='Name of the additional accessory')
    additional_accessory_description = models.TextField(help_text='Description of the additional accessory', blank=True, null=True)
    is_optional = models.BooleanField(default=False, help_text='Whether this additional accessory is optional')
    is_enabled = models.BooleanField(
        default=True,
        verbose_name=_('Enabled'),
        help_text=_(
            'When disabled, this additional accessory is shown as Unavailable on booking and equipment views. '
            'OIC can toggle this.'
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True, help_text='Date and time the equipment additional accessory was created')

    class Meta:
        verbose_name = _('Equipment additional accessory')
        verbose_name_plural = _('Equipment additional accessories')

    def __str__(self):
        return f"{self.equipment.code} - {self.additional_accessory_name} - {'Optional' if self.is_optional else 'Required'}"

# ============================================================================
# Equipment Settings
# ============================================================================
class ChargeProfilePricingProfile(models.TextChoices):
    STANDARD = "standard", _("Standard Charge Profile")
    DISCOUNTED = "discounted", _("Discounted Charge Profile")


class ChargeProfile(models.Model):
    """Charge profile defining pricing rules per equipment and user type."""
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='charge_profiles',
        help_text=_('Equipment this profile applies to')
    )
    user_type = models.CharField(
        max_length=50,
        help_text=_('User type this profile applies to')
    )

    pricing_profile = models.CharField(
        max_length=20,
        choices=ChargeProfilePricingProfile.choices,
        default=ChargeProfilePricingProfile.STANDARD,
        db_index=True,
        help_text=_("Pricing variant (discounted profiles return zero charges)."),
    )
    
    is_active = models.BooleanField(
        default=True,
        help_text=_('Whether this profile is active')
    )

    require_istem_fbr = models.BooleanField(
        default=False,
        help_text=_(
            "When enabled, bookings using this charge profile must enter an I-STEM FBR number "
            "and have it verified by the Officer in Charge before results are released."
        ),
        verbose_name=_("Require I-STEM FBR"),
    )

    show_charge_breakdown = models.BooleanField(
        default=True,
        help_text=_(
            "When enabled, the itemized charge breakdown is shown in the Charge Calculation "
            "section during booking / estimate. When disabled, only totals are shown."
        ),
        verbose_name=_("Show charge breakdown"),
    )
    
    # Primary charge unit (e.g., per sample, per hour)
    primary_unit_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_('Primary unit charge amount')
    )
    
    # Secondary charge (for breakpoint-based pricing)
    secondary_unit_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal('0.00'),
        help_text=_('Secondary unit charge amount')
    )
    
    # Breakpoint for tiered pricing
    breakpoint = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        blank=True,
        null=True,
        help_text=_('Breakpoint value for tiered pricing')
    )
    
    # Time calculation formula (for SAMPLE and SAMPLE_ELEMENT)
    time_formula = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text=_('Formula for time calculation (e.g., "(A * C) + B")')
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Charge Profile')
        verbose_name_plural = _('Charge Profiles')
        unique_together = [['equipment', 'user_type', 'pricing_profile']]
        ordering = ['equipment', 'user_type', 'pricing_profile']
    
    def __str__(self):
        profile_type_display = self.equipment.get_profile_type_display() if self.equipment else ""
        return f"{self.equipment.code if self.equipment else ''} - {self.user_type} - {self.pricing_profile} - {profile_type_display}"


class UserDiscountedChargeEquipment(models.Model):
    """
    Per-user equipment scope for Discounted Charge Profile.

    If a user has `use_discounted_charge_profile=True` and has NO rows in this table,
    we treat it as "apply discounted to all equipment" (backwards compatible).
    """

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="discounted_charge_equipment",
        verbose_name=_("User"),
    )
    equipment = models.ForeignKey(
        "Equipment",
        on_delete=models.CASCADE,
        related_name="discounted_charge_users",
        verbose_name=_("Equipment"),
    )
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("User Discounted Charge Equipment")
        verbose_name_plural = _("User Discounted Charge Equipment")
        unique_together = [("user", "equipment")]
        ordering = ["user_id", "equipment_id"]

    def __str__(self):
        return f"{self.user_id} -> {self.equipment_id} (discounted={self.is_active})"


class DynamicInputFieldType(models.TextChoices):
    """Types of dynamic input fields."""
    NUMERIC = 'NUMERIC', _('Numeric')
    TEXT = 'TEXT', _('Text')
    RADIO = 'RADIO', _('Radio')
    COMBO = 'COMBO', _('Combo/Dropdown')
    MULTI_SELECT = 'MULTI_SELECT', _('Multi-select')
    TOGGLE = 'TOGGLE', _('Toggle')
    PERIODIC_TABLE = 'PERIODIC_TABLE', _('Periodic table / Element selector')
    TABLE = 'TABLE', _('Table')
    ICPMS_STANDARD_COVERAGE = 'ICPMS_STANDARD_COVERAGE', _('ICPMS Standard Coverage')

class DynamicInputField(models.Model):
    """Dynamic input fields (A-G) for charge profiles."""
    
    FIELD_KEYS = [
        ('A', 'A'),
        ('B', 'B'),
        ('C', 'C'),
        ('D', 'D'),
        ('E', 'E'),
        ('F', 'F'),
        ('G', 'G'),
        ('H', 'H'),
        ('I', 'I'),
        ('J', 'J'),
        ('K', 'K'),
        ('L', 'L'),
        ('M', 'M'),
        ('N', 'N'),
        ('O', 'O'),
        ('P', 'P'),
        ('Q', 'Q'),
        ('R', 'R'),
        ('S', 'S'),
        ('T', 'T'),
        ('U', 'U'),
        ('V', 'V'),
        ('W', 'W'),
        ('X', 'X'),
        ('Y', 'Y'),
        ('Z', 'Z'),
    ]
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='input_fields',
        help_text=_('Equipment this field belongs to'),
        null=True,
        blank=True,
    )
    field_key = models.CharField(
        max_length=1,
        choices=FIELD_KEYS,
        help_text=_('Field key (A-G)')
    )
    field_label = models.CharField(
        max_length=255,
        help_text=_('Label for this field')
    )
    field_type = models.CharField(
        max_length=32,
        choices=DynamicInputFieldType.choices,
        help_text=_('Type of input field')
    )
    is_required = models.BooleanField(
        default=False,
        help_text=_('Whether this field is required')
    )
    default_value = models.CharField(
        max_length=500,
        blank=True,
        null=True,
        help_text=_('Default value for this field')
    )
    options = models.JSONField(
        blank=True,
        null=True,
        default=list,
        help_text=_('Options for radio/combo/multi-select fields')
    )
    help_text = models.TextField(
        blank=True,
        null=True,
        help_text=_(
            'Help text for this field. '
            'NUMERIC: line 1 = lower limit, line 2 = upper limit, line 3 = step '
            '(e.g. 0.01). Defaults 0 / 100 / 1 when blank. '
            'PERIODIC_TABLE: one element per line to disable (e.g. Fe); '
            'prefix with / to lock-preselect without charge (e.g. /C for Carbon). '
            'Also used for ICPMS Standard Coverage standards notes.'
        )
    )
    source_element_field_key = models.CharField(
        max_length=1,
        blank=True,
        null=True,
        help_text=_(
            'Field key link: '
            'ICPMS Standard Coverage — Periodic Table field providing the element list; '
            'TABLE — numeric field whose value sets the table row count (first column = S.No.).'
        ),
    )
    editing_required = models.BooleanField(
        default=False,
        help_text=_('If checked, this field can be edited by the user after booking until status is Complete.')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Dynamic Input Field')
        verbose_name_plural = _('Dynamic Input Fields')
        unique_together = [['equipment', 'field_key']]
        ordering = ['equipment', 'field_key']
    
    def __str__(self):
        return f"{self.equipment} - {self.field_key}: {self.field_label}"

class MultiParamDefinition(models.Model):
    """Slot option definitions for MULTI_PARAM charge profiles.
    
    Each row represents a slot option (radio option) with time and charge
    configured per user type. Used for "No of slots" radio field configuration.
    """
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='param_definitions',
        help_text=_('Equipment this slot option belongs to'),
        null=True,
        blank=True,
    )
    user_type = models.CharField(
        max_length=50,
        help_text=_('User type this slot option applies to'),
        null=True,
        blank=True,
    )
    param_name = models.CharField(
        max_length=255,
        help_text=_('Name of the slot option (e.g., "Slot 1", "Slot 2", "Morning Slot")')
    )
    param_code = models.CharField(
        max_length=50,
        help_text=_('Code/identifier for the slot option (used in radio field)')
    )
    unit_time_minutes = models.IntegerField(
        help_text=_('Time in minutes per sample for this slot option')
    )
    unit_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_('Charge per sample for this slot option')
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_('Whether this slot option is active')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Slot Option Configuration')
        verbose_name_plural = _('Slot Options Configuration')
        ordering = ['equipment', 'user_type', 'param_name']
        unique_together = [['equipment', 'user_type', 'param_code']]
    
    def __str__(self):
        user_type_str = f" - {self.user_type}" if self.user_type else ""
        return f"{self.equipment.code if self.equipment else ''}{user_type_str} - {self.param_name}"


def print_stl_upload_to(instance, filename):
    ext = os.path.splitext(filename)[1].lower() or ".stl"
    uid = uuid.uuid4().hex
    # NOTE: When upload_to is a callable, Django does NOT apply strftime formatting.
    # We must expand date tokens ourselves.
    now = timezone.now()
    return now.strftime(f"print_stl/%Y/%m/%d/{uid}{ext}")


class PrintMaterial(models.Model):
    """Filament/material catalog for 3D printer equipment (dynamic pricing per gram)."""

    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="print_materials",
        help_text=_("3D printer equipment this material belongs to"),
    )
    code = models.CharField(
        max_length=64,
        help_text=_('Stable code stored in booking input B (e.g. "pla_white")'),
    )
    name = models.CharField(max_length=255, help_text=_("Display name"))
    density_g_per_cm3 = models.DecimalField(
        max_digits=6,
        decimal_places=3,
        default=Decimal("1.240"),
        help_text=_("Material density in g/cm³"),
    )
    price_per_gram = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_("Charge per gram of filament (INR)"),
    )
    user_type = models.CharField(
        max_length=50,
        blank=True,
        null=True,
        help_text=_("Optional: limit material to a user type; blank = all types"),
    )
    is_active = models.BooleanField(default=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["equipment", "display_order", "name"]
        verbose_name = _("3D print material")
        verbose_name_plural = _("3D print materials")
        unique_together = [["equipment", "code"]]

    def __str__(self):
        return f"{self.equipment.code} - {self.name}"


class PrintAnalysisStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    PROCESSING = "PROCESSING", _("Processing")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")


class PrintAnalysisMethod(models.TextChoices):
    CURAENGINE = "CURAENGINE", _("CuraEngine")
    HEURISTIC = "HEURISTIC", _("Heuristic")


class PrintAnalysisBatchStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    PROCESSING = "PROCESSING", _("Processing")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    PARTIAL = "PARTIAL", _("Partial")


class PrintAnalysisBatch(models.Model):
    """ZIP upload container grouping multiple STL analyses for one 3D print booking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="print_analysis_batches",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="print_analysis_batches",
    )
    material = models.ForeignKey(
        PrintMaterial,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analysis_batches",
    )
    original_filename = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=PrintAnalysisBatchStatus.choices,
        default=PrintAnalysisBatchStatus.PENDING,
    )
    slicer_settings = models.JSONField(default=dict, blank=True)
    error_message = models.TextField(blank=True, default="")
    booking = models.ForeignKey(
        "Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_analysis_batches",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("3D print analysis batch")
        verbose_name_plural = _("3D print analysis batches")

    def __str__(self):
        return f"PrintAnalysisBatch {self.id} ({self.status})"


class PrintAnalysis(models.Model):
    """STL upload analysis for 3D print quoting (weight, time, bounding box)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        PrintAnalysisBatch,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="items",
    )
    sequence = models.PositiveIntegerField(default=0)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="print_analyses",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="print_analyses",
    )
    material = models.ForeignKey(
        PrintMaterial,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="analyses",
    )
    stl_file = models.FileField(upload_to=print_stl_upload_to, max_length=512)
    original_filename = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=20,
        choices=PrintAnalysisStatus.choices,
        default=PrintAnalysisStatus.PENDING,
    )
    analysis_method = models.CharField(
        max_length=20,
        choices=PrintAnalysisMethod.choices,
        blank=True,
        default="",
    )
    weight_grams = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    volume_cm3 = models.DecimalField(max_digits=12, decimal_places=4, null=True, blank=True)
    estimated_time_minutes = models.PositiveIntegerField(null=True, blank=True)
    actual_weight_grams = models.DecimalField(
        max_digits=12,
        decimal_places=3,
        null=True,
        blank=True,
        help_text=_("Post-print actual filament weight (g), set by Admin/OIC for charge adjustment."),
    )
    actual_time_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_("Post-print actual machine time (min), set by Admin/OIC for charge adjustment."),
    )
    bounding_box = models.JSONField(default=dict, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    error_message = models.TextField(blank=True, default="")
    slicer_settings = models.JSONField(default=dict, blank=True)
    price_per_gram_snapshot = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True
    )
    material_code_snapshot = models.CharField(max_length=64, blank=True, default="")
    booking = models.ForeignKey(
        "Booking",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="print_analyses",
    )
    cancelled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When set, this file was removed from the booking via partial cancellation."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sequence", "created_at"]
        verbose_name = _("3D print analysis")
        verbose_name_plural = _("3D print analyses")

    def __str__(self):
        return f"PrintAnalysis {self.id} ({self.status})"


# ============================================================================
# Slot System
# ============================================================================
class SlotStatus(models.TextChoices):
    """Daily slot availability status."""
    AVAILABLE = 'AVAILABLE', _('Available')
    NOT_AVAILABLE = 'NOT_AVAILABLE', _('Not Available')
    BOOKED = 'BOOKED', _('Booked')
    BLOCKED = 'BLOCKED', _('Blocked')
    UNDER_MAINTENANCE = 'UNDER_MAINTENANCE', _('Under Maintenance')
    OPERATOR_ABSENT = 'OPERATOR_ABSENT', _('Operator Absent')
    BOOKING_NOT_UTILIZED = 'BOOKING_NOT_UTILIZED', _('Booking Not Utilized')


class SlotMaster(models.Model):
    """Slot master template linked to ChargeProfile with dynamic slot generation."""
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='slot_masters',
        help_text=_('Equipment this slot belongs to'),
        null=True,
        blank=True,
    )
    slot_name = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        help_text=_('Optional name for this slot')
    )
    slot_number = models.IntegerField(
        help_text=_('Slot number (1, 2, 3, etc.)')
    )
    open_time = models.TimeField(
        help_text=_('Slot open/start time')
    )
    close_time = models.TimeField(
        help_text=_('Slot close/end time. Use 00:00 for end-of-day; if earlier than open time the slot spans midnight (e.g. 18:00–00:00).')
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_('Whether this slot is active')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Slot Master')
        verbose_name_plural = _('Slot Masters')
        unique_together = [['equipment', 'slot_number']]
        ordering = ['equipment', 'slot_number']
    
    def __str__(self):
        name = f" - {self.slot_name}" if self.slot_name else ""
        equipment_code = self.equipment.code if self.equipment else "N/A"
        return f"{equipment_code} - Slot {self.slot_number}{name}: {self.open_time} - {self.close_time}"
    
    def clean(self):
        """Reject zero-length slots. close_time before open_time is allowed (crosses midnight, e.g. 18:00–00:00)."""
        if self.open_time is not None and self.close_time is not None:
            if self.close_time == self.open_time:
                raise ValidationError(_('Close time must be after open time.'))


class DailySlot(models.Model):
    """Daily slot instance generated from SlotMaster for booking."""
    
    slot_master = models.ForeignKey(
        SlotMaster,
        on_delete=models.CASCADE,
        related_name='daily_slots',
        help_text=_('Slot master this daily slot references')
    )
    date = models.DateField(
        help_text=_('Date of this slot')
    )
    start_datetime = models.DateTimeField(
        help_text=_('Start datetime of this slot')
    )
    end_datetime = models.DateTimeField(
        help_text=_('End datetime of this slot')
    )
    status = models.CharField(
        max_length=20,
        choices=SlotStatus.choices,
        default=SlotStatus.AVAILABLE,
        help_text=_('Availability status of this slot')
    )
    blocked_label = models.CharField(
        max_length=255,
        blank=True,
        null=True,
        help_text=_('Custom label/reason when slot is blocked')
    )
    reserved_for_external = models.BooleanField(
        default=False,
        verbose_name=_('Reserved for External Users'),
        help_text=_('When True, this slot is shown as Available to external users; only these slots can be booked by external users. Admin and OIC can mark/unmark.')
    )
    home_department_only = models.BooleanField(
        default=False,
        db_index=True,
        verbose_name=_('Reserved for non-home department'),
        help_text=_(
            'When True, this slot is reserved for users outside the equipment’s home '
            '(internal) department. Unmarked slots are home-department only while any '
            'upcoming reserved mark exists on the equipment. Unbooked reserved slots open '
            'to all departments once within Reschedule Hours Threshold before start. '
            'Admin and OIC can mark/unmark. Has no effect if the equipment has no '
            'internal department.'
        ),
    )
    booking = models.ForeignKey(
        'Booking',
        on_delete=models.SET_NULL,
        related_name='daily_slots',
        blank=True,
        null=True,
        help_text=_('Booking associated with this slot')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Daily Slot')
        verbose_name_plural = _('Daily Slots')
        ordering = ['date', 'start_datetime']
        unique_together = [['slot_master', 'date']]
        indexes = [
            models.Index(fields=['date', 'status']),
            models.Index(fields=['slot_master', 'date']),
            # Quota: EXISTS (daily_slots WHERE booking_id = … AND start_datetime in period)
            models.Index(
                fields=['booking', 'start_datetime'],
                name='equip_ds_booking_start_dt',
                condition=models.Q(booking__isnull=False),
            ),
        ]
    
    def __str__(self):
        if self.slot_master and self.slot_master.equipment:
            equipment_code = self.slot_master.equipment.code
            slot_number = self.slot_master.slot_number
        else:
            equipment_code = 'N/A'
            slot_number = 'N/A'
        
        time_str = f"({self.start_datetime.strftime('%H:%M')} - {self.end_datetime.strftime('%H:%M')})" if self.start_datetime and self.end_datetime else ""
        return f"{equipment_code} - Slot {slot_number} - {self.date} {time_str}".strip()


# ============================================================================
# Operator leave management
# ============================================================================


class OperatorLeaveRequest(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", _("Pending OIC approval")
        APPROVED = "APPROVED", _("Approved")
        REJECTED = "REJECTED", _("Rejected")
        CANCELLED = "CANCELLED", _("Cancelled by operator")

    class Session(models.TextChoices):
        FN = "FN", _("Forenoon")
        AN = "AN", _("Afternoon")

    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name="operator_leave_requests",
        null=True,
        blank=True,
    )
    operator = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="operator_leave_requests",
        limit_choices_to={"user_type": UserType.OPERATOR},
    )
    start_date = models.DateField()
    start_session = models.CharField(max_length=2, choices=Session.choices, default=Session.FN)
    end_date = models.DateField()
    end_session = models.CharField(max_length=2, choices=Session.choices, default=Session.AN)
    reason = models.TextField()
    attachment = models.FileField(upload_to="operator_leave_attachments/", blank=True, null=True)

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    rejection_reason = models.TextField(blank=True, null=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_leave_requests_reviewed",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["operator", "start_date", "end_date"]),
            models.Index(fields=["equipment", "status", "start_date"]),
        ]

    def __str__(self):
        return f"Leave #{self.id} {self.operator_id} {self.start_date}-{self.end_date} ({self.status})"


# ============================================================================
# Booking System
# ============================================================================

class BookingDisruptionKind(models.TextChoices):
    """Why the booking entered the disruption (awaiting user refund vs reschedule choice)."""
    MAINTENANCE = "MAINTENANCE", _("Maintenance / equipment")
    OPERATOR_ABSENT = "OPERATOR_ABSENT", _("Operator unavailable")
    OTHER_DISRUPTION = "OTHER_DISRUPTION", _("Other disruption")


class IstemFbrStatus(models.TextChoices):
    """I-STEM Facility Booking Record (FBR) workflow for external users (GOI portal)."""
    PENDING_FBR = 'PENDING_FBR', _('FBR not submitted')
    PENDING_OIC = 'PENDING_OIC', _('Awaiting OIC verification')
    INVALID = 'INVALID', _('FBR rejected — correction required')
    EXECUTED = 'EXECUTED', _('FBR verified')


def charge_profile_requires_istem_fbr(charge_profile) -> bool:
    """True when this charge profile requires the I-STEM FBR workflow."""
    if not charge_profile:
        return False
    return bool(getattr(charge_profile, "require_istem_fbr", False))


def get_equipment_istem_portal_url(equipment) -> str:
    """Equipment-specific I-STEM booking link, or the national portal default."""
    url = (getattr(equipment, "istem_portal_url", None) or "").strip()
    if url:
        return url
    return "https://www.istem.gov.in/"


def get_equipment_istem_fbr_status_url(equipment) -> str:
    """Equipment-specific I-STEM FBR status check link for OIC/Admin (no default)."""
    return (getattr(equipment, "istem_fbr_status_url", None) or "").strip()


def initial_istem_fbr_fields_for_charge_profile(charge_profile) -> dict:
    """Return Booking field defaults for I-STEM FBR tracking when required by charge profile."""
    if charge_profile_requires_istem_fbr(charge_profile):
        return {"istem_fbr_status": IstemFbrStatus.PENDING_FBR}
    return {}


def initial_istem_fbr_fields_for_user_type(user_type: str, charge_profile=None) -> dict:
    """Backward-compatible helper; prefer charge_profile when available."""
    if charge_profile is not None:
        return initial_istem_fbr_fields_for_charge_profile(charge_profile)
    return {}


class BookingStatus(models.TextChoices):
    """Booking status choices."""
    PENDING = 'PENDING', _('Pending')
    PENDING_PAYMENT = 'PENDING_PAYMENT', _('Awaiting payment')
    WAITLISTED = 'WAITLISTED', _('Waitlisted')
    BOOKED = 'BOOKED', _('Booked')
    DISRUPTION_PENDING = 'DISRUPTION_PENDING', _('Awaiting your choice (disruption)')
    UNDER_MAINTENANCE = 'UNDER_MAINTENANCE', _('Under Maintenance')
    OTHER_DISRUPTION = 'OTHER_DISRUPTION', _('Other Disruption')
    HOLD = 'HOLD', _('Hold')
    COMPLETED = 'COMPLETED', _('Completed')
    CANCELLED = 'CANCELLED', _('Cancelled')
    ABSENT = 'ABSENT', _('Operator Unavailable')
    REFUNDED = 'REFUNDED', _('Refunded')
    BOOKING_NOT_UTILIZED = 'BOOKING_NOT_UTILIZED', _('Booking Not Utilized')


class Booking(models.Model):
    """Equipment booking with charge and time calculations."""
    booking_id = models.AutoField(primary_key=True)
    virtual_booking_id = models.CharField(
        max_length=64,
        unique=True,
        blank=True,
        null=True,
        verbose_name=_('Virtual Booking ID'),
        help_text=_(
            'Display ID: internal department code + equipment code + year + 5-digit sequence '
            '(e.g. CHGEM202600001)'
        ),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='bookings',
        help_text=_('User who made the booking')
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.PROTECT,
        related_name='bookings',
        help_text=_('Equipment being booked')
    )
    charge_profile = models.ForeignKey(
        ChargeProfile,
        on_delete=models.PROTECT,
        related_name='bookings',
        help_text=_('Charge profile used (snapshot)')
    )
    user_type_snapshot = models.CharField(
        max_length=50,
        help_text=_('User type at time of booking (snapshot)')
    )
    # Time and charge calculations
    total_time_minutes = models.IntegerField(
        help_text=_('Total booking time in minutes')
    )
    total_charge = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_('Total charge for this booking')
    )
    
    # Dynamic input values (stored as JSON)
    input_values = models.JSONField(
        default=dict,
        help_text=_('Dynamic input field values (A-G)')
    )
    
    # Selected parameters (for MULTI_PARAM)
    selected_parameters = models.JSONField(
        default=list,
        blank=True,
        null=True,
        help_text=_('Selected parameter codes (for MULTI_PARAM)')
    )
    
    # Charge breakdown (audit trail)
    charge_breakdown = models.JSONField(
        default=list,
        help_text=_('Line-by-line charge breakdown for audit')
    )
    
    # Status
    status = models.CharField(
        max_length=30,
        choices=BookingStatus.choices,
        default=BookingStatus.PENDING,
        help_text=_('Booking status')
    )

    # Payment (external: wallet partial + SBIePay / offline UTR for remainder)
    wallet_amount_applied = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Amount debited from department sub-wallet at booking time"),
    )
    amount_due = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text=_("Remaining amount to collect after wallet debit"),
    )
    settlement_department = models.ForeignKey(
        "users.Department",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="bookings_settled",
        help_text=_("Internal department (from equipment) for payment settlement"),
    )
    payment_settled_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("When booking balance was fully paid (wallet + gateway/UTR)"),
    )
    
    # Notes
    notes = models.TextField(
        blank=True,
        null=True,
        help_text=_('Additional notes for this booking')
    )

    # Grace deadline for auto Operator Absent / Operator Unavailable jobs (does not change slots).
    operator_absent_hold_until = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Operator absent hold until'),
        help_text=_(
            'When set by Admin/OIC, automatic Operator Absent / Operator Unavailable marking '
            'uses the later of this time and the last booked slot end as the booking end reference. '
            'Slots and booking schedule are not modified.'
        ),
    )
    atmosphere_sensitive_sample = models.BooleanField(
        default=False,
        verbose_name=_('Atmosphere-sensitive sample'),
        help_text=_(
            'When True, the sample may be submitted at slot start instead of the normal submission lead time. '
            'Staff are notified and should not mark Booking Not Utilized before the slot begins for delayed submission.'
        ),
    )
    sample_submission_deadline_reminder_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Sample submission deadline reminder sent at'),
        help_text=_(
            'When the advance (12 hours before sample submission deadline) email/notification was sent. '
            'Null means not yet sent.'
        ),
    )

    print_analysis = models.ForeignKey(
        "PrintAnalysis",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_bookings",
        help_text=_("Primary STL analysis snapshot used for 3D print bookings"),
    )
    print_analysis_batch = models.ForeignKey(
        "PrintAnalysisBatch",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="linked_bookings",
        help_text=_("ZIP batch of STL analyses for multi-file 3D print bookings"),
    )

    # External-user logistics: return samples after analysis (adds return shipping fee).
    sample_return_after_analysis = models.BooleanField(
        default=False,
        verbose_name=_("Return sample after analysis"),
        help_text=_(
            "External bookings only. When enabled, operator should return the submitted sample(s) after analysis. "
            "A return shipping fee may be added to booking charges."
        ),
    )
    return_shipping_fee_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_("Return shipping fee amount"),
        help_text=_(
            "Snapshot of the return shipping fee applied to this booking (in INR). Stored to keep historical bookings "
            "stable even if the admin-configured fee changes later."
        ),
    )

    # Return-shipping dispatch details (set by Accounts In Charge for external sample returns).
    return_shipping_company = models.CharField(
        max_length=128,
        blank=True,
        default="",
        verbose_name=_("Return shipping company"),
        help_text=_("Shipping/courier company for return dispatch (set by Accounts In Charge)."),
    )
    return_shipping_tracking_id = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Return shipping tracking ID"),
        help_text=_("Tracking or AWB number for returning samples to the user."),
    )
    return_shipping_tracking_updated_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_("Return shipping tracking updated at"),
        help_text=_("When the return shipping tracking info was last updated."),
    )
    
    # Set when user is first notified that S3 results are available (in-app + email)
    results_available_notified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Results available notified at'),
        help_text=_('When the user was notified (in-app and email) that result files are available in S3'),
    )
    
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bookings_created',
        verbose_name=_('Created by'),
        help_text=_('User who created this booking (admin, officer in charge, or the booking user)'),
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    completed_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_('When the booking was marked as completed. Used for repeat-sample request time limit.'),
        verbose_name=_('Completed at')
    )

    # Repeat sample: when True, the booking user can create a replica booking (same params, discount = original amount).
    repeat_sample_enabled = models.BooleanField(
        default=False,
        verbose_name=_('Repeat sample enabled'),
        help_text=_('If True, admin/OIC has enabled repeat sample for this completed booking; the user can create one replica booking.')
    )
    # When set, this booking is a repeat of source_booking; virtual_booking_id = source_booking.virtual_booking_id + "R". Excluded from quota.
    source_booking = models.ForeignKey(
        'self',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='repeat_bookings',
        verbose_name=_('Source booking (repeat of)'),
        help_text=_('If set, this booking is a repeat sample of the source booking; excluded from weekly/monthly limits.')
    )

    # User rating (computed overall 0-5 + optional feedback). Only the booking user can submit.
    rating_on_time_operator_availability = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_('On-time & operator availability'),
        help_text=_('User rating criteria: was the operator available and on-time? (Yes/No)'),
    )
    rating_laboratory_cleanliness_organization = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_('Laboratory cleanliness & organization'),
        help_text=_('User rating criteria: lab cleanliness and organization (Yes/No)'),
    )
    rating_sample_handling_care = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_('Sample handling & care'),
        help_text=_('User rating criteria: sample handling and care (Yes/No)'),
    )
    rating_operator_behaviour_professionalism = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_('Operator behaviour & professionalism'),
        help_text=_('User rating criteria: operator behaviour and professionalism (Yes/No)'),
    )
    rating_compliance_booking_request_parameters = models.BooleanField(
        null=True,
        blank=True,
        verbose_name=_('Compliance with booking request parameters'),
        help_text=_('User rating criteria: compliance with booking request parameters (Yes/No)'),
    )

    rating = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text=_('Overall user rating computed from criteria (0-5)'),
        verbose_name=_('Rating'),
        validators=[MinValueValidator(0), MaxValueValidator(5)]
    )
    rating_feedback = models.TextField(
        blank=True,
        null=True,
        help_text=_('Optional feedback text from the user who rated'),
        verbose_name=_('Rating feedback')
    )
    rated_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_('When the user submitted the rating'),
        verbose_name=_('Rated at')
    )

    rating_removed = models.BooleanField(
        default=False,
        verbose_name=_('Rating removed'),
        help_text=_('If True, this booking rating has been removed by admin/OIC and is excluded from equipment aggregates and user-visible lists.'),
    )
    rating_removed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Rating removed at'),
        help_text=_('When the rating was removed by admin/OIC.'),
    )
    rating_removed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='booking_ratings_removed',
        verbose_name=_('Rating removed by'),
        help_text=_('Admin/OIC user who removed this rating.'),
    )
    rating_removed_reason = models.TextField(
        null=True,
        blank=True,
        verbose_name=_('Rating removed reason'),
        help_text=_('Optional reason recorded when the rating is removed.'),
    )

    def compute_overall_rating_from_criteria(self):
        """
        Return overall rating 0-5 computed from the five yes/no criteria.
        Returns None if any criteria is unset (null).
        """
        criteria = [
            self.rating_on_time_operator_availability,
            self.rating_laboratory_cleanliness_organization,
            self.rating_sample_handling_care,
            self.rating_operator_behaviour_professionalism,
            self.rating_compliance_booking_request_parameters,
        ]
        if any(v is None for v in criteria):
            return None
        return int(sum(1 for v in criteria if v))

    # After charge recalculation (when parameters change on BOOKED): negative = refund to process, positive = extra to pay. Cleared when Refund or Pay Now is completed.
    charge_recalculation_pending_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_('Charge recalculation pending amount'),
        help_text=_('After charge recalculation: negative = refund to process, positive = extra amount to pay. Cleared when Refund or Pay Now is completed.'),
    )
    reward_points_used = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_('Reward points used'),
        help_text=_('Reward points redeemed for this booking'),
    )
    reward_discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_('Reward discount amount'),
        help_text=_('Discount amount applied from TA reward points'),
    )
    maintenance_disruption_flag = models.BooleanField(
        default=False,
        verbose_name=_('Maintenance disruption policy active'),
        help_text=_(
            'Set when equipment goes under maintenance while the user has an affected same-day booking; '
            'cancel stays available; reschedule unlocks when equipment is operational; optional auto-cancel at deadline.'
        ),
    )
    maintenance_decision_deadline_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Maintenance policy decision deadline'),
        help_text=_('If still undecided, booking may be auto-cancelled with full refund (slot window rule).'),
    )
    maintenance_reschedule_extra_week = models.BooleanField(
        default=False,
        verbose_name=_('Extra week for maintenance reschedule'),
        help_text=_('When True, slots API extends the visible window for this user reschedule (see maintenance operational marked time).'),
    )
    maintenance_operational_marked_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Equipment operational (maintenance reschedule)'),
        help_text=_(
            'When equipment returned from under maintenance; used with slot window reference to decide '
            'whether reschedule calendar gets one or two extra weeks.'
        ),
    )
    disruption_kind = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        choices=BookingDisruptionKind.choices,
        verbose_name=_('Disruption kind'),
        help_text=_('Set when status is Awaiting your choice (disruption); cleared when resolved.'),
    )
    disruption_reason = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Disruption reason'),
        help_text=_('Staff-provided reason for "Other disruption" emails; cleared when resolved.'),
    )
    disruption_release_slot_status = models.CharField(
        max_length=32,
        blank=True,
        null=True,
        verbose_name=_('Disruption slot release status'),
        help_text=_(
            'DailySlot status to apply when this booking is refunded or auto-cancelled under disruption policy '
            '(e.g. Under Maintenance vs Operator Absent).'
        ),
    )
    quota_period_anchor_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Quota period anchor datetime'),
        help_text=_(
            'When set, weekly/monthly quota calculations use this datetime to decide the quota period '
            '(instead of the current booking slots). Used for disruption reschedules so quota stays in the original period.'
        ),
    )
    # I-STEM (https://www.istem.gov.in/) FBR linkage — external user bookings only; null = not applicable (internal) or legacy row
    istem_fbr_number = models.CharField(
        max_length=128,
        blank=True,
        default='',
        verbose_name=_('I-STEM FBR number'),
        help_text=_('Facility Booking Record number from the national I-STEM portal for this request.'),
    )
    istem_fbr_status = models.CharField(
        max_length=20,
        choices=IstemFbrStatus.choices,
        null=True,
        blank=True,
        verbose_name=_('I-STEM FBR status'),
        help_text=_('Workflow state for external-user FBR verification by OIC. Null for internal users or pre-migration bookings.'),
    )
    istem_fbr_invalid_reason = models.TextField(
        null=True,
        blank=True,
        verbose_name=_('I-STEM FBR rejection reason'),
        help_text=_('Shown to the user when OIC marks the FBR as invalid.'),
    )
    istem_fbr_executed_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('I-STEM FBR executed at'),
        help_text=_('When OIC marked the FBR as verified.'),
    )
    istem_fbr_verified_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='bookings_istem_fbr_verified',
        verbose_name=_('I-STEM FBR verified by'),
        help_text=_('OIC (or admin) who last verified or rejected the FBR.'),
    )

    class Meta:
        verbose_name = _('Booking')
        verbose_name_plural = _('Bookings')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'status']),
            models.Index(fields=['equipment', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['virtual_booking_id']),
            models.Index(fields=['maintenance_disruption_flag', 'maintenance_decision_deadline_at']),
            # Quota: filter user + equipment (+ status IN …) + source_booking IS NULL
            models.Index(
                fields=['user', 'equipment', 'status'],
                name='book_quota_user_eq_st',
                condition=models.Q(source_booking__isnull=True),
            ),
            models.Index(
                fields=['equipment', 'user_type_snapshot', 'status'],
                name='book_quota_eq_ut_st',
                condition=models.Q(source_booking__isnull=True),
            ),
        ]
    
    @classmethod
    def department_code_for_virtual_id(cls, equipment) -> str:
        """Internal department short code prefixed on virtual booking IDs."""
        if equipment is None:
            return ""
        dept = getattr(equipment, "internal_department", None)
        if dept is None:
            return ""
        return (getattr(dept, "code", None) or "").strip()

    @classmethod
    def _virtual_id_prefix(cls, equipment_id: int, equipment_code: str, department_code: str = "") -> str:
        """
        Build allocation prefix: {department_code}{equipment_code}{year}.
        Department code comes from equipment.internal_department when not supplied.
        """
        dept_code = (department_code or "").strip()
        if not dept_code:
            try:
                dept_code = (
                    Equipment.objects.select_related("internal_department")
                    .filter(pk=equipment_id)
                    .values_list("internal_department__code", flat=True)
                    .first()
                ) or ""
                dept_code = str(dept_code).strip()
            except Exception:
                dept_code = ""
        code = (equipment_code or "").strip()
        year = timezone.now().year
        return f"{dept_code}{code}{year}"

    @classmethod
    def _allocate_base_virtual_booking_id(
        cls,
        equipment_id: int,
        code: str,
        department_code: str = "",
    ) -> str:
        """
        Next display ID: {dept}{code}{year}{seq:05d}. Uses indexed MAX(seq), not COUNT(*), so busy equipment
        does not scan every booking row on each insert (was causing multi-minute API delays).
        """
        prefix = cls._virtual_id_prefix(equipment_id, code, department_code=department_code)
        plen = len(prefix)
        target_len = plen + 5
        table = connection.ops.quote_name(cls._meta.db_table)
        max_seq = 0
        try:
            vendor = connection.vendor
            with connection.cursor() as cursor:
                if vendor == "postgresql":
                    pattern = f"^{re.escape(prefix)}[0-9]{{5}}$"
                    cursor.execute(
                        f"""
                        SELECT COALESCE(MAX(CAST(SUBSTRING(virtual_booking_id FROM %s FOR 5) AS INTEGER)), 0)
                        FROM {table}
                        WHERE equipment_id = %s AND virtual_booking_id ~ %s
                        """,
                        [plen + 1, equipment_id, pattern],
                    )
                    row = cursor.fetchone()
                    max_seq = int(row[0] or 0)
                elif vendor == "sqlite":
                    cursor.execute(
                        f"""
                        SELECT COALESCE(MAX(CAST(SUBSTR(virtual_booking_id, ?, 5) AS INTEGER)), 0)
                        FROM {table}
                        WHERE equipment_id = ?
                          AND LENGTH(virtual_booking_id) = ?
                          AND virtual_booking_id LIKE ?
                        """,
                        [plen + 1, equipment_id, target_len, f"{prefix}%"],
                    )
                    row = cursor.fetchone()
                    max_seq = int(row[0] or 0)
                elif vendor == "mysql":
                    pattern = f"^{re.escape(prefix)}[0-9]{{5}}$"
                    cursor.execute(
                        f"""
                        SELECT COALESCE(MAX(CAST(SUBSTRING(virtual_booking_id, %s, 5) AS UNSIGNED)), 0)
                        FROM {table}
                        WHERE equipment_id = %s AND virtual_booking_id REGEXP %s
                        """,
                        [plen + 1, equipment_id, pattern],
                    )
                    row = cursor.fetchone()
                    max_seq = int(row[0] or 0)
                else:
                    max_seq = cls.objects.filter(
                        equipment_id=equipment_id,
                        created_at__year=year,
                    ).count()
        except Exception:
            logger.exception(
                "allocate_virtual_booking_id failed equipment_id=%s prefix=%s; falling back to count()",
                equipment_id,
                prefix,
            )
            max_seq = cls.objects.filter(
                equipment_id=equipment_id,
                created_at__year=year,
            ).count()
        next_seq = max_seq + 1
        if next_seq > 99999:
            next_seq = 99999
        return f"{prefix}{next_seq:05d}"

    def save(self, *args, **kwargs):
        if not self.pk and not self.virtual_booking_id and self.equipment_id:
            # Repeat booking: virtual_booking_id = source_booking.virtual_booking_id + "R"
            if self.source_booking_id:
                source = self.source_booking if hasattr(self, 'source_booking') and self.source_booking else None
                if not source:
                    source = Booking.objects.filter(booking_id=self.source_booking_id).first()
                if source and source.virtual_booking_id:
                    self.virtual_booking_id = source.virtual_booking_id + 'R'
            if not self.virtual_booking_id:
                code = ""
                dept_code = ""
                equipment_obj = None
                if hasattr(self, "equipment") and self.equipment:
                    equipment_obj = self.equipment
                    code = equipment_obj.code or ""
                    dept_code = Booking.department_code_for_virtual_id(equipment_obj)
                if not code:
                    try:
                        equipment_obj = (
                            Equipment.objects.select_related("internal_department")
                            .filter(pk=self.equipment_id)
                            .first()
                        )
                        if equipment_obj:
                            code = equipment_obj.code or ""
                            dept_code = Booking.department_code_for_virtual_id(equipment_obj)
                    except Equipment.DoesNotExist:
                        pass
                self.virtual_booking_id = Booking._allocate_base_virtual_booking_id(
                    self.equipment_id,
                    code,
                    department_code=dept_code,
                )
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"Booking #{self.booking_id} - {self.equipment.code} - {self.user.email}"


class BookingResultFile(models.Model):
    """Result files uploaded when completing a booking (e.g. analysis results). Sent to user email."""
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='result_files',
        help_text=_('Booking this result file belongs to'),
    )
    file = models.FileField(
        upload_to='booking_results/%Y/%m/%d/',
        help_text=_('Uploaded result file'),
    )
    original_name = models.CharField(
        max_length=255,
        blank=True,
        help_text=_('Original filename when uploaded'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        verbose_name = _('Booking result file')
        verbose_name_plural = _('Booking result files')

    def __str__(self):
        return f"Booking #{self.booking.booking_id} - {self.original_name or self.file.name}"


# ============================================================================
# Repeat sample request (after completed booking)
# ============================================================================

class RepeatSampleRequestStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')


class RepeatSampleRequest(models.Model):
    """User request to repeat a completed booking (e.g. results not appropriate). Admin/OIC can approve (creates free re-book) or reject."""
    id = models.AutoField(primary_key=True)
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='repeat_sample_requests',
        help_text=_('Completed booking this request refers to'),
    )
    status = models.CharField(
        max_length=20,
        choices=RepeatSampleRequestStatus.choices,
        default=RepeatSampleRequestStatus.PENDING,
    )
    user_notes = models.TextField(blank=True, default='')
    requested_at = models.DateTimeField(auto_now_add=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    responded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='repeat_sample_requests_responded',
    )
    admin_notes = models.TextField(blank=True, default='')
    new_booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='created_from_repeat_request',
        help_text=_('New booking created when request was approved (free re-run)'),
    )

    class Meta:
        ordering = ['-requested_at']
        verbose_name = _('Repeat sample request')
        verbose_name_plural = _('Repeat sample requests')

    def __str__(self):
        return f"Repeat request #{self.id} – Booking #{self.booking_id} – {self.status}"


# ============================================================================
# Urgent booking request (internal users) + log of no-slot allocation
# ============================================================================

class NoSlotAllocationLog(models.Model):
    """Log of booking requests by users to whom no slot was allocated. Used by admin/OIC for urgent slot decisions."""
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='no_slot_allocation_logs',
        help_text=_('User who requested booking but got no slots'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='no_slot_allocation_logs',
        help_text=_('Equipment for which slots were requested'),
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    number_of_samples = models.PositiveIntegerField(
        default=1,
        help_text=_('Number of samples requested'),
    )
    slots_requested = models.PositiveIntegerField(
        default=1,
        help_text=_('Number of slots requested'),
    )
    duration_minutes = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_('Total duration requested in minutes'),
    )

    class Meta:
        ordering = ['-requested_at']
        verbose_name = _('No slot allocation log entry')
        verbose_name_plural = _('No slot allocation log')
        indexes = [
            models.Index(fields=['user', 'requested_at']),
            models.Index(fields=['equipment', 'requested_at']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.equipment.code} @ {self.requested_at}"


class WaitlistEntry(models.Model):
    """
    Waitlist queue entry per equipment. When a booking attempt fails, the user may be added here
    (if equipment has waitlist_queue_depth > 0). Order is by created_at (FIFO).
    Admin/OIC can view and clear the queue. When slots become available, all waitlisted users are notified.
    """
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='waitlist_entries',
        help_text=_('User on the waitlist'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='waitlist_entries',
        help_text=_('Equipment this waitlist entry is for'),
    )
    status = models.CharField(
        max_length=32,
        default="ACTIVE",
        help_text=_("ACTIVE: eligible for auto-booking. CANNOT_FULFILL: removed from queue but kept for audit/visibility."),
    )
    cannot_fulfill_remark = models.TextField(
        blank=True,
        null=True,
        help_text=_("Reason why this waitlist entry could not be fulfilled/auto-booked."),
    )
    marked_cannot_fulfill_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text=_("When this waitlist entry was marked as cannot fulfill."),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['equipment', 'created_at']
        verbose_name = _('Waitlist entry')
        verbose_name_plural = _('Waitlist entries')
        unique_together = [['equipment', 'user']]
        indexes = [
            models.Index(fields=['equipment', 'created_at']),
            models.Index(fields=['equipment', 'status', 'created_at']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.equipment.code} @ {self.created_at}"


class BookingAttemptOutcome(models.TextChoices):
    SUCCESS = 'SUCCESS', _('Success')
    FAILED = 'FAILED', _('Failed')


class BookingAttemptLog(models.Model):
    """
    Comprehensive log of every booking submit attempt (success or failure).
    Used for admin/OIC to view full history with filters; failure reason is stored when outcome is FAILED.
    """
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='booking_attempt_logs',
        help_text=_('User who attempted the booking'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='booking_attempt_logs',
        help_text=_('Equipment for which booking was attempted'),
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(
        max_length=20,
        choices=BookingAttemptOutcome.choices,
        help_text=_('Whether the booking succeeded or failed'),
    )
    failure_reason = models.TextField(
        blank=True,
        default='',
        help_text=_('Reason for failure when outcome is FAILED'),
    )
    number_of_samples = models.PositiveIntegerField(default=1)
    slots_requested = models.PositiveIntegerField(default=1)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    booking_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=_('Booking ID when outcome is SUCCESS'),
    )
    additional_info = models.JSONField(
        null=True,
        blank=True,
        help_text=_('Additional information provided when raising the request (e.g. input_values, selected_parameters)'),
    )

    class Meta:
        ordering = ['-requested_at']
        verbose_name = _('Booking request log entry')
        verbose_name_plural = _('Booking requests log')
        indexes = [
            models.Index(fields=['requested_at']),
            models.Index(fields=['equipment', 'requested_at']),
            models.Index(fields=['user', 'requested_at']),
            models.Index(fields=['outcome', 'requested_at']),
        ]

    def __str__(self):
        return f"{self.user.email} - {self.equipment.code} - {self.outcome} @ {self.requested_at}"


class UrgentBookingRequestStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    EXPIRED = 'EXPIRED', _('Expired')


class UrgentBookingRequestType(models.TextChoices):
    NO_SLOT = 'NO_SLOT', _('Unable to get slot despite repeated trials')
    REVIEWER_URGENT = 'REVIEWER_URGENT', _('Urgent comment from reviewer')


class UrgentBookingRequest(models.Model):
    """Urgent booking request from an internal user who could not get slots or has urgent reviewer comment."""
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='urgent_booking_requests',
        help_text=_('User who requested urgent booking'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='urgent_booking_requests',
        help_text=_('Equipment for which urgent booking is requested'),
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    request_type = models.CharField(
        max_length=24,
        choices=UrgentBookingRequestType.choices,
        default=UrgentBookingRequestType.NO_SLOT,
        help_text=_('Reason: no slot despite trials, or urgent comment from reviewer'),
    )
    disclaimer_accepted = models.BooleanField(
        default=False,
        help_text=_('User confirmed disclaimer about genuine urgent requirement'),
    )
    number_of_samples = models.PositiveIntegerField(default=1)
    slots_requested = models.PositiveIntegerField(default=1)
    duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    evidence_file = models.FileField(
        upload_to='urgent_requests/%Y/%m/%d/',
        null=True,
        blank=True,
        help_text=_('Documentary evidence for urgent comment from reviewer'),
    )
    evidence_original_name = models.CharField(max_length=255, blank=True)
    reviewer_comment = models.TextField(
        blank=True,
        default='',
        help_text=_('Faculty narrative for urgent comment from reviewer (submitted with evidence)'),
    )
    status = models.CharField(
        max_length=20,
        choices=UrgentBookingRequestStatus.choices,
        default=UrgentBookingRequestStatus.PENDING,
    )
    admin_notes = models.TextField(blank=True, default='')
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='urgent_requests_decided',
        verbose_name=_('Decided by'),
    )
    wallet_approved_at = models.DateTimeField(null=True, blank=True)
    wallet_approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='urgent_requests_wallet_approved',
        verbose_name=_('Approved by Supervisor'),
    )
    wallet_notes = models.TextField(blank=True, default='')

    # Optional: linked hold booking created via "Select Slot" in urgent flow. When admin/OIC approves, this booking is debited and set to BOOKED.
    hold_booking = models.ForeignKey(
        'equipment.Booking',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='urgent_booking_request',
        verbose_name=_('Hold booking'),
        help_text=_('Booking in HOLD status linked to this urgent request; converted to BOOKED on approval'),
    )

    class Meta:
        ordering = ['-requested_at']
        verbose_name = _('Urgent booking request')
        verbose_name_plural = _('Urgent booking requests')
        indexes = [
            models.Index(fields=['status', 'requested_at']),
            models.Index(fields=['user', 'equipment', 'status', 'decided_at']),
        ]

    def __str__(self):
        return f"Urgent: {self.user.email} - {self.equipment.code} ({self.get_status_display()})"


# ============================================================================
# Booking Event History
# ============================================================================

class BookingEventType(models.TextChoices):
    """Booking event type choices."""
    CREATED = 'CREATED', _('Created')
    CONFIRMED = 'CONFIRMED', _('Confirmed')
    CANCELLED = 'CANCELLED', _('Cancelled')
    RESCHEDULED = 'RESCHEDULED', _('Rescheduled')
    COMPLETED = 'COMPLETED', _('Completed')
    REFUNDED = 'REFUNDED', _('Refunded')
    ABSENT = 'ABSENT', _('Operator Unavailable')
    COMMENT = 'COMMENT', _('Comment')
    STATUS_CHANGED = 'STATUS_CHANGED', _('Status Changed')
    CHARGE_RECALCULATED = 'CHARGE_RECALCULATED', _('Charge Recalculated')
    REPEAT_SAMPLE_OFFERED = 'REPEAT_SAMPLE_OFFERED', _('Repeat Sample Request Approved')
    REPEAT_SAMPLE_CREATED = 'REPEAT_SAMPLE_CREATED', _('Repeat Sample Booking Confirmed')


class BookingEvent(models.Model):
    """Event history for bookings with comments and notifications."""
    event_id = models.AutoField(primary_key=True)
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='events',
        help_text=_('Booking this event belongs to')
    )
    event_type = models.CharField(
        max_length=32,
        choices=BookingEventType.choices,
        help_text=_('Type of event')
    )
    previous_status = models.CharField(
        max_length=30,
        choices=BookingStatus.choices,
        blank=True,
        null=True,
        help_text=_('Previous booking status (if status changed)')
    )
    new_status = models.CharField(
        max_length=30,
        choices=BookingStatus.choices,
        blank=True,
        null=True,
        help_text=_('New booking status (if status changed)')
    )
    comment = models.TextField(
        blank=True,
        null=True,
        help_text=_('Comment or description for this event')
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name='booking_events_created',
        null=True,
        blank=True,
        help_text=_('User who created this event')
    )
    metadata = models.JSONField(
        default=dict,
        blank=True,
        help_text=_('Additional metadata (e.g., refund amount, new times, etc.)')
    )
    notification_sent = models.BooleanField(
        default=False,
        help_text=_('Whether notification was sent for this event')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = _('Booking Event')
        verbose_name_plural = _('Booking Events')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['booking', 'created_at']),
            models.Index(fields=['event_type', 'created_at']),
        ]
    
    def __str__(self):
        return f"Event #{self.event_id} - {self.get_event_type_display()} - Booking #{self.booking.booking_id}"


# ============================================================================
# Sample / Slot tracing (real-time status for user transparency)
# ============================================================================

class SampleTraceStatus(models.TextChoices):
    """Sample lifecycle/tracing status."""
    SAMPLE_SENT = 'SAMPLE_SENT', _('Sample Sent')
    HELD_AT_OFFICE = 'HELD_AT_OFFICE', _('Held at Office')
    FORWARDED_TO_LAB = 'FORWARDED_TO_LAB', _('Forwarded to Lab')
    SAMPLE_ACCEPTED = 'SAMPLE_ACCEPTED', _('Sample Accepted')
    SAMPLE_REJECTED = 'SAMPLE_REJECTED', _('Sample Rejected')
    PROCESSING = 'PROCESSING', _('Processing')
    COMPLETED = 'COMPLETED', _('Analyzed')
    RETURNED = 'RETURNED', _('Returned')
    ARCHIVED = 'ARCHIVED', _('Archived')
    DISPOSED = 'DISPOSED', _('Disposed')
    NOT_UTILIZED = 'NOT_UTILIZED', _('Booking Not Utilized')
    OP_UNAVAILABLE = 'OP_UNAVAILABLE', _('Operator Unavailable')


class BookingSampleTrace(models.Model):
    """One record per sample-trace status update. Timeline for arrow-style display."""
    id = models.AutoField(primary_key=True)
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='sample_trace_events',
        verbose_name=_('Booking'),
    )
    status = models.CharField(
        max_length=20,
        choices=SampleTraceStatus.choices,
        verbose_name=_('Status'),
    )
    sample_identifiers = models.TextField(
        blank=True,
        default='',
        verbose_name=_('Sample identifiers'),
        help_text=_('Optional identifiers when status is Sample Sent'),
    )
    tracking_id = models.TextField(
        blank=True,
        default='',
        verbose_name=_('Tracking ID'),
        help_text=_('Optional: Courier company name and tracking ID when status is Sample Sent'),
    )
    reason = models.TextField(
        blank=True,
        default='',
        verbose_name=_('Reason'),
        help_text=_('Mandatory for Sample Rejected and Held at Office'),
    )
    results_folder_path = models.CharField(
        max_length=1000,
        blank=True,
        default='',
        verbose_name=_('Results folder path'),
        help_text=_('Filesystem path created when status is In Analysis (PROCESSING).'),
    )
    user_reply = models.TextField(
        blank=True,
        default='',
        verbose_name=_('User reply'),
        help_text=_('Booking user reply to the reason (for Sample Rejected or Held at Office)'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='booking_sample_trace_events',
        verbose_name=_('Created by'),
    )

    class Meta:
        verbose_name = _('Booking sample trace')
        verbose_name_plural = _('Booking sample traces')
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['booking', 'created_at']),
        ]

    def __str__(self):
        return f"Booking #{self.booking.booking_id} - {self.get_status_display()} at {self.created_at}"


class BookingSampleTraceReplyAttachment(models.Model):
    """File attachment for a sample trace reply (Held at Office / Sample Rejected)."""
    id = models.AutoField(primary_key=True)
    sample_trace = models.ForeignKey(
        BookingSampleTrace,
        on_delete=models.CASCADE,
        related_name='reply_attachments',
        verbose_name=_('Sample trace event'),
    )
    file = models.FileField(
        upload_to='sample_trace_replies/%Y/%m/%d/',
        verbose_name=_('File'),
    )
    original_name = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Original filename'))
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploaded_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sample_trace_reply_attachments',
        verbose_name=_('Uploaded by'),
    )

    class Meta:
        verbose_name = _('Sample trace reply attachment')
        verbose_name_plural = _('Sample trace reply attachments')
        ordering = ['uploaded_at']

    def __str__(self):
        return f"Attachment for trace #{self.sample_trace_id} - {self.original_name or self.file.name}"


# ============================================================================
# Booking Cancellation Request
# ============================================================================

class BookingCancellationRequestStatus(models.TextChoices):
    """Status choices for booking cancellation requests."""
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    CANCELLED = 'CANCELLED', _('Cancelled')


class BookingCancellationRequest(models.Model):
    """Model for booking cancellation requests with refund."""
    
    booking = models.ForeignKey(
        Booking,
        on_delete=models.CASCADE,
        related_name='cancellation_requests',
        verbose_name=_("Booking"),
        help_text=_("Booking to be cancelled"),
    )
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='booking_cancellation_requests',
        verbose_name=_("User"),
        help_text=_("User requesting cancellation"),
    )
    status = models.CharField(
        max_length=20,
        choices=BookingCancellationRequestStatus.choices,
        default=BookingCancellationRequestStatus.PENDING,
        verbose_name=_("Status"),
        help_text=_("Status of the cancellation request"),
    )
    notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Cancellation Notes"),
        help_text=_("Reason for cancellation"),
    )
    response_message = models.TextField(
        blank=True,
        null=True,
        verbose_name=_("Response Message"),
        help_text=_("Response message from admin"),
    )
    approved_by_email = models.EmailField(
        blank=True,
        null=True,
        verbose_name=_("Approved By Email"),
        help_text=_("Email of the admin who approved/rejected"),
    )
    requested_at = models.DateTimeField(
        auto_now_add=True,
        verbose_name=_("Requested At"),
        help_text=_("When the cancellation request was created"),
    )
    responded_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_("Responded At"),
        help_text=_("When the cancellation request was approved/rejected"),
    )
    
    class Meta:
        verbose_name = _('Booking Cancellation Request')
        verbose_name_plural = _('Booking Cancellation Requests')
        ordering = ['-requested_at']
        indexes = [
            models.Index(fields=['booking', 'status']),
            models.Index(fields=['user', 'status']),
            models.Index(fields=['status', 'requested_at']),
        ]
    
    def __str__(self):
        return f"Cancellation Request #{self.id} - Booking #{self.booking.booking_id} - {self.get_status_display()}"
    
    def approve(self, response_message: str = "") -> None:
        """Approve the cancellation request and process refund."""
        from django.conf import settings
        from django.utils import timezone
        from django.db import transaction
        from iic_booking.users.repositories.wallet_repository import WalletRepository
        
        if self.status != BookingCancellationRequestStatus.PENDING:
            raise ValueError("Request is not in pending status")
        
        # Process cancellation and refund in a transaction
        released_slot_ids = list(self.booking.daily_slots.values_list("id", flat=True))
        with transaction.atomic():
            # Free up slots
            from iic_booking.equipment.maintenance_policy import released_slot_status_after_booking_freed

            self.booking.daily_slots.update(
                booking=None,
                status=released_slot_status_after_booking_freed(self.booking.equipment),
            )
            
            # Process refund
            refund_transaction = None
            refund_target, _ = WalletRepository.get_booking_wallet_target(
                self.booking.user, getattr(self.booking.equipment, "internal_department", None)
            )
            if refund_target:
                from iic_booking.communication.utils import booking_display_id_for_email

                eq_name = (
                    getattr(self.booking.equipment, "name", None)
                    or getattr(self.booking.equipment, "code", None)
                    or ""
                )
                refund_description = (
                    f"Refund for cancelled Booking {booking_display_id_for_email(self.booking)}- {eq_name}"
                )
                if self.notes:
                    refund_description += f" - {self.notes}"
                refund_transaction = refund_target.credit(
                    amount=self.booking.total_charge,
                    description=refund_description,
                    related_user=self.booking.user,
                )
                self.booking.status = BookingStatus.REFUNDED
            else:
                self.booking.status = BookingStatus.CANCELLED
            
            # Update booking notes
            if self.notes:
                self.booking.notes = f"{self.booking.notes or ''}\n[User Cancellation Notes]: {self.notes}".strip()
            
            self.booking.save()
            
            # Update request status
            self.status = BookingCancellationRequestStatus.APPROVED
            self.approved_by_email = getattr(settings, 'ACCOUNTS_EMAIL', 'accounts@iicbooking.iitr.ac.in')
            self.response_message = response_message.strip() if response_message else "Cancellation approved and refund processed."
            self.responded_at = timezone.now()
            self.save()

        # After commit: run waitlist auto-booking so freed slots are visible and bookings/notifications
        # are not nested in the cancellation transaction.
        equipment_for_waitlist = self.booking.equipment
        try:
            from iic_booking.equipment.waitlist import notify_waitlist_slots_available

            notify_waitlist_slots_available(
                equipment_for_waitlist,
                preferred_slot_ids=released_slot_ids,
                respect_reschedule_threshold=False,
            )
        except Exception:
            logger.exception(
                "Failed to notify waitlist after cancellation approval for equipment %s",
                getattr(equipment_for_waitlist, "code", equipment_for_waitlist.pk),
            )

    def reject(self, response_message: str) -> None:
        """Reject the cancellation request."""
        from django.conf import settings
        from django.utils import timezone
        
        if self.status != BookingCancellationRequestStatus.PENDING:
            raise ValueError("Request is not in pending status")
        
        if not response_message or not response_message.strip():
            raise ValueError("Response message is required for rejection")
        
        # Update request status
        self.status = BookingCancellationRequestStatus.REJECTED
        self.approved_by_email = getattr(settings, 'ACCOUNTS_EMAIL', 'accounts@iicbooking.iitr.ac.in')
        self.response_message = response_message.strip()
        self.responded_at = timezone.now()
        self.save()
    
    def cancel(self) -> None:
        """Cancel the cancellation request (by user)."""
        if self.status != BookingCancellationRequestStatus.PENDING:
            raise ValueError("Only pending requests can be cancelled")
        
        self.status = BookingCancellationRequestStatus.CANCELLED
        self.save()


# ============================================================================
# Quota System
# ============================================================================

class QuotaType(models.TextChoices):
    """Quota type choices."""
    WEEKLY = 'WEEKLY', _('Weekly')
    MONTHLY = 'MONTHLY', _('Monthly')


class QuotaLimitType(models.TextChoices):
    """Quota limit type choices."""
    HOURS = 'HOURS', _('Total Hours')
    BOOKINGS = 'BOOKINGS', _('Number of Bookings')
    CHARGE = 'CHARGE', _('Total Charge Amount')


class EquipmentGroupQuota(models.Model):
    """Quota configuration at equipment group level.
    
    Quotas are configured in minutes and separated for:
    - Internal vs External users
    - Individual vs Faculty users
    
    Faculty quotas are shared across all users linked to the same wallet.
    """
    equipment_group = models.ForeignKey(
        EquipmentGroup,
        on_delete=models.CASCADE,
        related_name='quotas',
        verbose_name=_('Equipment Group'),
        help_text=_('Equipment group this quota applies to'),
    )
    quota_type = models.CharField(
        max_length=20,
        choices=QuotaType.choices,
        verbose_name=_('Quota Type'),
        help_text=_('Type of quota (weekly/monthly)'),
    )
    
    # Internal quotas (for STUDENT, INDIVIDUAL_STUDENT, FACULTY)
    internal_individual_quota_minutes = models.IntegerField(
        verbose_name=_('Internal Individual Quota (minutes)'),
        help_text=_('Weekly/monthly quota in minutes for internal individual users (INDIVIDUAL_STUDENT)'),
        default=0,
    )
    internal_faculty_quota_minutes = models.IntegerField(
        verbose_name=_('Internal Faculty Quota (minutes)'),
        help_text=_('Weekly/monthly quota in minutes for internal faculty users. Shared across all users linked to the same wallet.'),
        default=0,
    )
    
    # External quotas (for EXTERNAL, RND, INSTITUTE, OTHER)
    external_individual_quota_minutes = models.IntegerField(
        verbose_name=_('External Individual Quota (minutes)'),
        help_text=_('Weekly/monthly quota in minutes for external individual users'),
        default=0,
    )
    external_faculty_quota_minutes = models.IntegerField(
        verbose_name=_('External Faculty Quota (minutes)'),
        help_text=_('Weekly/monthly quota in minutes for external faculty users. Shared across all users linked to the same wallet.'),
        default=0,
    )
    
    is_enforced = models.BooleanField(
        default=True,
        verbose_name=_('Is Enforced'),
        help_text=_('Whether this quota is enforced'),
    )
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Equipment Group Quota')
        verbose_name_plural = _('Equipment Group Quotas')
        unique_together = [['equipment_group', 'quota_type']]
        ordering = ['equipment_group', 'quota_type']
    
    def __str__(self):
        return f"{self.equipment_group.name} - {self.get_quota_type_display()}"


class UserTypeQuota(models.Model):
    """Quota limits for specific user types."""
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='user_type_quotas',
        help_text=_('Equipment this quota applies to')
    )
    user_type = models.CharField(
        max_length=50,
        help_text=_('User type this quota applies to')
    )
    quota_type = models.CharField(
        max_length=20,
        choices=QuotaType.choices,
        help_text=_('Type of quota (weekly/monthly)')
    )
    limit_type = models.CharField(
        max_length=20,
        choices=QuotaLimitType.choices,
        help_text=_('What this quota limits')
    )
    limit_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_('Quota limit value')
    )
    is_enforced = models.BooleanField(
        default=True,
        help_text=_('Whether this quota is enforced')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('User Type Quota')
        verbose_name_plural = _('User Type Quotas')
        ordering = ['equipment', 'user_type']
    
    def __str__(self):
        return f"{self.equipment.code} - {self.user_type} - {self.quota_type} - {self.limit_type}"


class ExternalUserQuota(models.Model):
    """Quota limits for external users."""
    
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='external_user_quotas',
        help_text=_('Equipment this quota applies to')
    )
    quota_type = models.CharField(
        max_length=20,
        choices=QuotaType.choices,
        help_text=_('Type of quota (weekly/monthly)')
    )
    limit_type = models.CharField(
        max_length=20,
        choices=QuotaLimitType.choices,
        help_text=_('What this quota limits')
    )
    limit_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        help_text=_('Quota limit value')
    )
    is_paid = models.BooleanField(
        default=False,
        help_text=_('Whether this is a paid quota')
    )
    is_enforced = models.BooleanField(
        default=True,
        help_text=_('Whether this quota is enforced')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('External User Quota')
        verbose_name_plural = _('External User Quotas')
        ordering = ['equipment', 'quota_type']
    
    def __str__(self):
        return f"{self.equipment.code} - External - {self.quota_type} - {self.limit_type}"


# class BookingSlot(models.Model):
#     """Many-to-many relationship between bookings and daily slots."""
    
#     booking = models.ForeignKey(
#         Booking,
#         on_delete=models.CASCADE,
#         related_name='booking_slots',
#         help_text=_('Booking this slot belongs to')
#     )
#     daily_slot = models.ForeignKey(
#         DailySlot,
#         on_delete=models.PROTECT,
#         related_name='booking_slots',
#         help_text=_('Daily slot allocated to this booking')
#     )
#     created_at = models.DateTimeField(auto_now_add=True)
    
#     class Meta:
#         verbose_name = _('Booking Slot')
#         verbose_name_plural = _('Booking Slots')
#         unique_together = [['booking', 'daily_slot']]
    
#     def __str__(self):
#         return f"{self.booking} - {self.daily_slot}"


# ============================================================================
# Holiday Calendar
# ============================================================================

# class HolidayType(models.TextChoices):
#     """Holiday type choices."""
#     FULL = 'FULL', _('Full Holiday')
#     PARTIAL = 'PARTIAL', _('Partial Holiday')


# class HolidayCalendar(models.Model):
#     """Holiday calendar for equipment."""
    
#     equipment = models.ForeignKey(
#         Equipment,
#         on_delete=models.CASCADE,
#         related_name='holidays',
#         blank=True,
#         null=True,
#         help_text=_('Equipment this holiday applies to (null = global)')
#     )
#     holiday_name = models.CharField(
#         max_length=255,
#         help_text=_('Name of the holiday')
#     )
#     start_date = models.DateField(
#         help_text=_('Start date of holiday')
#     )
#     end_date = models.DateField(
#         help_text=_('End date of holiday')
#     )
#     holiday_type = models.CharField(
#         max_length=20,
#         choices=HolidayType.choices,
#         default=HolidayType.FULL,
#         help_text=_('Type of holiday')
#     )
#     open_time = models.TimeField(
#         blank=True,
#         null=True,
#         help_text=_('Open time (for partial holidays)')
#     )
#     close_time = models.TimeField(
#         blank=True,
#         null=True,
#         help_text=_('Close time (for partial holidays)')
#     )
#     created_at = models.DateTimeField(auto_now_add=True)
#     updated_at = models.DateTimeField(auto_now=True)
    
#     class Meta:
#         verbose_name = _('Holiday Calendar')
#         verbose_name_plural = _('Holiday Calendar')
#         ordering = ['start_date']
    
#     def __str__(self):
#         equipment_str = self.equipment.code if self.equipment else "Global"
#         return f"{equipment_str} - {self.holiday_name} ({self.start_date} to {self.end_date})"
    
#     def clean(self):
#         """Validate holiday dates and times."""
#         if self.end_date < self.start_date:
#             raise ValidationError(_('End date must be after or equal to start date.'))
#         if self.holiday_type == HolidayType.PARTIAL:
#             if not self.open_time or not self.close_time:
#                 raise ValidationError(_('Open time and close time are required for partial holidays.'))
#             if self.open_time >= self.close_time:
#                 raise ValidationError(_('Close time must be after open time.'))


class Holiday(models.Model):
    """Holiday calendar to manage holidays and non-working days."""
    
    date = models.DateField(
        unique=True,
        help_text=_('Holiday date'),
        verbose_name=_('Date')
    )
    reason = models.CharField(
        max_length=255,
        help_text=_('Reason for the holiday'),
        verbose_name=_('Reason'),
        blank=True,
        null=True
    )
    is_active = models.BooleanField(
        default=True,
        help_text=_('Whether this holiday is active'),
        verbose_name=_('Active')
    )
    color = models.CharField(
        max_length=7,
        blank=True,
        null=True,
        default="#fef3c7",
        help_text=_('Background color for calendar (e.g. #fef3c7). Used in weekly calendar view.'),
        verbose_name=_('Color')
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = _('Holiday')
        verbose_name_plural = _('Holidays')
        ordering = ['-date']
        indexes = [
            models.Index(fields=['date', 'is_active']),
        ]
    
    def __str__(self):
        reason_text = f" - {self.reason}" if self.reason else ""
        return f"{self.date}{reason_text}"
    
    def clean(self):
        """Validate holiday date."""
        if self.date and self.date < timezone.localdate():
            # Allow past holidays for historical records
            pass
    
    @staticmethod
    def is_holiday(check_date):
        """
        Check if a given date is a holiday.
        Includes Saturdays, Sundays and dates in the Holiday table.
        
        Args:
            check_date: date object to check
            
        Returns:
            tuple: (is_holiday: bool, reason: str)
        """
        # Check if it's a Saturday (5) or Sunday (6) (0 = Monday, 6 = Sunday)
        weekday = check_date.weekday()
        if weekday == 5:
            return True, "Saturday"
        if weekday == 6:
            return True, "Sunday"
        
        # Check if it's in the holiday table
        try:
            holiday = Holiday.objects.get(date=check_date, is_active=True)
            return True, holiday.reason or "Holiday"
        except Holiday.DoesNotExist:
            return False, None
    
    @staticmethod
    def get_holidays_in_range(start_date, end_date):
        """
        Get all holidays (including Saturdays and Sundays) in a date range.
        Returns dict: date -> {"reason": str, "color": str}.
        Single DB query for table holidays; Saturdays and Sundays added in memory with default color.
        """
        from datetime import timedelta

        DEFAULT_WEEKEND_COLOR = "#e5e7eb"
        holidays = {}
        # Single query for all rows in range (include color)
        for row in Holiday.objects.filter(
            date__gte=start_date, date__lte=end_date, is_active=True
        ).values_list("date", "reason", "color"):
            d, reason, color = row[0], row[1], row[2]
            holidays[d] = {
                "reason": reason or "Holiday",
                "color": color or "#fef3c7",
            }
        # Add Saturdays and Sundays in range (no DB)
        current_date = start_date
        while current_date <= end_date:
            weekday = current_date.weekday()
            if weekday == 5:  # Saturday
                holidays.setdefault(current_date, {"reason": "Saturday", "color": DEFAULT_WEEKEND_COLOR})
            elif weekday == 6:  # Sunday
                holidays.setdefault(current_date, {"reason": "Sunday", "color": DEFAULT_WEEKEND_COLOR})
            current_date += timedelta(days=1)
        return holidays


class CalendarColorSetting(models.Model):
    """
    Admin-configurable colors for the weekly calendar display.
    Keys: slot statuses (AVAILABLE, BOOKED, BLOCKED, UNDER_MAINTENANCE, OPERATOR_ABSENT)
    and HOLIDAY_DEFAULT for weekends/holidays when no per-holiday color is set.
    """
    key = models.CharField(
        max_length=50,
        unique=True,
        verbose_name=_('Setting key'),
        help_text=_('e.g. AVAILABLE, BOOKED, BLOCKED, UNDER_MAINTENANCE, OPERATOR_ABSENT, HOLIDAY_DEFAULT'),
    )
    value = models.CharField(
        max_length=20,
        default='#e5e7eb',
        verbose_name=_('Hex color'),
        help_text=_('Hex color code (e.g. #dcfce7)'),
    )

    class Meta:
        verbose_name = _('Calendar color setting')
        verbose_name_plural = _('Calendar color settings')
        ordering = ['key']

    def __str__(self):
        return f'{self.key}: {self.value}'


class BookingChargeSetting(models.Model):
    """
    Admin-configurable booking charge settings (e.g. GST for external users).
    Key-value store. EXTERNAL_GST_PERCENT = "18" means 18% GST on base charge for external users.
    """
    key = models.CharField(
        max_length=64,
        unique=True,
        verbose_name=_('Setting key'),
    )
    value = models.CharField(
        max_length=32,
        default='0',
        verbose_name=_('Value'),
    )

    class Meta:
        verbose_name = _('Booking charge setting')
        verbose_name_plural = _('Booking charge settings')
        ordering = ['key']

    def __str__(self):
        return f'{self.key}={self.value}'


class ProformaInvoiceFormat(models.Model):
    """
    Admin-editable format and wording for Proforma Invoice PDFs.
    Single row (singleton): edit in Django Admin to change terms, disclaimer, and optional header/footer.
    """
    terms_and_conditions = models.TextField(
        _('Terms and conditions text'),
        blank=True,
        default='Standard Terms and Conditions available for Standard Proforma Invoices.',
        help_text=_('Shown just after the table. E.g. "Standard Terms and Conditions available for Standard Proforma Invoices."'),
    )
    disclaimer = models.TextField(
        _('Disclaimer text'),
        blank=True,
        default='This is a computer generated invoice and does not require a signature.',
        help_text=_('Shown at bottom. E.g. "This is a computer generated invoice and does not require a signature."'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Proforma invoice format')
        verbose_name_plural = _('Proforma invoice format')

    def __str__(self):
        return 'Proforma Invoice Format'


class InternalUserSlotWindowSetting(models.Model):
    """
    Common slot window setting for all equipment, applied to internal users only.
    When set, internal users see the next week from this weekday+time (e.g. Wednesday 21:00);
    before that they see only current week (+ previous). One row per deployment (singleton).
    """
    reference_weekday = models.SmallIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Slot window reference weekday'),
        help_text=_('Weekday (0=Monday … 6=Sunday) when the next week becomes visible to internal users.'),
        validators=[MinValueValidator(0), MaxValueValidator(6)],
    )
    reference_time = models.TimeField(
        null=True,
        blank=True,
        verbose_name=_('Slot window reference time'),
        help_text=_('Time (24h) on that weekday when the next week opens.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Internal user slot window setting')
        verbose_name_plural = _('Internal user slot window settings')

    def __str__(self):
        if self.reference_weekday is not None and self.reference_time:
            days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            return f'{days[self.reference_weekday]} at {self.reference_time.strftime("%H:%M")}'
        return 'Not set'


class BookingBufferConfig(models.Model):
    """
    Singleton-style config for the daily "Booking Not Utilized" check.
    A scheduled task runs every day at 20:00. For each booked slot whose start_datetime
    is older than (now - buffer_days), if the sample is not yet marked as
    Sample received / Sample rejected / Processing, the slot is marked as Booking Not Utilized,
    and the user is notified by email (no refund).
    Use a single row; buffer_days=0 disables the check.
    """
    buffer_days = models.PositiveIntegerField(
        default=2,
        verbose_name=_('Buffer time (days)'),
        help_text=_(
            'After a booked slot start time, wait this many days before auto-marking as '
            'Booking Not Utilized if sample not received/rejected/processing. Set to 0 to disable.'
        ),
    )
    enabled = models.BooleanField(
        default=True,
        verbose_name=_('Enabled'),
        help_text=_('When unchecked, the daily 20:00 check is skipped.'),
    )
    sample_retention_days = models.PositiveIntegerField(
        default=60,
        verbose_name=_('Sample retention (days)'),
        help_text=_(
            'After the sample is marked "Analyzed" (COMPLETED in trace), wait this many days '
            'before auto-archiving the sample in Sample Lifecycle. Set to 0 to disable auto-archive.'
        ),
    )
    auto_archive_enabled = models.BooleanField(
        default=True,
        verbose_name=_('Auto-archive enabled'),
        help_text=_('When unchecked, the daily auto-archive task is skipped.'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Booking buffer config')
        verbose_name_plural = _('Booking buffer configs')
        ordering = ['pk']

    def __str__(self):
        return f'Buffer {self.buffer_days} day(s), enabled={self.enabled}'


class UrgentHoldExpiryConfig(models.Model):
    """
    Singleton-style config for urgent request validity.
    Urgent request is valid for urgent_booking_validity_days from request creation.
    After this period, PENDING requests are auto-expired (hold released if any, slots freed).
    Admin and OIC control this. One row per deployment.
    """
    hold_expiry_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_('Hold expiry (hours)'),
        help_text=_('Deprecated: use urgent_booking_validity_days. Kept for DB backward compatibility.'),
    )
    urgent_booking_validity_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=1,
        verbose_name=_('Urgent booking validity (days)'),
        help_text=_(
            'Urgent request is valid for this many days from request creation. After this period, '
            'PENDING requests are auto-expired (hold released, slots freed). Admin and OIC can set this.'
        ),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Urgent hold expiry config')
        verbose_name_plural = _('Urgent hold expiry configs')
        ordering = ['pk']

    def __str__(self):
        v = getattr(self, 'urgent_booking_validity_days', None)
        return f'Validity: {v or 1} day(s)'


class Semester(models.Model):
    """Academic semester for organizing student equipment operating nominations."""
    name = models.CharField(
        max_length=100,
        verbose_name=_('Semester Name'),
        help_text=_('e.g. 2024-25 Odd, 2024-25 Even'),
    )
    code = models.CharField(
        max_length=50,
        unique=True,
        verbose_name=_('Semester Code'),
        help_text=_('Short code e.g. 2024-25-Odd'),
    )
    start_date = models.DateField(
        verbose_name=_('Start Date'),
        help_text=_('Semester start date'),
    )
    end_date = models.DateField(
        verbose_name=_('End Date'),
        help_text=_('Semester end date'),
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name=_('Active'),
        help_text=_('Whether this semester is currently active for nominations'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Semester')
        verbose_name_plural = _('Semesters')
        ordering = ['-start_date']

    def __str__(self):
        return self.name


class StudentEquipmentNominationStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')


class EquipmentOperatingTACallStatus(models.TextChoices):
    OPEN = 'OPEN', _('Open')
    CLOSED = 'CLOSED', _('Closed')


class EquipmentOperatingTACall(models.Model):
    """
    OIC or Admin initiates a request for nomination of TA for operating a particular equipment.
    Email is sent to all internal (Faculty) users with instrument details, number of operators required,
    eligibility criteria, duty hours, benefits, and nomination deadline.
    """
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='ta_nomination_calls',
        verbose_name=_('Equipment'),
        help_text=_('Equipment for which TA operators are sought'),
    )
    semester = models.ForeignKey(
        Semester,
        on_delete=models.CASCADE,
        related_name='ta_nomination_calls',
        verbose_name=_('Semester'),
        help_text=_('Semester for which the call applies'),
    )
    number_of_operators_required = models.PositiveIntegerField(
        verbose_name=_('Number of operators required'),
        help_text=_('Number of TA operators needed for this equipment'),
    )
    eligibility_criteria = models.TextField(
        verbose_name=_('Eligibility criteria'),
        help_text=_('Eligibility criteria for nominees'),
        blank=True,
    )
    expected_duty_hours = models.TextField(
        verbose_name=_('Expected duty hours'),
        help_text=_('Expected duty hours for the TA role'),
        blank=True,
    )
    expected_duty_time = models.CharField(
        max_length=255,
        verbose_name=_('Expected duty time (from–to)'),
        help_text=_('Expected duty time window, e.g. 9:30 AM to 5:30 PM'),
        blank=True,
    )
    benefits = models.TextField(
        verbose_name=_('Benefits'),
        help_text=_('Benefits (e.g. certificate, TA recognition, reward points)'),
        blank=True,
    )
    nomination_deadline = models.DateField(
        verbose_name=_('Nomination deadline'),
        help_text=_('Deadline by which faculty must submit nominations'),
    )
    status = models.CharField(
        max_length=20,
        choices=EquipmentOperatingTACallStatus.choices,
        default=EquipmentOperatingTACallStatus.OPEN,
        verbose_name=_('Status'),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_nomination_calls_created',
        verbose_name=_('Created by'),
        help_text=_('OIC or Admin who initiated the call'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    email_sent_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Email sent at'),
        help_text=_('When the call email was sent to all faculty'),
    )

    class Meta:
        verbose_name = _('TA operating nomination call')
        verbose_name_plural = _('TA operating nomination calls')
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.equipment.code} – {self.semester} (TA call)"


class StudentEquipmentNomination(models.Model):
    """
    Supervisor-nominated students allowed to operate equipment for a given semester.
    Only the student's supervisor can create a nomination; admin/OIC can approve or reject.
    """
    student = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='equipment_operating_nominations',
        limit_choices_to={'user_type__in': [UserType.STUDENT, UserType.INDIVIDUAL_STUDENT]},
        verbose_name=_('Student'),
        help_text=_('Student nominated to operate the equipment'),
    )
    supervisor = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='equipment_nominations_given',
        limit_choices_to={'user_type': UserType.FACULTY},
        verbose_name=_('Supervisor'),
        help_text=_('Faculty supervisor who nominated the student'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='student_operating_nominations',
        verbose_name=_('Equipment'),
        help_text=_('Equipment the student is nominated to operate'),
    )
    semester = models.ForeignKey(
        Semester,
        on_delete=models.CASCADE,
        related_name='nominations',
        verbose_name=_('Semester'),
        help_text=_('Semester for which the nomination applies'),
    )
    status = models.CharField(
        max_length=20,
        choices=StudentEquipmentNominationStatus.choices,
        default=StudentEquipmentNominationStatus.PENDING,
        verbose_name=_('Status'),
    )
    remarks = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Remarks'),
        help_text=_('Optional remarks from supervisor or approver'),
    )
    nominated_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(blank=True, null=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='equipment_nominations_approved',
        verbose_name=_('Approved by'),
        help_text=_('Admin or OIC who approved/rejected'),
    )
    ta_call = models.ForeignKey(
        'EquipmentOperatingTACall',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='nominations',
        verbose_name=_('TA call'),
        help_text=_('The TA nomination call this nomination responds to (if any)'),
    )
    resume = models.FileField(
        upload_to='nomination_resumes/%Y/%m/%d/',
        blank=True,
        null=True,
        verbose_name=_('Resume'),
        help_text=_('Resume uploaded by the student for OIC/Admin review'),
    )
    resume_submitted_at = models.DateTimeField(
        blank=True,
        null=True,
        verbose_name=_('Resume submitted at'),
        help_text=_('When the student submitted their resume for this nomination'),
    )

    class Meta:
        verbose_name = _('Student equipment operating nomination')
        verbose_name_plural = _('Student equipment operating nominations')
        ordering = ['-nominated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['student', 'equipment', 'semester'],
                name='equipment_student_equipment_nomination_student_equipment_semester_uniq',
            ),
        ]

    def __str__(self):
        return f"{self.student.email} – {self.equipment.code} ({self.semester})"


class TAAssignmentStatus(models.TextChoices):
    ALLOCATED = 'ALLOCATED', _('Allocated')
    ACCEPTED = 'ACCEPTED', _('Accepted')
    DECLINED = 'DECLINED', _('Declined')
    CANCELLED = 'CANCELLED', _('Cancelled')


class TAAssignment(models.Model):
    """Assignment of a booking duty to an approved TA nomination."""

    nomination = models.ForeignKey(
        StudentEquipmentNomination,
        on_delete=models.PROTECT,
        related_name='ta_assignments',
        verbose_name=_('Approved TA nomination'),
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.PROTECT,
        related_name='ta_assignments',
        verbose_name=_('Booking'),
    )
    ta_student = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='ta_assignments',
        verbose_name=_('TA student'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.PROTECT,
        related_name='ta_assignments',
        verbose_name=_('Equipment'),
    )
    semester = models.ForeignKey(
        Semester,
        on_delete=models.PROTECT,
        related_name='ta_assignments',
        verbose_name=_('Semester/Academic cycle'),
    )
    status = models.CharField(
        max_length=20,
        choices=TAAssignmentStatus.choices,
        default=TAAssignmentStatus.ALLOCATED,
        verbose_name=_('Status'),
    )
    allocation_notes = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Allocation notes'),
    )
    expected_hours = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.00"))],
        verbose_name=_('Expected duty hours'),
    )
    allocated_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_assignments_allocated',
        verbose_name=_('Allocated by'),
    )
    allocated_at = models.DateTimeField(auto_now_add=True, verbose_name=_('Allocated at'))
    responded_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Responded at'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('TA assignment')
        verbose_name_plural = _('TA assignments')
        ordering = ['-allocated_at']
        indexes = [
            models.Index(fields=['ta_student', 'status', 'allocated_at']),
            models.Index(fields=['booking', 'status']),
            models.Index(fields=['equipment', 'allocated_at']),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=['booking', 'ta_student'],
                condition=models.Q(status__in=[TAAssignmentStatus.ALLOCATED, TAAssignmentStatus.ACCEPTED]),
                name='ta_assignment_unique_active_booking_student',
            ),
        ]

    def __str__(self):
        return f"TA Assignment #{self.id} booking={self.booking_id} ta={self.ta_student_id} status={self.status}"


class TARewardConfig(models.Model):
    """Per-equipment configuration for TA reward earning and redemption."""

    equipment = models.OneToOneField(
        Equipment,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='ta_reward_config',
        verbose_name=_('Equipment'),
        help_text=_('If set, this config applies to the specific equipment.'),
    )

    is_enabled = models.BooleanField(
        default=False,
        verbose_name=_('Reward system enabled'),
        help_text=_('Enable/disable TA reward earning and redemption'),
    )
    points_per_duty_hour = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("10.00"),
        verbose_name=_('Points per duty hour'),
    )
    points_per_sample = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_('Points per sample'),
    )
    currency_per_point = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=Decimal("1.0000"),
        verbose_name=_('Currency value per point'),
        help_text=_('Currency discount equivalent of one reward point'),
    )
    max_redeem_percent_per_booking = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("30.00"),
        verbose_name=_('Max redeem percentage per booking'),
    )
    max_redeem_points_per_booking = models.PositiveIntegerField(
        default=300,
        verbose_name=_('Max redeem points per booking'),
    )
    min_booking_amount_for_redeem = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("100.00"),
        verbose_name=_('Minimum booking amount for redemption'),
    )
    expiry_days = models.PositiveIntegerField(
        null=True,
        blank=True,
        default=180,
        verbose_name=_('Point expiry days'),
        help_text=_('If set, earned points expire after this many days'),
    )
    allow_stack_with_other_discounts = models.BooleanField(
        default=True,
        verbose_name=_('Allow stacking with other discounts'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('TA reward config')
        verbose_name_plural = _('TA reward configs')
        ordering = ['-updated_at']

    def __str__(self):
        if self.equipment_id:
            return f"TA Reward Config [{self.equipment.code}] (enabled={self.is_enabled})"
        return f"TA Reward Config [Global Fallback] (enabled={self.is_enabled})"


class TADutyLogStatus(models.TextChoices):
    PENDING = 'PENDING', _('Pending')
    VERIFIED = 'VERIFIED', _('Verified')
    REJECTED = 'REJECTED', _('Rejected')


class TADutyLog(models.Model):
    """Verifiable TA duty record used to grant reward points."""

    nomination = models.ForeignKey(
        StudentEquipmentNomination,
        on_delete=models.PROTECT,
        related_name='duty_logs',
        verbose_name=_('Nomination'),
    )
    student = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='ta_duty_logs',
        verbose_name=_('Student'),
        help_text=_('Student who performed the TA duty'),
    )
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.PROTECT,
        related_name='ta_duty_logs',
        verbose_name=_('Equipment'),
    )
    booking = models.ForeignKey(
        Booking,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_duty_logs',
        verbose_name=_('Related booking'),
    )
    assignment = models.ForeignKey(
        'TAAssignment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='duty_logs',
        verbose_name=_('Related TA assignment'),
    )
    duty_date = models.DateField(verbose_name=_('Duty date'))
    hours_spent = models.DecimalField(
        max_digits=6,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_('Hours spent'),
    )
    samples_processed = models.PositiveIntegerField(
        default=0,
        verbose_name=_('Samples processed'),
    )
    remarks = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Remarks'),
    )
    status = models.CharField(
        max_length=20,
        choices=TADutyLogStatus.choices,
        default=TADutyLogStatus.PENDING,
        verbose_name=_('Status'),
    )
    verified_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_duty_logs_verified',
        verbose_name=_('Verified by'),
    )
    verified_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Verified at'),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_duty_logs_created',
        verbose_name=_('Created by'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('TA duty log')
        verbose_name_plural = _('TA duty logs')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['student', 'status', 'duty_date']),
            models.Index(fields=['equipment', 'duty_date']),
            models.Index(fields=['booking']),
        ]
        constraints = [
            models.CheckConstraint(
                check=models.Q(hours_spent__gt=0) | models.Q(samples_processed__gt=0),
                name='ta_duty_hours_or_samples_positive',
            ),
            models.UniqueConstraint(
                fields=['assignment'],
                condition=models.Q(assignment__isnull=False),
                name='ta_duty_unique_assignment',
            ),
            models.UniqueConstraint(
                fields=['booking', 'student'],
                condition=models.Q(booking__isnull=False),
                name='ta_duty_unique_booking_student',
            ),
        ]

    def __str__(self):
        return f"DutyLog #{self.id} - {self.student.email} - {self.equipment.code}"


class TARewardLedgerEntryType(models.TextChoices):
    EARN = 'EARN', _('Earn')
    REDEEM = 'REDEEM', _('Redeem')
    EXPIRE = 'EXPIRE', _('Expire')
    REVERSE = 'REVERSE', _('Reverse')
    ADJUST = 'ADJUST', _('Adjust')


class TARewardLedgerSourceType(models.TextChoices):
    DUTY_LOG = 'DUTY_LOG', _('TA Duty Log')
    BOOKING = 'BOOKING', _('Booking')
    MANUAL = 'MANUAL', _('Manual')
    EXPIRY = 'EXPIRY', _('Expiry')


class TARewardLedger(models.Model):
    """Immutable ledger for TA reward point accounting."""

    student = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='ta_reward_ledger_entries',
        verbose_name=_('Student'),
    )
    entry_type = models.CharField(
        max_length=20,
        choices=TARewardLedgerEntryType.choices,
        verbose_name=_('Entry type'),
    )
    points = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name=_('Points'),
    )
    currency_value = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=Decimal("0.00"),
        verbose_name=_('Currency value'),
    )
    source_type = models.CharField(
        max_length=20,
        choices=TARewardLedgerSourceType.choices,
        verbose_name=_('Source type'),
    )
    source_id = models.PositiveIntegerField(
        null=True,
        blank=True,
        verbose_name=_('Source ID'),
    )
    description = models.TextField(
        blank=True,
        null=True,
        verbose_name=_('Description'),
    )
    expires_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name=_('Expires at'),
    )
    is_expired = models.BooleanField(
        default=False,
        verbose_name=_('Is expired'),
    )
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='ta_reward_ledger_entries_created',
        verbose_name=_('Created by'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('TA reward ledger entry')
        verbose_name_plural = _('TA reward ledger entries')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['student', 'created_at']),
            models.Index(fields=['student', 'entry_type', 'created_at']),
            models.Index(fields=['source_type', 'source_id']),
        ]

    def __str__(self):
        return f"{self.entry_type} {self.points} pts - {self.student.email}"


class BookingRewardRedemption(models.Model):
    """Tracks reward point redemption applied on a booking."""

    booking = models.OneToOneField(
        Booking,
        on_delete=models.PROTECT,
        related_name='reward_redemption',
        verbose_name=_('Booking'),
    )
    student = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='booking_reward_redemptions',
        verbose_name=_('Student'),
    )
    points_used = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name=_('Points used'),
    )
    discount_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        verbose_name=_('Discount amount'),
    )
    ledger_entry = models.ForeignKey(
        TARewardLedger,
        on_delete=models.PROTECT,
        related_name='booking_redemptions',
        verbose_name=_('Ledger entry'),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Booking reward redemption')
        verbose_name_plural = _('Booking reward redemptions')
        ordering = ['-created_at']

    def __str__(self):
        return f"Booking #{self.booking.booking_id} redeemed {self.points_used} pts"


class InventoryItemCategory(models.TextChoices):
    MAJOR_ASSET = 'MAS', _('Major Asset (MAS)')
    MINOR_OR_LLTA = 'MIA_LLTA', _('Minor Asset (MIA) / Limited Life Time Asset (LLTA)')
    CONSUMABLE_STORES = 'CS', _('Consumable Stores (CS)')


class InventoryRequestType(models.TextChoices):
    CONSUMABLE = 'CONSUMABLE', _('Consumable')
    NON_CONSUMABLE = 'NON_CONSUMABLE', _('Non-consumable')
    MIXED = 'MIXED', _('Mixed')


class InventoryRequestStatus(models.TextChoices):
    DRAFT = 'DRAFT', _('Draft')
    SUBMITTED = 'SUBMITTED', _('Submitted')
    APPROVED = 'APPROVED', _('Approved')
    PARTIALLY_FULFILLED = 'PARTIALLY_FULFILLED', _('Partially fulfilled')
    FULFILLED = 'FULFILLED', _('Fulfilled')
    REJECTED = 'REJECTED', _('Rejected')
    CANCELLED = 'CANCELLED', _('Cancelled')


class InventoryTransactionType(models.TextChoices):
    RECEIPT = 'RECEIPT', _('Receipt')
    ISSUE = 'ISSUE', _('Issue')
    RETURN = 'RETURN', _('Return')
    ADJUSTMENT = 'ADJUSTMENT', _('Adjustment')
    TRANSFER_IN = 'TRANSFER_IN', _('Transfer in')
    TRANSFER_OUT = 'TRANSFER_OUT', _('Transfer out')
    SCRAP = 'SCRAP', _('Scrap')


class IssuedAssetStatus(models.TextChoices):
    IN_USE = 'IN_USE', _('In use')
    RETURNED = 'RETURNED', _('Returned')
    DAMAGED = 'DAMAGED', _('Damaged')
    LOST = 'LOST', _('Lost')
    SCRAPPED = 'SCRAPPED', _('Scrapped')


class InventoryItem(models.Model):
    item_id = models.AutoField(primary_key=True)
    item_code = models.CharField(max_length=100, unique=True, blank=True, editable=False, verbose_name=_('Item code'))
    name = models.CharField(max_length=255, verbose_name=_('Item name'))
    category = models.CharField(
        max_length=20,
        choices=InventoryItemCategory.choices,
        verbose_name=_('Category'),
        help_text=_(
            'MAS: Major Assets (long-term durable/high-value assets). '
            'MIA/LLTA: Minor/Limited-life assets (typically 4-5 years). '
            'CS: Consumable Stores (used up quickly or negligible resale value).'
        ),
    )
    uom = models.CharField(max_length=50, verbose_name=_('Unit of measure'))
    specification = models.TextField(blank=True, default='', verbose_name=_('Specification'))
    active = models.BooleanField(default=True, verbose_name=_('Active'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Inventory item')
        verbose_name_plural = _('Inventory items')
        ordering = ['name']
        indexes = [
            models.Index(fields=['category', 'active']),
        ]

    def __str__(self):
        return f"{self.item_code} - {self.name}"

    @staticmethod
    def _next_item_code():
        prefix = "IIC-"
        last_code = (
            InventoryItem.objects
            .filter(item_code__startswith='IIC')
            .order_by('-item_code')
            .values_list('item_code', flat=True)
            .first()
        )
        if not last_code:
            seq = 1
        else:
            try:
                match = re.search(r'IIC-?(\d+)$', last_code)
                seq = int(match.group(1)) + 1 if match else 1
            except Exception:
                seq = 1
        return f"{prefix}{seq:04d}"

    def save(self, *args, **kwargs):
        if not self.item_code:
            for _ in range(3):
                candidate = self._next_item_code()
                if not InventoryItem.objects.filter(item_code=candidate).exists():
                    self.item_code = candidate
                    break
            if not self.item_code:
                self.item_code = f"IIC-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        super().save(*args, **kwargs)


class EquipmentInventoryItem(models.Model):
    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='inventory_items',
        verbose_name=_('Equipment'),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='equipment_mappings',
        verbose_name=_('Item'),
    )
    min_level = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    max_level = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    reorder_level = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    critical_level = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    default_store_location = models.CharField(max_length=255, blank=True, default='')
    is_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Equipment inventory item')
        verbose_name_plural = _('Equipment inventory items')
        unique_together = [['equipment', 'item']]
        indexes = [
            models.Index(fields=['equipment', 'is_enabled']),
            models.Index(fields=['item', 'is_enabled']),
        ]

    def __str__(self):
        return f"{self.equipment.code} - {self.item.item_code}"


class EquipmentItemStock(models.Model):
    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='inventory_stock_balances',
        verbose_name=_('Equipment'),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='stock_balances',
        verbose_name=_('Item'),
    )
    current_qty = models.DecimalField(max_digits=14, decimal_places=3, default=Decimal('0.000'))
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Equipment item stock')
        verbose_name_plural = _('Equipment item stocks')
        unique_together = [['equipment', 'item']]
        indexes = [
            models.Index(fields=['equipment', 'item']),
            models.Index(fields=['item', 'current_qty']),
        ]

    def __str__(self):
        return f"{self.equipment.code} - {self.item.item_code}: {self.current_qty}"


class InventoryRequest(models.Model):
    request_id = models.AutoField(primary_key=True)
    request_no = models.CharField(max_length=100, unique=True, blank=True, editable=False, verbose_name=_('Request number'))
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='inventory_requests',
        verbose_name=_('Equipment'),
    )
    requested_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='inventory_requests_raised',
        verbose_name=_('Requested by'),
    )
    request_type = models.CharField(
        max_length=20,
        choices=InventoryRequestType.choices,
        default=InventoryRequestType.MIXED,
        verbose_name=_('Request type'),
    )
    status = models.CharField(
        max_length=30,
        choices=InventoryRequestStatus.choices,
        default=InventoryRequestStatus.DRAFT,
        verbose_name=_('Status'),
    )
    justification = models.TextField(blank=True, default='', verbose_name=_('Justification'))
    required_by_date = models.DateField(null=True, blank=True, verbose_name=_('Required by date'))
    submitted_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Submitted at'))
    decision_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='inventory_requests_decided',
        verbose_name=_('Decision by'),
    )
    decision_at = models.DateTimeField(null=True, blank=True, verbose_name=_('Decision at'))
    decision_note = models.TextField(blank=True, default='', verbose_name=_('Decision note'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Inventory request')
        verbose_name_plural = _('Inventory requests')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['equipment', 'status', 'created_at']),
            models.Index(fields=['requested_by', 'status', 'created_at']),
        ]

    def __str__(self):
        return f"{self.request_no} - {self.equipment.code} - {self.status}"

    @staticmethod
    def is_user_authorized_for_equipment(user, equipment, at_time=None):
        """Return True when user is current OIC for equipment (primary or active delegated)."""
        if not user or not getattr(user, 'pk', None) or not equipment or not getattr(equipment, 'pk', None):
            return False
        check_time = at_time or timezone.now()

        is_primary = EquipmentManager.objects.filter(equipment=equipment, manager=user).exists()
        if is_primary:
            return True

        # Active temporary OIC delegation where the primary OIC is still mapped to this equipment.
        return EquipmentTemporaryOIC.objects.filter(
            equipment=equipment,
            temporary_oic=user,
            resume_at__gt=check_time,
        ).filter(
            primary_oic__equipment_manager__equipment=equipment,
        ).exists()

    def clean(self):
        super().clean()
        if self.requested_by_id and self.equipment_id:
            if not self.is_user_authorized_for_equipment(self.requested_by, self.equipment):
                raise ValidationError(_('Requested by user is not authorized as OIC for this equipment.'))

    @staticmethod
    def _next_request_no():
        prefix = timezone.now().strftime("INVR-%Y%m%d-")
        last_for_day = (
            InventoryRequest.objects
            .filter(request_no__startswith=prefix)
            .order_by('-request_no')
            .values_list('request_no', flat=True)
            .first()
        )
        if not last_for_day:
            seq = 1
        else:
            try:
                seq = int(last_for_day.rsplit('-', 1)[-1]) + 1
            except Exception:
                seq = 1
        return f"{prefix}{seq:04d}"

    def save(self, *args, **kwargs):
        if not self.request_no:
            # Retry on very unlikely unique collision under concurrency.
            for _ in range(3):
                candidate = self._next_request_no()
                if not InventoryRequest.objects.filter(request_no=candidate).exists():
                    self.request_no = candidate
                    break
            if not self.request_no:
                self.request_no = f"INVR-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        super().save(*args, **kwargs)


class InventoryRequestLine(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(
        InventoryRequest,
        on_delete=models.CASCADE,
        related_name='lines',
        verbose_name=_('Request'),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='request_lines',
        verbose_name=_('Item'),
    )
    requested_qty = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    approved_qty = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    issued_qty = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    estimated_unit_cost = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        verbose_name=_('Estimated unit cost'),
    )
    remarks = models.TextField(blank=True, default='', verbose_name=_('Remarks'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Inventory request line')
        verbose_name_plural = _('Inventory request lines')
        unique_together = [['request', 'item']]
        indexes = [
            models.Index(fields=['request', 'item']),
            models.Index(fields=['item']),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(requested_qty__gte=0), name='inv_req_line_requested_gte_0'),
            models.CheckConstraint(check=models.Q(approved_qty__gte=0), name='inv_req_line_approved_gte_0'),
            models.CheckConstraint(check=models.Q(issued_qty__gte=0), name='inv_req_line_issued_gte_0'),
            models.CheckConstraint(check=models.Q(approved_qty__lte=models.F('requested_qty')), name='inv_req_line_approved_lte_requested'),
            models.CheckConstraint(check=models.Q(issued_qty__lte=models.F('approved_qty')), name='inv_req_line_issued_lte_approved'),
        ]

    def __str__(self):
        return f"{self.request.request_no} - {self.item.item_code}"


class InventoryTransaction(models.Model):
    transaction_id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='inventory_transactions',
        verbose_name=_('Equipment'),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='inventory_transactions',
        verbose_name=_('Item'),
    )
    tx_type = models.CharField(max_length=20, choices=InventoryTransactionType.choices, verbose_name=_('Transaction type'))
    quantity = models.DecimalField(max_digits=12, decimal_places=3, verbose_name=_('Quantity'))
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, verbose_name=_('Unit cost'))
    batch_no = models.CharField(max_length=100, blank=True, default='', verbose_name=_('Batch number'))
    expiry_date = models.DateField(null=True, blank=True, verbose_name=_('Expiry date'))
    reference_type = models.CharField(max_length=50, blank=True, default='', verbose_name=_('Reference type'))
    reference_id = models.CharField(max_length=100, blank=True, default='', verbose_name=_('Reference ID'))
    performed_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name='inventory_transactions_performed',
        verbose_name=_('Performed by'),
    )
    performed_at = models.DateTimeField(default=timezone.now, verbose_name=_('Performed at'))
    remarks = models.TextField(blank=True, default='', verbose_name=_('Remarks'))
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('Inventory transaction')
        verbose_name_plural = _('Inventory transactions')
        ordering = ['-performed_at', '-transaction_id']
        indexes = [
            models.Index(fields=['equipment', 'item', 'performed_at']),
            models.Index(fields=['tx_type', 'performed_at']),
            models.Index(fields=['reference_type', 'reference_id']),
        ]
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='inv_tx_quantity_gt_0'),
        ]

    def __str__(self):
        return f"{self.equipment.code} - {self.item.item_code} - {self.tx_type} ({self.quantity})"

    @property
    def signed_quantity(self):
        if self.tx_type in {
            InventoryTransactionType.RECEIPT,
            InventoryTransactionType.RETURN,
            InventoryTransactionType.TRANSFER_IN,
        }:
            return self.quantity
        return -self.quantity

    def _apply_delta_to_stock(self, delta):
        stock, _ = EquipmentItemStock.objects.select_for_update().get_or_create(
            equipment=self.equipment,
            item=self.item,
            defaults={'current_qty': Decimal('0.000')},
        )
        stock.current_qty = (stock.current_qty or Decimal('0.000')) + delta
        stock.save(update_fields=['current_qty', 'updated_at'])

    def save(self, *args, **kwargs):
        with transaction.atomic():
            old_instance = None
            if self.pk:
                old_instance = InventoryTransaction.objects.select_for_update().filter(pk=self.pk).first()
            super().save(*args, **kwargs)

            # Revert previous effect (update case), then apply new effect.
            if old_instance:
                self._apply_delta_to_stock(-old_instance.signed_quantity)
            self._apply_delta_to_stock(self.signed_quantity)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            self._apply_delta_to_stock(-self.signed_quantity)
            return super().delete(*args, **kwargs)


class IssuedAsset(models.Model):
    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(
        Equipment,
        on_delete=models.CASCADE,
        related_name='issued_assets',
        verbose_name=_('Equipment'),
    )
    item = models.ForeignKey(
        InventoryItem,
        on_delete=models.PROTECT,
        related_name='issued_assets',
        verbose_name=_('Item'),
    )
    serial_no = models.CharField(max_length=255, blank=True, default='', verbose_name=_('Serial number'))
    issued_to = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='assets_issued_to',
        verbose_name=_('Issued to'),
    )
    issued_on = models.DateField(default=timezone.now, verbose_name=_('Issued on'))
    expected_return_on = models.DateField(null=True, blank=True, verbose_name=_('Expected return on'))
    returned_on = models.DateField(null=True, blank=True, verbose_name=_('Returned on'))
    condition_on_issue = models.TextField(blank=True, default='', verbose_name=_('Condition on issue'))
    condition_on_return = models.TextField(blank=True, default='', verbose_name=_('Condition on return'))
    status = models.CharField(
        max_length=20,
        choices=IssuedAssetStatus.choices,
        default=IssuedAssetStatus.IN_USE,
        verbose_name=_('Status'),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('Issued asset')
        verbose_name_plural = _('Issued assets')
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['equipment', 'status']),
            models.Index(fields=['item', 'status']),
            models.Index(fields=['serial_no']),
        ]

    def __str__(self):
        serial = self.serial_no or 'NA'
        return f"{self.equipment.code} - {self.item.item_code} - {serial}"


class ProcurementRequestStatus(models.TextChoices):
    DRAFT = 'DRAFT', _('Draft')
    SUBMITTED = 'SUBMITTED', _('Submitted by initiator')
    PENDING_OIC_REVIEW = 'PENDING_OIC_REVIEW', _('Pending Lab OIC review')
    UNDER_OFFICE_VERIFICATION = 'UNDER_OFFICE_VERIFICATION', _('Under office verification')
    OFFICE_VERIFIED = 'OFFICE_VERIFIED', _('Office verified')
    PENDING_STORE_APPROVAL = 'PENDING_STORE_APPROVAL', _('Pending store approval')
    STORE_APPROVED = 'STORE_APPROVED', _('Store approved')
    PENDING_HEAD_APPROVAL_EMAIL = 'PENDING_HEAD_APPROVAL_EMAIL', _('Pending head approval (email)')
    PENDING_HEAD_APPROVAL_OFFLINE = 'PENDING_HEAD_APPROVAL_OFFLINE', _('Pending head approval (offline)')
    HEAD_APPROVED = 'HEAD_APPROVED', _('Head approved')
    PURCHASE_IN_PROGRESS = 'PURCHASE_IN_PROGRESS', _('Purchase in progress')
    PURCHASE_COMPLETED_PENDING_OFFICE_SEEN = 'PURCHASE_COMPLETED_PENDING_OFFICE_SEEN', _('Purchase completed pending office seen')
    OFFICE_SEEN_COMPLETED = 'OFFICE_SEEN_COMPLETED', _('Office seen and completed')
    REJECTED_BY_OFFICE = 'REJECTED_BY_OFFICE', _('Rejected by office')
    REJECTED_BY_STORE = 'REJECTED_BY_STORE', _('Rejected by store')
    REJECTED_BY_HEAD = 'REJECTED_BY_HEAD', _('Rejected by head')
    REJECTED_BY_OIC = 'REJECTED_BY_OIC', _('Rejected by Lab OIC')
    CANCELLED = 'CANCELLED', _('Cancelled')


class ProcurementHeadApprovalMode(models.TextChoices):
    OFFLINE = 'OFFLINE', _('Offline')
    EMAIL = 'EMAIL', _('Email')
    NOT_REQUIRED = 'NOT_REQUIRED', _('Not required')


class ProcurementAttachmentType(models.TextChoices):
    ESTIMATE_COPY = 'ESTIMATE_COPY', _('Estimate copy')
    HEAD_APPROVAL_SCAN = 'HEAD_APPROVAL_SCAN', _('Head approval scan')
    INVOICE = 'INVOICE', _('Invoice')
    OTHER = 'OTHER', _('Other')


class ProcurementActionType(models.TextChoices):
    SUBMITTED = 'SUBMITTED', _('Submitted')
    OIC_ENDORSED = 'OIC_ENDORSED', _('Lab OIC endorsed')
    OIC_REJECTED = 'OIC_REJECTED', _('Lab OIC rejected')
    OFFICE_VERIFIED = 'OFFICE_VERIFIED', _('Office verified')
    OFFICE_REJECTED = 'OFFICE_REJECTED', _('Office rejected')
    STORE_APPROVED = 'STORE_APPROVED', _('Store approved')
    STORE_REJECTED = 'STORE_REJECTED', _('Store rejected')
    HEAD_APPROVED = 'HEAD_APPROVED', _('Head approved')
    HEAD_REJECTED = 'HEAD_REJECTED', _('Head rejected')
    PURCHASE_MARKED_COMPLETE = 'PURCHASE_MARKED_COMPLETE', _('Purchase marked complete')
    OFFICE_SEEN = 'OFFICE_SEEN', _('Office seen')


class ProcurementRequest(models.Model):
    request_id = models.AutoField(primary_key=True)
    request_no = models.CharField(max_length=100, unique=True, blank=True, editable=False, verbose_name=_('Request number'))
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='procurement_requests')
    initiated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='procurement_requests_initiated')
    status = models.CharField(
        max_length=50,
        choices=ProcurementRequestStatus.choices,
        default=ProcurementRequestStatus.DRAFT,
    )
    head_approval_required = models.BooleanField(default=False)
    head_approval_mode = models.CharField(
        max_length=20,
        choices=ProcurementHeadApprovalMode.choices,
        default=ProcurementHeadApprovalMode.NOT_REQUIRED,
    )
    total_estimated_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    remarks = models.TextField(blank=True, default='')
    oic_endorsed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_oic_endorsed')
    oic_endorsed_at = models.DateTimeField(null=True, blank=True)
    office_verified_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_office_verified')
    office_verified_at = models.DateTimeField(null=True, blank=True)
    store_approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_store_approved')
    store_approved_at = models.DateTimeField(null=True, blank=True)
    head_approved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_head_approved')
    head_approved_at = models.DateTimeField(null=True, blank=True)
    purchase_completed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_purchase_completed')
    purchase_completed_at = models.DateTimeField(null=True, blank=True)
    office_seen_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_requests_office_seen')
    office_seen_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['equipment', 'status', 'created_at']),
            models.Index(fields=['initiated_by', 'status', 'created_at']),
        ]

    @staticmethod
    def _next_request_no():
        prefix = timezone.now().strftime("PR-%Y%m%d-")
        last_for_day = (
            ProcurementRequest.objects
            .filter(request_no__startswith=prefix)
            .order_by('-request_no')
            .values_list('request_no', flat=True)
            .first()
        )
        if not last_for_day:
            seq = 1
        else:
            try:
                seq = int(last_for_day.rsplit('-', 1)[-1]) + 1
            except Exception:
                seq = 1
        return f"{prefix}{seq:04d}"

    def save(self, *args, **kwargs):
        if not self.request_no:
            self.request_no = self._next_request_no()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.request_no} - {self.status}"


class ProcurementRequestLine(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(ProcurementRequest, on_delete=models.CASCADE, related_name='lines')
    item_master = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_lines')
    finalized_item_master = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_lines_finalized')
    manual_item_name = models.CharField(max_length=255, blank=True, default='')
    classification = models.CharField(max_length=20, choices=InventoryItemCategory.choices)
    office_corrected_name = models.CharField(max_length=255, blank=True, default='')
    office_corrected_classification = models.CharField(max_length=20, choices=InventoryItemCategory.choices, blank=True, default='')
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal('0.000'))
    tentative_unit_cost = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal('0.00'))
    tentative_total_cost = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['id']
        constraints = [
            models.CheckConstraint(check=models.Q(quantity__gt=0), name='proc_line_qty_gt_0'),
            models.CheckConstraint(check=models.Q(tentative_unit_cost__gte=0), name='proc_line_unit_cost_gte_0'),
        ]

    def save(self, *args, **kwargs):
        self.tentative_total_cost = (self.quantity or Decimal('0.000')) * (self.tentative_unit_cost or Decimal('0.00'))
        super().save(*args, **kwargs)


class ProcurementAttachment(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(ProcurementRequest, on_delete=models.CASCADE, related_name='attachments')
    line = models.ForeignKey(ProcurementRequestLine, on_delete=models.CASCADE, null=True, blank=True, related_name='attachments')
    attachment_type = models.CharField(max_length=30, choices=ProcurementAttachmentType.choices)
    file = models.FileField(upload_to='procurement/%Y/%m/%d/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_attachments_uploaded')
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-uploaded_at']


class ProcurementActionLog(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(ProcurementRequest, on_delete=models.CASCADE, related_name='action_logs')
    action = models.CharField(max_length=40, choices=ProcurementActionType.choices)
    by_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='procurement_actions_done')
    comments = models.TextField(blank=True, default='')
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


# --- Equipment lifecycle: AMC, expenses, write-off ---------------------------------


class EquipmentExpenseType(models.TextChoices):
    AMC = 'AMC', _('AMC / service contract payment')
    CALIBRATION = 'CALIBRATION', _('Calibration')
    REPAIR = 'REPAIR', _('Repair')
    CONSUMABLE = 'CONSUMABLE', _('Consumable (direct)')
    PROCUREMENT_LINKED = 'PROCUREMENT_LINKED', _('Linked to procurement request')
    OTHER = 'OTHER', _('Other')


class EquipmentAMCContract(models.Model):
    """Annual Maintenance Contract (or similar) attached to an equipment."""

    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='amc_contracts')
    vendor_name = models.CharField(max_length=255)
    contract_reference = models.CharField(max_length=120, blank=True, default='')
    start_date = models.DateField()
    end_date = models.DateField()
    contract_value = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal('0.00'))
    coverage_notes = models.TextField(blank=True, default='')
    contract_document = models.FileField(upload_to='equipment_amc/%Y/%m/%d/', blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='amc_contracts_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-start_date']
        indexes = [models.Index(fields=['equipment', 'is_active', 'end_date'])]


class EquipmentExpense(models.Model):
    """Recorded spend against an equipment (AMC, calibration, repairs, consumables, etc.)."""

    id = models.AutoField(primary_key=True)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='equipment_expenses')
    expense_type = models.CharField(max_length=30, choices=EquipmentExpenseType.choices, default=EquipmentExpenseType.OTHER)
    classification = models.CharField(
        max_length=20,
        choices=InventoryItemCategory.choices,
        blank=True,
        default='',
        help_text=_('Consumable / minor / major — optional tag'),
    )
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    expense_date = models.DateField()
    description = models.TextField(blank=True, default='')
    procurement_request = models.ForeignKey(
        ProcurementRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='linked_expenses',
    )
    amc_contract = models.ForeignKey(
        EquipmentAMCContract,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='expense_entries',
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='equipment_expenses_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-expense_date', '-id']
        indexes = [models.Index(fields=['equipment', 'expense_date'])]


class EquipmentWriteOffStatus(models.TextChoices):
    PENDING_OFFICE = 'PENDING_OFFICE', _('Pending Office Superintendent')
    PENDING_STORE = 'PENDING_STORE', _('Pending Store In Charge')
    PENDING_HEAD = 'PENDING_HEAD', _('Pending Head of Department')
    APPROVED = 'APPROVED', _('Approved')
    REJECTED = 'REJECTED', _('Rejected')
    EXECUTED = 'EXECUTED', _('Executed (asset written off)')
    CANCELLED = 'CANCELLED', _('Cancelled')


class EquipmentWriteOffRequest(models.Model):
    """
    Write-off proposal for limited-life / major assets etc., initiated by Lab OIC.
    Chain: Office Superintendent → Store In Charge → Head of Department → execution.
    """

    id = models.AutoField(primary_key=True)
    request_no = models.CharField(max_length=100, unique=True, blank=True, editable=False)
    equipment = models.ForeignKey(Equipment, on_delete=models.CASCADE, related_name='write_off_requests')
    initiated_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name='write_off_requests_initiated')
    reason = models.TextField()
    asset_classification = models.CharField(max_length=20, choices=InventoryItemCategory.choices, blank=True, default='')
    estimated_residual_value = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    status = models.CharField(
        max_length=30,
        choices=EquipmentWriteOffStatus.choices,
        default=EquipmentWriteOffStatus.PENDING_OFFICE,
    )
    office_reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='write_off_office_reviewed')
    office_reviewed_at = models.DateTimeField(null=True, blank=True)
    store_reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='write_off_store_reviewed')
    store_reviewed_at = models.DateTimeField(null=True, blank=True)
    head_reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='write_off_head_reviewed')
    head_reviewed_at = models.DateTimeField(null=True, blank=True)
    executed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='write_off_executed')
    executed_at = models.DateTimeField(null=True, blank=True)
    rejection_comments = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['equipment', 'status', 'created_at']),
            models.Index(fields=['initiated_by', 'status']),
        ]

    @staticmethod
    def _next_request_no():
        prefix = timezone.now().strftime('WO-%Y%m%d-')
        last_for_day = (
            EquipmentWriteOffRequest.objects.filter(request_no__startswith=prefix).order_by('-request_no').values_list('request_no', flat=True).first()
        )
        if not last_for_day:
            seq = 1
        else:
            try:
                seq = int(last_for_day.rsplit('-', 1)[-1]) + 1
            except Exception:
                seq = 1
        return f'{prefix}{seq:04d}'

    def save(self, *args, **kwargs):
        if not self.request_no:
            self.request_no = self._next_request_no()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.request_no} — {self.status}'


class EquipmentWriteOffActionType(models.TextChoices):
    SUBMITTED = 'SUBMITTED', _('Submitted')
    OFFICE_FORWARDED = 'OFFICE_FORWARDED', _('Office forwarded')
    OFFICE_REJECTED = 'OFFICE_REJECTED', _('Office rejected')
    STORE_FORWARDED = 'STORE_FORWARDED', _('Store forwarded')
    STORE_REJECTED = 'STORE_REJECTED', _('Store rejected')
    HEAD_APPROVED = 'HEAD_APPROVED', _('Head approved')
    HEAD_REJECTED = 'HEAD_REJECTED', _('Head rejected')
    EXECUTED = 'EXECUTED', _('Marked executed')
    CANCELLED = 'CANCELLED', _('Cancelled')


class EquipmentWriteOffActionLog(models.Model):
    id = models.AutoField(primary_key=True)
    request = models.ForeignKey(EquipmentWriteOffRequest, on_delete=models.CASCADE, related_name='action_logs')
    action = models.CharField(max_length=40, choices=EquipmentWriteOffActionType.choices)
    by_user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='write_off_actions')
    comments = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']


class EquipmentAdditionRequestStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    APPROVED = "APPROVED", _("Approved")
    REJECTED = "REJECTED", _("Rejected")


def equipment_addition_image_upload_to(instance, filename):
    ext = os.path.splitext(filename)[1].lower() or ".jpg"
    if ext and not ext.startswith("."):
        ext = "." + ext
    unique = uuid.uuid4().hex[:12]
    return f"equipment_addition_requests/images/{unique}{ext}"


def equipment_addition_document_upload_to(instance, filename):
    safe = get_valid_filename(filename)[:120] or "document"
    unique = uuid.uuid4().hex[:12]
    return f"equipment_addition_requests/documents/{unique}_{safe}"


class EquipmentAdditionRequest(models.Model):
    """Public proposal to add equipment; Admin approves before a real Equipment row is created."""

    id = models.AutoField(primary_key=True)
    status = models.CharField(
        max_length=20,
        choices=EquipmentAdditionRequestStatus.choices,
        default=EquipmentAdditionRequestStatus.PENDING,
        db_index=True,
    )
    name = models.CharField(max_length=255, help_text=_("Proposed equipment name"))
    code = models.CharField(max_length=255, help_text=_("Proposed equipment code"))
    description = models.TextField(blank=True, default="")
    make = models.CharField(max_length=255, blank=True, default="")
    model_information = models.CharField(max_length=255, blank=True, default="")
    year_of_installation = models.CharField(max_length=20, blank=True, default="")
    location = models.TextField(blank=True, default="")
    specifications = models.TextField(blank=True, default="")
    sample_requirements = models.TextField(
        blank=True,
        default="",
        verbose_name=_("Sample requirements and preparation"),
    )
    slots_per_day = models.PositiveIntegerField(null=True, blank=True)
    slot_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    slot_start_time = models.TimeField(null=True, blank=True)
    slot_end_time = models.TimeField(null=True, blank=True)
    charge_calculation_basis = models.TextField(blank=True, default="")
    time_calculation_basis = models.TextField(blank=True, default="")
    charge_iitr_student = models.CharField(max_length=255, blank=True, default="")
    charge_iitr_faculty = models.CharField(max_length=255, blank=True, default="")
    charge_external_educational_student = models.CharField(max_length=255, blank=True, default="")
    charge_external_govt_rnd = models.CharField(max_length=255, blank=True, default="")
    charge_industry = models.CharField(max_length=255, blank=True, default="")
    charge_startup_incubated_iitr = models.CharField(max_length=255, blank=True, default="")
    charge_external_startup_msme = models.CharField(max_length=255, blank=True, default="")
    equipment_image = models.ImageField(
        upload_to=equipment_addition_image_upload_to,
        blank=True,
        null=True,
        max_length=512,
        verbose_name=_("Equipment image"),
    )
    supporting_document = models.FileField(
        upload_to=equipment_addition_document_upload_to,
        blank=True,
        null=True,
        max_length=512,
        verbose_name=_("Supporting document"),
    )
    internal_department = models.ForeignKey(
        Department,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="equipment_addition_requests",
        limit_choices_to={
            "department_type": DepartmentType.INTERNAL,
            "internal_subcategory": InternalDepartmentSubcategory.IIT_ROORKEE_DEPT_CENTRES,
        },
        verbose_name=_("Internal department"),
    )
    proposed_oic_name = models.CharField(max_length=255, blank=True, default="")
    proposed_oic_email = models.EmailField(blank=True, default="")
    proposed_operator_name = models.CharField(max_length=255, blank=True, default="")
    proposed_operator_email = models.EmailField(blank=True, default="")
    submitter_name = models.CharField(max_length=255)
    submitter_email = models.EmailField()
    submitter_phone = models.CharField(max_length=40, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="equipment_addition_reviews",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default="")
    created_equipment = models.ForeignKey(
        Equipment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="addition_requests",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Equipment addition request")
        verbose_name_plural = _("Equipment addition requests")

    def __str__(self):
        return f"{self.code} — {self.name} ({self.status})"
