from django import forms
from django.contrib import admin, messages
from django.contrib.admin import SimpleListFilter
from django.core.files.storage import default_storage
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _
from django.urls import reverse
from django.db import models
import json
import re
import logging
from iic_booking.users.models.user_type import UserType

logger = logging.getLogger(__name__)
from .image_utils import save_local_equipment_image_backup
from .models import (
    Equipment,
    EquipmentCategory,
    EquipmentGroup,
    EquipmentGroupQuota,
    EquipmentManager,
    EquipmentOperator,
    ICPMSStandardSample,
    EquipmentSpecification,
    EquipmentAccessory,
    EquipmentAdditionalAccessory,
    ChargeProfile, ChargeProfilePricingProfile, DynamicInputField, DynamicInputFieldType, MultiParamDefinition,
    PrintMaterial,
    SlotMaster,
    DailySlot,
    Booking,
    RepeatSampleRequest,
    BookingAttemptLog,
    Holiday,
    SlotStatus,
    QuotaType,
    BookingBufferConfig,
    InternalUserSlotWindowSetting,
    ProformaInvoiceFormat,
    Semester,
    StudentEquipmentNomination,
    StudentEquipmentNominationStatus,
    EquipmentOperatingTACall,
    EquipmentOperatingTACallStatus,
    InventoryItem,
    EquipmentInventoryItem,
    EquipmentItemStock,
    InventoryRequest,
    InventoryRequestLine,
    InventoryTransaction,
    IssuedAsset,
    BookingChargeSetting,
)
from decimal import Decimal


@admin.register(BookingChargeSetting)
class BookingChargeSettingAdmin(admin.ModelAdmin):
    list_display = ("key", "value")
    search_fields = ("key",)
    ordering = ("key",)


@admin.register(ICPMSStandardSample)
class ICPMSStandardSampleAdmin(admin.ModelAdmin):
    list_display = ("s_no", "part_no", "name_of_std", "concentration", "status", "updated_at")
    list_filter = ("status",)
    search_fields = ("s_no", "part_no", "name_of_std", "list_of_elements", "concentration")
    ordering = ("id",)


class EquipmentAdminForm(forms.ModelForm):
    """Equipment admin form; image uses the model ImageField (stored in the configured media backend)."""

    class Meta:
        model = Equipment
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "image" in self.fields:
            self.fields["image"].help_text = _(
                "Photo shown on the booking site. Upload a file to set or replace; use the clear checkbox only to remove the image."
            )

class EquipmentManagerInlineForm(forms.ModelForm):
    """Form for Equipment Office in Charge inline; officer dropdown shows name instead of email."""
    class Meta:
        model = EquipmentManager
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'manager' in self.fields:
            from iic_booking.users.models.user import User
            qs = User.objects.filter(
                user_type=UserType.MANAGER,
                is_active=True
            ).order_by('name', 'email')
            self.fields['manager'].queryset = qs
            self.fields['manager'].label_from_instance = lambda obj: obj.name or obj.email or str(obj)


class EquipmentManagerInline(admin.TabularInline):
    """Inline admin for Equipment Office in Charge (Officers in Charge)."""
    model = EquipmentManager
    form = EquipmentManagerInlineForm
    extra = 0
    fields = ['manager', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    classes = ['collapse']

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Filter manager field to only show users with manager user type."""
        if db_field.name == "manager":
            from iic_booking.users.models.user import User
            kwargs["queryset"] = User.objects.filter(
                user_type=UserType.MANAGER,
                is_active=True
            ).order_by('name', 'email')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)


class EquipmentOperatorInlineForm(forms.ModelForm):
    """Form for Equipment Operator inline; operator dropdown shows name instead of email."""
    class Meta:
        model = EquipmentOperator
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'operator' in self.fields:
            from iic_booking.users.models.user import User
            qs = User.objects.filter(
                user_type=UserType.OPERATOR,
                is_active=True
            ).order_by('name', 'email')
            self.fields['operator'].queryset = qs
            self.fields['operator'].label_from_instance = lambda obj: obj.name or obj.email or str(obj)
        if "role" in self.fields:
            self.fields["role"].help_text = "Select Primary or Secondary operator for this instrument."


class EquipmentOperatorInlineFormSet(forms.models.BaseInlineFormSet):
    """Validate at most one PRIMARY and one SECONDARY operator per equipment."""

    def clean(self):
        super().clean()
        seen_roles: set[str] = set()
        for form in self.forms:
            if not hasattr(form, "cleaned_data"):
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            operator = form.cleaned_data.get("operator")
            role = form.cleaned_data.get("role")
            if not operator:
                continue
            if role in seen_roles:
                raise forms.ValidationError(
                    "Only one Primary operator and one Secondary operator can be assigned per instrument."
                )
            seen_roles.add(role)


class EquipmentOperatorInline(admin.TabularInline):
    """Inline admin for Equipment Operators."""
    model = EquipmentOperator
    form = EquipmentOperatorInlineForm
    formset = EquipmentOperatorInlineFormSet
    extra = 0
    fields = ['operator', 'role', 'created_at', 'updated_at']
    readonly_fields = ['created_at', 'updated_at']
    classes = ['collapse']

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Filter operator field to only show users with operator user type."""
        if db_field.name == "operator":
            from iic_booking.users.models.user import User
            kwargs["queryset"] = User.objects.filter(
                user_type=UserType.OPERATOR,
                is_active=True
            ).order_by('name', 'email')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

class EquipmentSpecificationInlineForm(forms.ModelForm):
    """Form for Equipment Specification inline; spec_value uses multi-line textarea."""
    class Meta:
        model = EquipmentSpecification
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if 'spec_value' in self.fields:
            self.fields['spec_value'].widget = forms.Textarea(
                attrs={'rows': 3, 'cols': 40, 'class': 'vLargeTextField'}
            )


class EquipmentSpecificationInline(admin.TabularInline):
    """Inline admin for Equipment Specifications."""
    model = EquipmentSpecification
    form = EquipmentSpecificationInlineForm
    extra = 0
    fields = ['spec_key', 'spec_value']
    readonly_fields = ['created_at']
    classes = ['collapse']

class EquipmentAccessoryInline(admin.TabularInline):
    """Inline admin for Equipment Accessories."""
    model = EquipmentAccessory
    extra = 0
    fields = ['accessory_name', 'is_optional']
    readonly_fields = ['created_at']
    classes = ['collapse']

class EquipmentAdditionalAccessoryInline(admin.TabularInline):
    """Inline admin for Equipment Additional Accessories."""
    model = EquipmentAdditionalAccessory
    extra = 0
    fields = ['additional_accessory_name', 'additional_accessory_description', 'is_optional']
    readonly_fields = ['created_at']
    classes = ['collapse']

class DynamicInputFieldForm(forms.ModelForm):
    """Custom form for DynamicInputField with better options handling."""

    options_text = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 3, 'placeholder': 'Enter options, one per line'}),
        help_text=_(
            'Enter options one per line (for RADIO, COMBO, MULTI_SELECT). '
            'For TABLE: enter column headers, one per line. '
            'For NUMERIC (especially key A), you may enter plain formula like B*4 '
            '(interpreted as {"min":1,"max_formula":"B*4"}), '
            'or JSON like {"min":1,"max":100} / {"min":1,"max_formula":"B*5"}. '
            'max_formula supports A-Z and SLOT_DURATION_MINUTES.'
        )
    )

    class Meta:
        model = DynamicInputField
        fields = '__all__'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Convert options JSON list to text format for editing
        if self.instance and self.instance.pk and self.instance.options:
            if isinstance(self.instance.options, list):
                self.initial['options_text'] = '\n'.join(str(opt) for opt in self.instance.options)
                # Show default_value as 1-based index for RADIO/COMBO/MULTI_SELECT when it matches an option
                opts = [str(o) for o in self.instance.options]
                if self.instance.field_type in [DynamicInputFieldType.RADIO, DynamicInputFieldType.COMBO, DynamicInputFieldType.MULTI_SELECT]:
                    if self.instance.default_value and self.instance.default_value in opts:
                        self.initial['default_value'] = str(opts.index(self.instance.default_value) + 1)
            else:
                if isinstance(self.instance.options, dict):
                    self.initial['options_text'] = json.dumps(self.instance.options, indent=2)
                else:
                    self.initial['options_text'] = str(self.instance.options)
        else:
            self.initial['options_text'] = ''

        # Help text for default_value when field type uses options
        if 'default_value' in self.fields:
            self.fields['default_value'].help_text = _(
                'For RADIO/COMBO/MULTI_SELECT: use 1 for first option, 2 for second, etc. '
                'Or enter the exact option value. For other types, enter the default value.'
            )

    def clean_options_text(self):
        """Clean and return options text."""
        options_text = self.cleaned_data.get('options_text', '').strip()
        return options_text

    def clean(self):
        """Validate that options are provided for field types that require them."""
        cleaned_data = super().clean()
        field_type = cleaned_data.get('field_type')
        options_text = cleaned_data.get('options_text', '').strip()

        # Field types that require options (TABLE = column headers, one per line)
        field_types_requiring_options = [
            DynamicInputFieldType.RADIO,
            DynamicInputFieldType.COMBO,
            DynamicInputFieldType.MULTI_SELECT,
            DynamicInputFieldType.TABLE,
        ]

        if field_type in field_types_requiring_options and not options_text:
            raise forms.ValidationError({
                'options_text': _('Options are required for RADIO, COMBO, MULTI_SELECT, and TABLE (column headers, one per line) field types.')
            })

        if field_type == DynamicInputFieldType.NUMERIC and options_text:
            if options_text.startswith('{'):
                try:
                    parsed = json.loads(options_text)
                except Exception:
                    raise forms.ValidationError({
                        'options_text': _('For NUMERIC JSON options, provide valid JSON (e.g. {"min":1,"max_formula":"B*5"}).')
                    })
                if not isinstance(parsed, dict):
                    raise forms.ValidationError({
                        'options_text': _('For NUMERIC JSON options, use a JSON object.')
                    })
            else:
                # Plain formula mode for numeric field options_text (e.g., "B*4")
                if not re.fullmatch(r"[A-Za-z0-9_\+\-\*\/\(\)\.\s]+", options_text):
                    raise forms.ValidationError({
                        'options_text': _('Invalid numeric formula. Use letters/numbers and + - * / ( ) . only.')
                    })

        return cleaned_data

    def save(self, commit=True):
        """Save options as JSON list and resolve default_value index to option value."""
        instance = super().save(commit=False)
        options_text = self.cleaned_data.get('options_text', '').strip()
        field_type = self.cleaned_data.get('field_type')
        default_value = (self.cleaned_data.get('default_value') or '').strip()

        if options_text:
            if field_type == DynamicInputFieldType.NUMERIC:
                if options_text.startswith('{'):
                    parsed_options = json.loads(options_text)
                    if isinstance(parsed_options, dict):
                        # Enforce minimum value 1 for numeric formula mode.
                        parsed_options['min'] = 1
                    instance.options = parsed_options
                else:
                    # Plain formula shorthand: "B*4" => {"min":1,"max_formula":"B*4"}
                    instance.options = {
                        'min': 1,
                        'max_formula': options_text,
                    }
            else:
                # Convert text to list (options = options for RADIO/COMBO/MULTI_SELECT; column headers for TABLE)
                options = [opt.strip() for opt in options_text.split('\n') if opt.strip()]
                instance.options = options

            # For RADIO/COMBO/MULTI_SELECT: if default_value is 1-based index (1, 2, 3...), save the actual option
            if field_type in [DynamicInputFieldType.RADIO, DynamicInputFieldType.COMBO, DynamicInputFieldType.MULTI_SELECT]:
                if default_value.isdigit():
                    options = [opt.strip() for opt in options_text.split('\n') if opt.strip()]
                    idx = int(default_value)
                    if 1 <= idx <= len(options):
                        instance.default_value = options[idx - 1]
                    # else: keep existing default_value from form (invalid index)
                elif default_value:
                    instance.default_value = default_value
        else:
            # Clear options when optional/options text is empty (including PERIODIC_TABLE and TABLE)
            instance.options = []

        if commit:
            instance.save()
        return instance

class DynamicInputFieldInline(admin.TabularInline):
    """Inline admin for Dynamic Input Fields."""
    form = DynamicInputFieldForm
    model = DynamicInputField
    extra = 0
    fk_name = 'equipment'
    fields = ['field_key', 'field_label', 'field_type', 'is_required', 'editing_required', 'default_value', 'options_text', 'help_text', 'source_element_field_key']
    classes = ['collapse']


class PrintMaterialInline(admin.TabularInline):
    """Filament/material catalog for PRINT_3D equipment."""
    model = PrintMaterial
    extra = 1
    fk_name = "equipment"
    fields = [
        "code",
        "name",
        "density_g_per_cm3",
        "price_per_gram",
        "user_type",
        "display_order",
        "is_active",
    ]
    ordering = ("display_order", "name")

class MultiParamDefinitionForm(forms.ModelForm):
    """Custom form for MultiParamDefinition with user_type dropdown."""
    
    class Meta:
        model = MultiParamDefinition
        fields = '__all__'
        widgets = {
            'user_type': forms.Select(choices=UserType.get_choices()),
        }
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Restrict user_type choices to only: student, faculty, rnd center, institute, external
        allowed_user_types = [
            (UserType.STUDENT, _("IITR Student")),
            (UserType.FACULTY, _("IITR Faculty")),
            (UserType.EXTERNAL, _("Educational Institute")),
            (UserType.RND, _("Govt R&D Organizations")),
            (UserType.INSTITUTE, _("Industry")),
            (UserType.STARTUP_INCUBATED_IITR, _("Startup Incubated at IIT Roorkee")),
            (UserType.EXTERNAL_STARTUP_MSME, _("External Startup/MSME")),
        ]
        self.fields['user_type'].widget = forms.Select(choices=allowed_user_types)
        self.fields['user_type'].required = True
        self.fields['user_type'].help_text = _('User type for this slot option configuration')
        
        # Update field labels and help text
        self.fields['param_name'].label = _('Slot Option Name')
        self.fields['param_name'].help_text = _('Name of the slot option (e.g., "Slot 1", "Slot 2", "Morning Slot")')
        
        self.fields['param_code'].label = _('Slot Option Code')
        self.fields['param_code'].help_text = _('Code/identifier for this slot option (used in radio field)')
        
        self.fields['unit_time_minutes'].label = _('Time per Sample (minutes)')
        self.fields['unit_time_minutes'].help_text = _('Time in minutes per sample for this slot option')
        
        self.fields['unit_charge'].label = _('Charge per Sample')
        self.fields['unit_charge'].help_text = _('Charge per sample for this slot option')
    
    def clean(self):
        """Validate that user_type is provided and check for duplicates."""
        cleaned_data = super().clean()
        user_type = cleaned_data.get('user_type')
        equipment = cleaned_data.get('equipment')
        param_code = cleaned_data.get('param_code')
        
        if not user_type:
            raise forms.ValidationError({
                'user_type': _('User type is required for slot option configuration.')
            })
        
        # Check for duplicate slot option code for same equipment and user_type.
        # Only run when equipment is saved (on add, equipment is unsaved and cannot be used in filters).
        if equipment and getattr(equipment, 'pk', None) and param_code and user_type:
            existing = MultiParamDefinition.objects.filter(
                equipment=equipment,
                user_type=user_type,
                param_code=param_code
            )
            if self.instance and self.instance.pk:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise forms.ValidationError({
                    'param_code': _('A slot option with this code already exists for this equipment and user type.')
                })
        
        return cleaned_data

class MultiParamDefinitionInlineFormSet(forms.models.BaseInlineFormSet):
    """Formset that injects management form defaults when missing (e.g. add view with collapsed inline)."""

    def __init__(self, data=None, *args, **kwargs):
        prefix = kwargs.get("prefix")
        if data is not None and prefix and (str(prefix) + "-INITIAL_FORMS") not in data:
            data = data.copy()
            data.setdefault(str(prefix) + "-TOTAL_FORMS", 0)
            data.setdefault(str(prefix) + "-INITIAL_FORMS", 0)
            data.setdefault(str(prefix) + "-MIN_NUM_FORMS", 0)
            data.setdefault(str(prefix) + "-MAX_NUM_FORMS", 1000)
        super().__init__(data=data, *args, **kwargs)


class MultiParamDefinitionInline(admin.TabularInline):
    """Inline admin for Multi-Parameter Definitions (Slot Options Configuration)."""
    form = MultiParamDefinitionForm
    formset = MultiParamDefinitionInlineFormSet
    model = MultiParamDefinition
    extra = 0
    fk_name = 'equipment'
    fields = ['user_type', 'param_name', 'param_code', 'unit_time_minutes', 'unit_charge', 'is_active']
    classes = ['collapse']

    def get_queryset(self, request):
        """Order by user_type and param_name for better organization."""
        qs = super().get_queryset(request)
        return qs.order_by('user_type', 'param_name')

class SlotMasterInline(admin.TabularInline):
    """Inline admin for Slot Masters."""
    model = SlotMaster
    extra = 0
    fk_name = 'equipment'
    fields = ['slot_number', 'slot_name', 'open_time', 'close_time', 'is_active']
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['slot_number']
    classes = ['collapse']

class ChargeProfileInlineFormSet(forms.models.BaseInlineFormSet):
    """Pass parent equipment to each form so MULTI_PARAM can make charge fields optional."""

    def _construct_form(self, i, **kwargs):
        # Pass parent before form __init__ so ChargeProfileForm can set required=False for MULTI_PARAM
        kwargs["_parent_equipment"] = getattr(self, "instance", None)
        return super()._construct_form(i, **kwargs)

    def clean(self):
        super().clean()
        if any(form.errors for form in self.forms):
            return
        seen_user_types: list[str] = []
        for form in self.forms:
            if not hasattr(form, "cleaned_data") or not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            ut = form.cleaned_data.get("user_type")
            if not ut:
                continue
            if ut in seen_user_types:
                raise forms.ValidationError(
                    _(
                        "Duplicate user type in charge profiles: each user type may only appear once "
                        "in standard pricing for this equipment."
                    )
                )
            seen_user_types.append(ut)

    def save_new(self, form, commit=True):
        """Avoid IntegrityError when a standard row already exists (double row, missing id, stale POST)."""
        obj = form.save(commit=False)
        setattr(obj, self.fk.get_attname(), self.instance.pk)
        if not commit:
            return obj
        pp = obj.pricing_profile or ChargeProfilePricingProfile.STANDARD
        if pp == ChargeProfilePricingProfile.STANDARD:
            defaults = {
                "is_active": obj.is_active,
                "primary_unit_charge": obj.primary_unit_charge,
                "secondary_unit_charge": obj.secondary_unit_charge,
                "breakpoint": obj.breakpoint,
                "time_formula": obj.time_formula,
            }
            cp, _created = ChargeProfile.objects.update_or_create(
                equipment=self.instance,
                user_type=obj.user_type,
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
                defaults=defaults,
            )
            return cp
        obj.save()
        return obj


class ChargeProfileForm(forms.ModelForm):
    """Custom form for ChargeProfile with user_type dropdown."""

    class Meta:
        model = ChargeProfile
        fields = "__all__"
        widgets = {
            "user_type": forms.Select(choices=UserType.get_choices()),
            "pricing_profile": forms.HiddenInput(),
        }

    def __init__(self, *args, **kwargs):
        self._parent_equipment = kwargs.pop("_parent_equipment", None)
        super().__init__(*args, **kwargs)
        if not getattr(self.instance, "pk", None):
            self.initial.setdefault("pricing_profile", ChargeProfilePricingProfile.STANDARD)
        # Restrict user_type choices to only: student, faculty, rnd center, institute, external
        allowed_user_types = [
            (UserType.STUDENT, _("IITR Student")),
            (UserType.FACULTY, _("IITR Faculty")),
            (UserType.EXTERNAL, _("Educational Institute")),
            (UserType.RND, _("Govt R&D Organizations")),
            (UserType.INSTITUTE, _("Industry")),
            (UserType.STARTUP_INCUBATED_IITR, _("Startup Incubated at IIT Roorkee")),
            (UserType.EXTERNAL_STARTUP_MSME, _("External Startup/MSME")),
        ]
        self.fields["user_type"].widget = forms.Select(choices=allowed_user_types)
        self.fields["user_type"].help_text = _("Select the user type this profile applies to")
        if "require_istem_fbr" in self.fields:
            self.fields["require_istem_fbr"].help_text = _(
                "When enabled, users booking with this charge profile must submit an I-STEM FBR number "
                "and have it verified by the Officer in Charge before results are released."
            )

        # For MULTI_PARAM equipment, charge fields are hidden in UI and filled from slot options
        equipment = self._parent_equipment or (
            self.instance.equipment if self.instance and self.instance.pk else None
        )
        if equipment and getattr(equipment, "profile_type", None) == "MULTI_PARAM":
            self.fields["primary_unit_charge"].required = False
            self.fields["secondary_unit_charge"].required = False

    def clean(self):
        cleaned_data = super().clean()
        equipment = getattr(self, "_parent_equipment", None) or (
            self.instance.equipment if self.instance and self.instance.pk else None
        )
        if not equipment and cleaned_data.get("equipment"):
            equipment = cleaned_data["equipment"]
        if equipment and getattr(equipment, "profile_type", None) == "MULTI_PARAM":
            if cleaned_data.get("primary_unit_charge") is None or cleaned_data.get("primary_unit_charge") == "":
                cleaned_data["primary_unit_charge"] = Decimal("0.00")
            if cleaned_data.get("secondary_unit_charge") is None or cleaned_data.get("secondary_unit_charge") == "":
                cleaned_data["secondary_unit_charge"] = Decimal("0.00")
        return cleaned_data


class ChargeProfileInline(admin.StackedInline):
    """Inline admin for Charge Profiles."""
    form = ChargeProfileForm
    formset = ChargeProfileInlineFormSet
    model = ChargeProfile
    extra = 0
    fk_name = 'equipment'
    fields = [
        'user_type',
        'pricing_profile',
        'is_active',
        'require_istem_fbr',
        'primary_unit_charge',
        'secondary_unit_charge',
        'breakpoint',
        'time_formula',
    ]
    readonly_fields = ['created_at', 'updated_at']
    ordering = ['user_type']
    classes = ['collapse']

    def get_queryset(self, request):
        # Only allow admins to edit STANDARD charge profiles. Discounted variants are
        # auto-seeded (zero charges) and managed via user flag in /admin/section/users.
        qs = super().get_queryset(request)
        return qs.filter(pricing_profile=ChargeProfilePricingProfile.STANDARD)
    
    class Media:
        js = (
            'admin/js/jquery.init.js',
            'js/dynamic_input_field.js',
            'js/charge_profile_admin.js',
        )

class EquipmentCategoryFilter(SimpleListFilter):
    """Filter equipment by category."""
    title = _('Category')
    parameter_name = 'category'

    def lookups(self, request, model_admin):
        categories = EquipmentCategory.objects.all().order_by('name')
        return [(cat.id, cat.name) for cat in categories]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(category_id=self.value())
        return queryset


class InternalDepartmentFilter(SimpleListFilter):
    """Filter equipment by internal department."""
    title = _('Internal Department')
    parameter_name = 'internal_department'

    def lookups(self, request, model_admin):
        from iic_booking.users.models.department import Department, DepartmentType
        depts = Department.objects.filter(department_type=DepartmentType.INTERNAL).order_by('name')
        return [(d.id, d.name) for d in depts]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(internal_department_id=self.value())
        return queryset


@admin.register(EquipmentCategory)
class EquipmentCategoryAdmin(admin.ModelAdmin):
    """Admin for Equipment Category."""
    list_display = ['name', 'code', 'equipment_count', 'created_at']
    list_filter = ['created_at']
    search_fields = ['name', 'code', 'description']
    ordering = ['name']
    readonly_fields = ['created_at', 'updated_at']

    def equipment_count(self, obj):
        return obj.equipment.count()
    equipment_count.short_description = _('Equipment count')


class EquipmentGroupQuotaInline(admin.TabularInline):
    """Inline admin for Equipment Group Quotas."""
    model = EquipmentGroupQuota
    extra = 0
    fields = ['quota_type', 'internal_individual_quota_minutes', 'internal_faculty_quota_minutes',
              'external_individual_quota_minutes', 'external_faculty_quota_minutes', 'is_enforced']
    verbose_name = _('Quota Configuration')
    verbose_name_plural = _('Quota Configurations')


class EquipmentInlineForm(forms.ModelForm):
    """Custom form for Equipment inline to show equipment dropdown."""
    equipment_select = forms.ModelChoiceField(
        queryset=Equipment.objects.none(),
        required=False,
        label=_('Select Equipment'),
        help_text=_('Choose an equipment to add to this group')
    )
    
    class Meta:
        model = Equipment
        fields = []
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Get parent object from formset
        parent_obj = getattr(self, '_parent_obj', None)
        
        # Add equipment selection field for new entries
        if not self.instance.pk:
            # Show equipment that don't belong to any group
            if parent_obj:
                self.fields['equipment_select'].queryset = Equipment.objects.filter(
                    models.Q(equipment_group__isnull=True) | 
                    models.Q(equipment_group=parent_obj)
                ).order_by('code', 'name')
            else:
                self.fields['equipment_select'].queryset = Equipment.objects.filter(
                    equipment_group__isnull=True
                ).order_by('code', 'name')
            self.fields['equipment_select'].required = True
        else:
            # For existing entries, show the equipment info as readonly
            self.fields['equipment_select'].widget = forms.HiddenInput()


class EquipmentInlineFormset(forms.models.BaseInlineFormSet):
    """Custom formset to handle equipment selection."""
    def _construct_form(self, i, **kwargs):
        form = super()._construct_form(i, **kwargs)
        # Set parent_obj on form using instance (which is the EquipmentGroup)
        form._parent_obj = getattr(self, 'instance', None)
        return form

    def save(self, commit=True):
        """Override save to handle equipment selection."""
        self.new_objects = []
        self.changed_objects = []
        self.deleted_objects = []
        equipment_group = self.instance

        for form in self.forms:
            if not form.cleaned_data:
                continue
            if self.can_delete and form.cleaned_data.get('DELETE', False):
                obj = form.instance
                if obj.pk:
                    self.deleted_objects.append(obj)
                    obj.equipment_group = None
                    if commit:
                        obj.save()
                continue
            if not form.instance.pk and form.cleaned_data.get('equipment_select'):
                equipment = form.cleaned_data['equipment_select']
                equipment.equipment_group = equipment_group
                if commit:
                    equipment.save()
                self.new_objects.append(equipment)
            elif form.instance.pk:
                form.instance.equipment_group = equipment_group
                if commit:
                    form.instance.save()
                self.changed_objects.append((form.instance, form.changed_data))

        return self.new_objects + [obj for obj, _ in self.changed_objects]
    
    def is_valid(self):
        """Override to handle equipment selection validation."""
        valid = super().is_valid()
        if valid:
            # Check that new forms have equipment selected
            for form in self.forms:
                if not form.instance.pk and form.cleaned_data and not form.cleaned_data.get('DELETE', False):
                    if not form.cleaned_data.get('equipment_select'):
                        form.add_error('equipment_select', _('Please select an equipment.'))
                        valid = False
        return valid


class EquipmentInline(admin.TabularInline):
    """Inline admin for Equipment in Equipment Group."""
    model = Equipment
    form = EquipmentInlineForm
    formset = EquipmentInlineFormset
    extra = 1
    fields = ['equipment_select', 'equipment_display']
    readonly_fields = ['equipment_display']
    can_delete = True
    show_change_link = True
    verbose_name = _('Equipment')
    verbose_name_plural = _('Equipment')
    
    def equipment_display(self, obj):
        """Display equipment information for existing entries."""
        if obj and obj.pk:
            return format_html(
                '<strong>{}</strong> - {}<br><small>Status: {}</small>',
                obj.code,
                obj.name,
                obj.get_status_display() if obj.status else 'N/A'
            )
        return '-'
    equipment_display.short_description = _('Equipment')
    
    def get_queryset(self, request):
        """Filter to show only equipment belonging to this group."""
        qs = super().get_queryset(request)
        return qs.select_related('category', 'equipment_group')
    
    def get_formset(self, request, obj=None, **kwargs):
        """Return formset class - instance is automatically passed by Django."""
        return super().get_formset(request, obj, **kwargs)


@admin.register(EquipmentGroup)
class EquipmentGroupAdmin(admin.ModelAdmin):
    """Admin for Equipment Group."""
    list_display = ['name', 'code', 'equipment_count', 'created_at']
    list_filter = ['created_at']
    search_fields = ['name', 'code', 'description']
    ordering = ['name']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [EquipmentInline, EquipmentGroupQuotaInline]
    
    fieldsets = (
        (_('Basic Information'), {
            'fields': ('name', 'code', 'description')
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )

    def equipment_count(self, obj):
        return obj.equipment.count()
    equipment_count.short_description = _('Equipment count')


@admin.register(Equipment)
class EquipmentAdmin(admin.ModelAdmin):
    """Admin configuration for Equipment model."""
    form = EquipmentAdminForm
    
    list_display = [
        'code',
        'name',
        'category',
        'equipment_group',
        'internal_department',
        'visibility_group',
        'profile_type',
        'status',
        'skip_quota_check',
        'manager_display',
        'operator_display',
    ]
    
    list_filter = [
        'status',
        'profile_type',
        'skip_quota_check',
        'equipment_group',
        EquipmentCategoryFilter,
        InternalDepartmentFilter,
    ]
    
    search_fields = [
        'code',
        'name',
        'category__name',
        'category__code',
        'equipment_group__name',
        'equipment_group__code',
        'internal_department__name',
        'internal_department__code',
        'equipment_managers__manager__email',
        'equipment_managers__manager__name',
        'equipment_operators__operator__name',
    ]
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Restrict internal_department dropdown to internal departments only."""
        if db_field.name == 'internal_department':
            from iic_booking.users.models.department import Department, DepartmentType
            kwargs['queryset'] = Department.objects.filter(
                department_type=DepartmentType.INTERNAL
            ).order_by('name')
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
    
    actions = ['generate_slots_one_month', 'assign_to_group']
    
    inlines = [
        EquipmentManagerInline,
        EquipmentOperatorInline,
        EquipmentSpecificationInline,
        EquipmentAccessoryInline,
        EquipmentAdditionalAccessoryInline,
        DynamicInputFieldInline,
        ChargeProfileInline,
        SlotMasterInline,
    ]
    
    def get_inlines(self, request, obj):
        """Return inlines based on equipment profile_type."""
        inlines = list(super().get_inlines(request, obj))
        
        # Always include MultiParamDefinitionInline on add (obj is None) so the formset
        # is rendered and its management form (TOTAL_FORMS, etc.) is in the POST.
        # Otherwise submitting with profile_type=MULTI_PARAM causes "TOTAL_FORMS required" error.
        # On change view, only show it when profile_type is MULTI_PARAM.
        if obj:
            if obj.profile_type == 'MULTI_PARAM':
                try:
                    slot_index = inlines.index(SlotMasterInline)
                    inlines.insert(slot_index, MultiParamDefinitionInline)
                except ValueError:
                    inlines.append(MultiParamDefinitionInline)
            elif obj.profile_type == 'PRINT_3D':
                try:
                    charge_index = inlines.index(ChargeProfileInline)
                    inlines.insert(charge_index, PrintMaterialInline)
                except ValueError:
                    inlines.append(PrintMaterialInline)
        else:
            # Add view: always include so management form is present on submit
            try:
                slot_index = inlines.index(SlotMasterInline)
                inlines.insert(slot_index, MultiParamDefinitionInline)
            except ValueError:
                inlines.append(MultiParamDefinitionInline)
        
        return inlines
    
    fieldsets = (
        (_('Basic Information'), {
            'fields': (
                'name', 'code', 'category', 'equipment_group', 'internal_department', 'visibility_group',
                'profile_type', 'description', 'status', 'location',
                'make', 'show_make_on_card',
                'split_booking_enabled',
                'skip_quota_check',
                'auto_slot_selection_default',
                'enable_charge_recalculation', 'user_rating_enabled',
                'sample_preparation_by_user',
                'weekly_view_display',
            )
        }),
        (_('Important Instruction'), {
            'fields': ('important_instruction',),
            'description': _('Optional instructions shown prominently on the equipment page (above specifications).'),
        }),
        (_('Booking emails'), {
            'fields': (
                'booking_email_extra_text',
                'completion_email_extra_text',
                'print_3d_stl_notification_email',
                'istem_portal_url',
                'istem_fbr_status_url',
            ),
            'description': _(
                'Optional text appended to emails for this equipment. '
                'Booking extra: confirmation and reminder emails. '
                'Completion extra: sent when a booking is marked completed (URLs become links in HTML).'
            ),
            'classes': ('collapse',),
        }),
        (_('Image'), {
            'fields': ('image_preview', 'image'),
            'description': _('Equipment photo for the booking site. Stored in media storage until you replace or clear it.'),
            'classes': ('collapse',)
        }),
        (_('Video'), {
            'fields': ('video_preview', 'video_file'),
            'description': _('Upload a video file for the equipment. Supported formats: MP4, WebM, OGG.'),
            'classes': ('collapse',)
        }),
        (_('Slot Configuration'), {
            'fields': (
                'slot_duration_minutes', 'slots_per_day', 'reschedule_hours_threshold', 'results_base_location',
                'weekly_view_time_from', 'weekly_view_time_to',
                'slot_window_reference_weekday', 'slot_window_reference_time',
                'urgent_peak_window_minutes', 'max_urgent_requests', 'waitlist_queue_depth',
                'booking_not_utilize_window_hours',
                'operator_unavailable_after_booking_end_hours',
                'operator_absent_disruption_after_booking_end_hours',
                'repeat_sample_request_days', 'repeat_sample_disclaimer',
            ),
            'description': _(
                'Slot duration and count. '
                'Slot window time from/to: only slots within this time range (inclusive) are shown and bookable; leave empty for no limit. Changing these triggers waitlist auto-booking. '
                'Slot window reference (weekday/time): when next week becomes visible to internal users. '
                'Results base location: root folder where In Analysis status creates booking folder structure '
                '(Equipment -> Internal/External -> Year -> Department -> User -> Booking ID). '
                'Urgent peak window: minutes after slot window time for showing failed attempts in Request urgent booking log (leave empty for all). '
                'Waitlist queue depth: max users on waitlist when booking fails (0 = disabled). '
                'Booking Not Utilize Window: hours after last slot end before the manual “Booking Not Utilized” '
                'button is allowed per equipment (0 = hidden). Global auto not-utilized (weekdays 20:00 IST) uses '
                '24h after latest booked slot end_datetime and only empty or Sample Sent lifecycle. '
                'Auto Operator Unavailable: hours after last slot end for possible full refund when lifecycle '
                'moved past Sample Sent but is not finished — skipped if latest trace is forwarded, accepted, processing, held, or rejected. '
                'Auto Operator Absent Disruption: hours after last slot end when latest trace is stuck at Forwarded, '
                'Accepted, or Processing; opens disruption (refund vs reschedule). '
                'Repeat sample: days after completion when user can request a repeat; disclaimer shown in popup. Define actual timings in Slot Masters below.'
            ),
            'classes': ('collapse',)
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ['created_at', 'updated_at', 'image_preview', 'video_preview']
    
    ordering = ['name', 'profile_type']
    
    def save_model(self, request, obj, form, change):
        """Persist equipment. When slot window changes, run waitlist."""
        existing_image_name = None
        if change and obj.pk:
            try:
                existing_image_name = Equipment.objects.filter(pk=obj.pk).values_list("image", flat=True).first()
            except Exception:
                existing_image_name = None

        new_image_uploaded = bool(request.FILES.get("image"))
        clear_image_requested = bool(request.POST.get("image-clear"))

        old_time_from = None
        old_time_to = None
        if change and obj.pk:
            try:
                prev = Equipment.objects.only("weekly_view_time_from", "weekly_view_time_to").get(pk=obj.pk)
                old_time_from = prev.weekly_view_time_from
                old_time_to = prev.weekly_view_time_to
            except Exception:
                pass

        super().save_model(request, obj, form, change)

        # Preserve existing image when admin saves without uploading a new file.
        # Always restore the previous DB path — do not gate on storage availability
        # (false-negative S3 checks previously left the image blank after unrelated edits).
        if (
            existing_image_name
            and not new_image_uploaded
            and not clear_image_requested
            and (
                not getattr(obj, "image", None)
                or not getattr(obj.image, "name", None)
                or obj.image.name != existing_image_name
            )
        ):
            try:
                from .image_utils import normalize_storage_path

                obj.image.name = normalize_storage_path(existing_image_name) or existing_image_name
                obj.save(update_fields=["image"])
            except Exception:
                pass

        if new_image_uploaded and getattr(obj, "image", None) and obj.image.name:
            upload = request.FILES.get("image")
            if upload:
                try:
                    from .image_utils import normalize_equipment_image_db_path

                    normalize_equipment_image_db_path(obj, save=True)
                    upload.seek(0)
                    save_local_equipment_image_backup(obj.image.name, upload.read())
                except Exception:
                    pass

        # Safety check: warn if remote storage cannot open the image (do not clear the DB path).
        if getattr(obj, "image", None) and getattr(obj.image, "name", None):
            from .image_utils import verify_file_field_in_storage

            if not verify_file_field_in_storage(obj.image):
                messages.error(
                    request,
                    _(
                        "Equipment image was not found in remote storage after save "
                        "(local backup alone is not enough — images vanish on redeploy). "
                        "Check AWS credentials/bucket. Path: %(path)s"
                    )
                    % {"path": obj.image.name},
                )

        if change and (old_time_from != obj.weekly_view_time_from or old_time_to != obj.weekly_view_time_to):
            try:
                from iic_booking.equipment.waitlist import notify_waitlist_slots_available
                notified = notify_waitlist_slots_available(obj)
                if notified:
                    self.message_user(
                        request,
                        _('Slot window updated. Waitlist processed: %(count)d booking(s) created and queue cleared.') % {'count': notified},
                        messages.SUCCESS,
                    )
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning('Failed to process waitlist after slot window change for equipment %s: %s', getattr(obj, 'code', obj.pk), e)
        
        # Note: Slot masters are now generated on-demand via API when frontend requests slots.
        # This prevents data loss from automatic deletion of slots and bookings.
        # If slot configuration changed, slot masters will be updated intelligently
        # when slots are next requested, preserving historical bookings.
    
    def manager_display(self, obj):
        """Display manager names and emails."""
        managers = obj.equipment_managers.all()
        if managers:
            manager_list = []
            for mgr in managers:
                manager_list.append(
                    format_html(
                        '{}<br><small style="color: #666;">{}</small>',
                        mgr.manager.name or mgr.manager.email,
                        mgr.manager.email
                    )
                )
            return format_html('<br>'.join(str(m) for m in manager_list))
        return "-"
    
    manager_display.short_description = _("Officers in Charge")
    
    def operator_display(self, obj):
        """Display operator names and emails."""
        operators = obj.equipment_operators.all()
        if operators:
            operator_list = []
            for op in operators:
                operator_list.append(
                    format_html(
                        '{}<br><small style="color: #666;">{}</small>',
                        op.operator.name or op.operator.email,
                        op.operator.email
                    )
                )
            return format_html('<br>'.join(str(o) for o in operator_list))
        return "-"
    
    operator_display.short_description = _("Operators")
    
    def generate_slots_one_month(self, request, queryset):
        """Admin action to generate slots forward for 1 month."""
        from .slot_utils import SlotGenerator
        from datetime import date
        from django.contrib import messages
        
        total_slots_created = 0
        for equipment in queryset:
            try:
                # Check if equipment has slot masters
                slot_masters = SlotMaster.objects.filter(
                    equipment=equipment,
                    is_active=True
                )
                
                if not slot_masters.exists():
                    messages.warning(
                        request,
                        _('Equipment "{}" has no active slot masters. Please create slot masters first.').format(equipment.code)
                    )
                    continue
                
                # Generate slots for 1 month forward
                slots = SlotGenerator.generate_monthly_slots(
                    equipment=equipment,
                    start_date=timezone.localdate(),
                    months=1
                )
                
                total_slots_created += len(slots)
                messages.success(
                    request,
                    _('Successfully generated {} slot(s) for equipment "{}" for the next month.').format(
                        len(slots), equipment.code
                    )
                )
            except Exception as e:
                messages.error(
                    request,
                    _('Error generating slots for equipment "{}": {}').format(equipment.code, str(e))
                )
        
        if total_slots_created > 0:
            messages.success(
                request,
                _('Total slots created: {}').format(total_slots_created)
            )
    
    generate_slots_one_month.short_description = _("Generate slots forward for 1 month")
    
    def assign_to_group(self, request, queryset):
        """Admin action to assign multiple equipment to an equipment group."""
        from django.contrib import messages
        from django.shortcuts import render
        
        # Check if this is the form submission (has equipment_group in POST)
        is_form_submission = request.method == "POST" and 'equipment_group' in request.POST
        
        if is_form_submission:
            # Process the assignment
            group_id = request.POST.get('equipment_group')
            
            # Get the queryset from selected IDs if available
            selected_ids = request.POST.getlist('selected_ids')
            if selected_ids:
                queryset = Equipment.objects.filter(pk__in=selected_ids)
            
            if group_id:
                try:
                    group = EquipmentGroup.objects.get(pk=group_id)
                    updated_count = queryset.update(equipment_group=group)
                    messages.success(
                        request,
                        _('Successfully assigned {count} equipment to group "{group}".').format(
                            count=updated_count,
                            group=group.name
                        )
                    )
                except EquipmentGroup.DoesNotExist:
                    messages.error(request, _('Selected equipment group does not exist.'))
            else:
                # Remove from group (set to None)
                updated_count = queryset.update(equipment_group=None)
                messages.success(
                    request,
                    _('Successfully removed {count} equipment from their groups.').format(
                        count=updated_count
                    )
                )
            return None
        
        # Show intermediate form
        # Store selected IDs for form submission
        selected_ids = [str(eq.pk) for eq in queryset]
        
        # Get all available equipment groups
        groups = EquipmentGroup.objects.all().order_by('name')
        
        context = {
            'title': _('Assign Equipment to Group'),
            'equipment': queryset,
            'groups': groups,
            'selected_ids': selected_ids,
            'opts': self.model._meta,
            'has_change_permission': self.has_change_permission(request),
        }
        
        return render(request, 'admin/equipment/equipment/assign_to_group.html', context)
    
    assign_to_group.short_description = _("Assign selected equipment to group")
    
    def image_preview(self, obj):
        """Display equipment image via staff-only view (same session as admin; stable, no disappearing)."""
        if not obj or not getattr(obj, "equipment_id", None) or not (obj.image and obj.image.name):
            return format_html('<span style="color: #999;">No image uploaded</span>')
        try:
            proxy_url = reverse("serve_equipment_image", kwargs={"pk": obj.equipment_id})
            cache_bust = getattr(obj, "updated_at", None)
            if cache_bust:
                from django.utils.dateformat import format as date_format
                cache_bust = date_format(cache_bust, "U") if hasattr(cache_bust, "year") else str(cache_bust)
            else:
                cache_bust = obj.equipment_id
            url = f"{proxy_url}?t={cache_bust}" if cache_bust else proxy_url
            return format_html(
                '<img src="{}" alt="Equipment image" style="max-width: 300px; max-height: 300px; '
                'object-fit: contain; border: 1px solid #ddd; padding: 5px; '
                'background: #f9f9f9; border-radius: 4px;" loading="lazy" />'
                '<br><small style="color: #666; margin-top: 5px; display: block;">'
                'Stored path: {}</small>',
                url,
                obj.image.name,
            )
        except Exception as e:
            return format_html(
                '<span style="color: #dc3545;">Error loading image: {}</span>'
                '<br><small style="color: #666;">Stored path: {}</small>',
                str(e),
                obj.image.name if obj.image else "",
            )

    image_preview.short_description = _("Image Preview")
    
    def video_preview(self, obj):
        """Display equipment video preview."""
        if obj.video_file:
            try:
                # Get the URL from storage
                video_url = default_storage.url(obj.video_file.name)
                return format_html(
                    '<video controls style="max-width: 500px; max-height: 300px; '
                    'border: 1px solid #ddd; padding: 5px; background: #f9f9f9; '
                    'border-radius: 4px;">'
                    '<source src="{}" type="video/mp4">'
                    'Your browser does not support the video tag.'
                    '</video>'
                    '<br><small style="color: #666; margin-top: 5px; display: block;">'
                    '<a href="{}" target="_blank">View full video</a> | '
                    'File: {}</small>',
                    video_url,
                    video_url,
                    obj.video_file.name.split('/')[-1] if obj.video_file.name else 'N/A'
                )
            except Exception as e:
                return format_html(
                    '<span style="color: #d32f2f;">Error loading video: {}</span>',
                    str(e)
                )
        return format_html('<span style="color: #999;">No video uploaded</span>')
    
    video_preview.short_description = _("Video Preview")


# ============================================================================
# Holiday Admin
# ============================================================================

@admin.register(Holiday)
class HolidayAdmin(admin.ModelAdmin):
    """Admin configuration for Holiday model."""
    
    list_display = ['date', 'reason', 'is_active', 'color', 'created_at', 'updated_at']
    list_filter = ['is_active', 'date', 'created_at']
    search_fields = ['reason', 'date']
    date_hierarchy = 'date'
    ordering = ['-date']
    
    fieldsets = (
        (_('Holiday Information'), {
            'fields': ('date', 'reason', 'is_active', 'color')
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ['created_at', 'updated_at']
    
    def get_queryset(self, request):
        """Optimize queryset."""
        return super().get_queryset(request).select_related()
    
    def save_model(self, request, obj, form, change):
        """Save holiday and show info about Sunday holidays."""
        super().save_model(request, obj, form, change)
        
        # Check if it's a Saturday or Sunday
        weekday = obj.date.weekday()
        if weekday == 5:
            messages.info(
                request,
                _('Note: This date is a Saturday, which is automatically considered a holiday. '
                  'Slots will not be generated for this date.')
            )
        if weekday == 6:
            from django.contrib import messages
            messages.info(
                request,
                _('Note: This date is a Sunday, which is automatically considered a holiday. '
                  'The holiday entry in the table is for reference only.')
            )


# ============================================================================
# Booking Buffer Config Admin (daily 20:00 check for Booking Not Utilized)
# ============================================================================

@admin.register(BookingBufferConfig)
class BookingBufferConfigAdmin(admin.ModelAdmin):
    """Admin for buffer time (days) used by the daily 20:00 Booking Not Utilized check."""

    list_display = ["buffer_days", "enabled", "sample_retention_days", "auto_archive_enabled", "updated_at"]
    list_editable = ["enabled", "auto_archive_enabled"]
    ordering = ["pk"]

    fieldsets = (
        (
            _("Buffer settings"),
            {
                "fields": ("buffer_days", "enabled"),
                "description": _(
                    "Daily at 20:00, booked slots whose start time is older than (today minus buffer days) "
                    "are checked. If the sample is not yet marked as Sample received / Sample rejected / Processing, "
                    "the slot is marked as Booking Not Utilized and the user is notified by email (no refund). "
                    "Set buffer_days to 0 or disable to turn off the check."
                ),
            },
        ),
        (
            _("Sample lifecycle retention"),
            {
                "fields": ("sample_retention_days", "auto_archive_enabled"),
                "description": _(
                    "Daily, samples that are in 'Analyzed' for longer than the configured retention "
                    "are automatically marked as 'Archived' in Sample Lifecycle. Set sample_retention_days to 0 or "
                    "disable to turn off auto-archive."
                ),
            },
        ),
        (_("Timestamps"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ["created_at", "updated_at"]


# ============================================================================
# Internal User Slot Window (common for all equipment)
# ============================================================================

@admin.register(InternalUserSlotWindowSetting)
class InternalUserSlotWindowSettingAdmin(admin.ModelAdmin):
    """Admin for common slot window for internal users (day + time when next week opens). Singleton."""

    list_display = ["reference_weekday", "reference_time", "updated_at"]
    ordering = ["pk"]

    fieldsets = (
        (
            _("Slot window (internal users)"),
            {
                "fields": ("reference_weekday", "reference_time"),
                "description": _(
                    "Common for all equipment. Before this day and time, internal users see only the current week; "
                    "on or after it, they see current and next week. Leave both empty for no restriction. "
                    "Weekday: 0=Monday … 6=Sunday."
                ),
            },
        ),
        (_("Timestamps"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ["created_at", "updated_at"]


# ============================================================================
# Proforma Invoice Format (admin-editable template text)
# ============================================================================

@admin.register(ProformaInvoiceFormat)
class ProformaInvoiceFormatAdmin(admin.ModelAdmin):
    """Admin for proforma invoice PDF wording: terms and disclaimer. Single row (singleton)."""

    list_display = ["updated_at"]
    ordering = ["pk"]

    fieldsets = (
        (
            _("Proforma invoice format"),
            {
                "fields": ("terms_and_conditions", "disclaimer"),
                "description": _(
                    "Text shown on the proforma invoice PDF. "
                    "Terms and conditions appear just after the table. "
                    "Disclaimer appears at the bottom (e.g. computer-generated, no signature required). "
                    "Create or edit the single row to change the format."
                ),
            },
        ),
        (_("Timestamps"), {"fields": ("created_at", "updated_at"), "classes": ("collapse",)}),
    )
    readonly_fields = ["created_at", "updated_at"]

    def has_add_permission(self, request):
        # Allow only one row (singleton)
        if ProformaInvoiceFormat.objects.exists():
            return False
        return super().has_add_permission(request)


# ============================================================================
# Operating Hours Admin
# ============================================================================

# @admin.register(OperatingHours)
# class OperatingHoursAdmin(admin.ModelAdmin):
#     """Admin configuration for Operating Hours."""
    
#     list_display = ['equipment', 'day_of_week', 'is_closed', 'open_time', 'close_time']
#     list_filter = ['equipment', 'day_of_week', 'is_closed']
#     search_fields = ['equipment__code', 'equipment__name']
#     ordering = ['equipment', 'day_of_week']
    
#     fieldsets = (
#         (_('Equipment & Day'), {
#             'fields': ('equipment', 'day_of_week')
#         }),
#         (_('Hours'), {
#             'fields': ('is_closed', 'open_time', 'close_time')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ['item_code', 'name', 'category', 'uom', 'active', 'updated_at']
    list_filter = ['category', 'active']
    search_fields = ['item_code', 'name', 'specification']
    ordering = ['name']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(EquipmentInventoryItem)
class EquipmentInventoryItemAdmin(admin.ModelAdmin):
    list_display = ['equipment', 'item', 'min_level', 'reorder_level', 'critical_level', 'is_enabled']
    list_filter = ['is_enabled', 'equipment']
    search_fields = ['equipment__code', 'equipment__name', 'item__item_code', 'item__name']
    autocomplete_fields = ['equipment', 'item']
    readonly_fields = ['created_at', 'updated_at']


@admin.register(EquipmentItemStock)
class EquipmentItemStockAdmin(admin.ModelAdmin):
    list_display = ['equipment', 'item', 'current_qty', 'updated_at']
    list_filter = ['equipment']
    search_fields = ['equipment__code', 'equipment__name', 'item__item_code', 'item__name']
    autocomplete_fields = ['equipment', 'item']
    readonly_fields = ['updated_at']


class InventoryRequestLineInline(admin.TabularInline):
    model = InventoryRequestLine
    extra = 0
    autocomplete_fields = ['item']
    fields = ['item', 'requested_qty', 'approved_qty', 'issued_qty', 'estimated_unit_cost', 'remarks']


@admin.register(InventoryRequest)
class InventoryRequestAdmin(admin.ModelAdmin):
    list_display = ['request_no', 'equipment', 'requested_by', 'request_type', 'status', 'created_at', 'required_by_date']
    list_filter = ['request_type', 'status', 'equipment']
    search_fields = ['request_no', 'equipment__code', 'equipment__name', 'requested_by__email', 'requested_by__name']
    autocomplete_fields = ['equipment', 'requested_by', 'decision_by']
    readonly_fields = ['request_no', 'created_at', 'updated_at', 'submitted_at', 'decision_at']
    inlines = [InventoryRequestLineInline]


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ['transaction_id', 'equipment', 'item', 'tx_type', 'quantity', 'performed_by', 'performed_at']
    list_filter = ['tx_type', 'performed_at', 'equipment']
    search_fields = ['equipment__code', 'equipment__name', 'item__item_code', 'item__name', 'reference_type', 'reference_id']
    autocomplete_fields = ['equipment', 'item', 'performed_by']
    readonly_fields = ['created_at']


@admin.register(IssuedAsset)
class IssuedAssetAdmin(admin.ModelAdmin):
    list_display = ['equipment', 'item', 'serial_no', 'issued_to', 'status', 'issued_on', 'returned_on']
    list_filter = ['status', 'equipment', 'item']
    search_fields = ['equipment__code', 'item__item_code', 'serial_no', 'issued_to__email', 'issued_to__name']
    autocomplete_fields = ['equipment', 'item', 'issued_to']
    readonly_fields = ['created_at', 'updated_at']


# ============================================================================
# Slot System Admin
# ============================================================================

# @admin.register(SlotSettings)
# class SlotSettingsAdmin(admin.ModelAdmin):
#     """Admin configuration for Slot Settings."""
    
#     list_display = ['equipment', 'slot_duration_minutes', 'slots_per_day']
#     list_filter = ['equipment']
#     search_fields = ['equipment__code', 'equipment__name']
#     ordering = ['equipment']
    
#     fieldsets = (
#         (_('Equipment'), {
#             'fields': ('equipment',)
#         }),
#         (_('Slot Configuration'), {
#             'fields': ('slot_duration_minutes', 'slots_per_day')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']


# @admin.register(SlotMaster)
# class SlotMasterAdmin(admin.ModelAdmin):
#     """Admin configuration for Slot Master."""
    
#     list_display = ['equipment', 'slot_name', 'start_time', 'end_time', 'is_active']
#     list_filter = ['equipment', 'is_active']
#     search_fields = ['equipment__code', 'equipment__name', 'slot_name']
#     ordering = ['equipment', 'start_time']
    
#     fieldsets = (
#         (_('Equipment'), {
#             'fields': ('equipment',)
#         }),
#         (_('Slot Details'), {
#             'fields': ('slot_name', 'start_time', 'end_time', 'is_active')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']


class BulkStatusChangeForm(forms.Form):
    """Form for bulk status change action."""
    status = forms.ChoiceField(
        choices=SlotStatus.choices,
        label=_('New Status'),
        help_text=_('Select the new status for selected slots'),
        widget=forms.Select(attrs={'id': 'id_status'})
    )
    blocked_label = forms.CharField(
        required=False,
        max_length=255,
        label=_('Blocked Label'),
        help_text=_('Optional: Custom label when status is BLOCKED (only used when status is BLOCKED)'),
        widget=forms.TextInput(attrs={
            'placeholder': 'e.g., Maintenance, Repair, Operator Training, etc.',
            'id': 'id_blocked_label'
        })
    )
    
    def clean(self):
        cleaned_data = super().clean()
        status = cleaned_data.get('status')
        blocked_label = cleaned_data.get('blocked_label', '').strip()
        
        # Clear blocked_label if status is not BLOCKED
        if status != SlotStatus.BLOCKED:
            cleaned_data['blocked_label'] = None
        elif not blocked_label:
            # If status is BLOCKED but no label provided, that's okay (optional)
            cleaned_data['blocked_label'] = None
        
        return cleaned_data


@admin.register(DailySlot)
class DailySlotAdmin(admin.ModelAdmin):
    """Admin configuration for Daily Slot."""
    
    list_display = ['equipment_display', 'slot_number_display', 'date', 'start_datetime', 'end_datetime', 'status', 'blocked_label_display', 'booking_id_display']
    list_filter = ['status', 'date', 'slot_master__equipment']
    search_fields = ['slot_master__equipment__code', 'slot_master__equipment__name', 'slot_master__slot_name', 'blocked_label']
    list_editable = ['status']
    ordering = ['-date', 'start_datetime']
    date_hierarchy = 'date'
    list_per_page = 50
    actions = ['change_status_bulk']
    
    fieldsets = (
        (_('Slot Reference'), {
            'fields': ('slot_master',)
        }),
        (_('Date & Time'), {
            'fields': ('date', 'start_datetime', 'end_datetime')
        }),
        (_('Status & Booking'), {
            'fields': ('status', 'blocked_label', 'booking')
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    readonly_fields = ['created_at', 'updated_at']
    
    def equipment_display(self, obj):
        """Display equipment code."""
        if obj.slot_master and obj.slot_master.equipment:
            return obj.slot_master.equipment.code
        return '-'
    equipment_display.short_description = _('Equipment')
    equipment_display.admin_order_field = 'slot_master__equipment__code'
    
    def slot_number_display(self, obj):
        """Display slot number and name."""
        if obj.slot_master:
            slot_name = f" - {obj.slot_master.slot_name}" if obj.slot_master.slot_name else ""
            return f"Slot {obj.slot_master.slot_number}{slot_name}"
        return '-'
    slot_number_display.short_description = _('Slot')
    slot_number_display.admin_order_field = 'slot_master__slot_number'
    
    def blocked_label_display(self, obj):
        """Display blocked label if status is BLOCKED."""
        if obj.status == 'BLOCKED' and obj.blocked_label:
            return obj.blocked_label
        return '-'
    blocked_label_display.short_description = _('Blocked Label')
    blocked_label_display.admin_order_field = 'blocked_label'
    
    def booking_id_display(self, obj):
        """Display booking ID if status is BOOKED."""
        if obj.status == 'BOOKED' and obj.booking:
            return f"#{obj.booking.booking_id}"
        return '-'
    booking_id_display.short_description = _('Booking ID')
    booking_id_display.admin_order_field = 'booking__booking_id'
    
    @admin.action(description=_('Change status for selected slots'))
    def change_status_bulk(self, request, queryset):
        """Bulk action to change status for multiple slots."""
        from django.shortcuts import redirect, render
        
        # Check if this is the intermediate form submission
        # Django admin actions are called via POST, but we need to distinguish between:
        # 1. Initial action call from changelist (POST with action parameter, no 'status' field)
        # 2. Intermediate form submission (POST with 'status' field from our form)
        is_form_submission = request.method == "POST" and 'status' in request.POST
        
        # If this is the initial action call (not form submission), store IDs and show form
        if not is_form_submission:
            # Store selected IDs in session for the intermediate form
            selected_ids = [str(slot.id) for slot in queryset]
            request.session['dailyslot_selected_ids'] = selected_ids
            form = BulkStatusChangeForm()
            
            context = {
                "title": _("Change Status for {count} Selected Slot(s)").format(count=queryset.count()),
                "slots": queryset,
                "form": form,
                "opts": self.model._meta,
                "selected_ids": selected_ids,
            }
            return render(request, "admin/equipment/dailyslot/bulk_status_change.html", context)
        
        # This is the intermediate form submission
        if is_form_submission:
            # This is the intermediate form submission
            form = BulkStatusChangeForm(request.POST)
            if form.is_valid():
                # Get selected IDs from POST or session
                selected_ids = request.POST.getlist('selected_ids')
                if not selected_ids:
                    selected_ids = request.session.get('dailyslot_selected_ids', [])
                
                # Convert to integers for filtering
                try:
                    selected_ids = [int(id) for id in selected_ids if id]
                except (ValueError, TypeError):
                    selected_ids = []
                
                if not selected_ids:
                    self.message_user(request, _("No slots selected."), level="error")
                    return redirect("admin:equipment_dailyslot_changelist")
                
                # Ensure we have valid integer IDs
                if not selected_ids:
                    self.message_user(request, _("No valid slot IDs found."), level="error")
                    return redirect("admin:equipment_dailyslot_changelist")
                
                # Get fresh queryset from database
                slots_queryset = self.model.objects.filter(id__in=selected_ids)
                
                # Check if any slots were found
                if not slots_queryset.exists():
                    self.message_user(request, _("No slots found with the provided IDs: {ids}").format(ids=selected_ids), level="error")
                    return redirect("admin:equipment_dailyslot_changelist")
                
                new_status = form.cleaned_data["status"]
                blocked_label = form.cleaned_data.get("blocked_label", "").strip()
                
                # Clear blocked_label if status is not BLOCKED
                if new_status != SlotStatus.BLOCKED:
                    blocked_label = None
                
                count = 0
                errors = []
                available_slot_ids_by_equipment = {}
                
                # Process each slot
                for slot in slots_queryset:
                    try:
                        old_status = slot.status
                        # Don't change status if slot is booked (has a booking)
                        if slot.booking_id and new_status != SlotStatus.BOOKED:
                            errors.append(
                                _("Slot {slot} (ID: {id}) has a booking and cannot be changed to {status}").format(
                                    slot=str(slot),
                                    id=slot.id,
                                    status=form.cleaned_data["status"]
                                )
                            )
                            continue
                        
                        # Update status and blocked_label
                        slot.status = new_status
                        if new_status == SlotStatus.BLOCKED:
                            slot.blocked_label = blocked_label if blocked_label else None
                        else:
                            slot.blocked_label = None
                        
                        # Save the slot - use save() without update_fields to ensure all fields are saved
                        slot.save()
                        if old_status != SlotStatus.AVAILABLE and new_status == SlotStatus.AVAILABLE and slot.slot_master_id and slot.slot_master.equipment_id:
                            available_slot_ids_by_equipment.setdefault(slot.slot_master.equipment_id, []).append(slot.id)
                        
                        count += 1
                    except Exception as e:
                        errors.append(_("Slot {slot} (ID: {id}): {error}").format(
                            slot=str(slot),
                            id=slot.id,
                            error=str(e)
                        ))
                
                if count > 0:
                    self.message_user(
                        request,
                        _("Successfully changed status to '{status}' for {count} slot(s).").format(
                            status=dict(SlotStatus.choices)[new_status],
                            count=count
                        ),
                    )
                if errors:
                    self.message_user(
                        request,
                        _("Errors: {errors}").format(errors='; '.join(errors[:10])),  # Limit to first 10 errors
                        level="error",
                    )
                if available_slot_ids_by_equipment:
                    try:
                        from iic_booking.equipment.waitlist import notify_waitlist_slots_available
                        equipment_map = {
                            e.equipment_id: e
                            for e in Equipment.objects.filter(equipment_id__in=list(available_slot_ids_by_equipment.keys()))
                        }
                        for equipment_id, slot_ids in available_slot_ids_by_equipment.items():
                            equipment = equipment_map.get(equipment_id)
                            if not equipment:
                                continue
                            notify_waitlist_slots_available(
                                equipment,
                                preferred_slot_ids=slot_ids,
                                respect_reschedule_threshold=True,
                            )
                    except Exception as e:
                        logger.warning("Failed to notify waitlist after DailySlot bulk AVAILABLE update: %s", e)
                # Clear session
                if 'dailyslot_selected_ids' in request.session:
                    del request.session['dailyslot_selected_ids']
                return redirect("admin:equipment_dailyslot_changelist")
            else:
                # Form is invalid, show errors
                selected_ids = request.POST.getlist('selected_ids')
                if not selected_ids:
                    selected_ids = request.session.get('dailyslot_selected_ids', [])
                try:
                    selected_ids = [int(id) for id in selected_ids if id]
                except (ValueError, TypeError):
                    selected_ids = []
                slots_queryset = self.model.objects.filter(id__in=selected_ids) if selected_ids else queryset
                context = {
                    "title": _("Change Status for {count} Selected Slot(s)").format(count=slots_queryset.count()),
                    "slots": slots_queryset,
                    "form": form,
                    "opts": self.model._meta,
                    "selected_ids": selected_ids if selected_ids else [str(slot.id) for slot in queryset],
                }
                return render(request, "admin/equipment/dailyslot/bulk_status_change.html", context)
    
    change_status_bulk.allowed_permissions = ("change",)

    def save_model(self, request, obj, form, change):
        """When a DailySlot is made AVAILABLE from admin UI, trigger waitlist confirmation."""
        old_status = None
        if change and obj.pk:
            try:
                old_status = DailySlot.objects.only("status").get(pk=obj.pk).status
            except DailySlot.DoesNotExist:
                old_status = None
        super().save_model(request, obj, form, change)
        if obj.status == SlotStatus.AVAILABLE and old_status != SlotStatus.AVAILABLE:
            equipment = getattr(getattr(obj, "slot_master", None), "equipment", None)
            if equipment:
                try:
                    from iic_booking.equipment.waitlist import notify_waitlist_slots_available
                    notify_waitlist_slots_available(
                        equipment,
                        preferred_slot_ids=[obj.id],
                    )
                except Exception as e:
                    logger.warning("Failed to notify waitlist for DailySlot %s after AVAILABLE update: %s", obj.id, e)

# ============================================================================
# Booking Admin
# ============================================================================

class DailySlotInline(admin.TabularInline):
    """Inline admin for Daily Slots associated with a Booking."""
    model = DailySlot
    extra = 0
    fields = ['slot_master', 'date', 'start_datetime', 'end_datetime', 'status', 'booking_id_display']
    readonly_fields = ['slot_master', 'date', 'start_datetime', 'end_datetime', 'status', 'booking_id_display']
    can_delete = False
    classes = ['collapse']
    fk_name = 'booking'
    
    def has_add_permission(self, request, obj=None):
        """Disable adding slots from booking admin."""
        return False
    
    def booking_id_display(self, obj):
        """Display booking ID if status is BOOKED."""
        if obj.status == 'BOOKED' and obj.booking:
            return f"#{obj.booking.booking_id}"
        return '-'
    booking_id_display.short_description = _('Booking ID')


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    """Admin configuration for Booking."""
    
    list_display = ['booking_id', 'virtual_booking_id', 'equipment', 'user', 'status', 'rating', 'total_time_minutes', 'total_charge', 'created_at']
    list_filter = ['status', 'equipment', 'user_type_snapshot', 'created_at']
    search_fields = ['booking_id', 'virtual_booking_id', 'equipment__code', 'user__email', 'user__name']
    ordering = ['-created_at']
    date_hierarchy = 'created_at'
    readonly_fields = ['booking_id', 'virtual_booking_id', 'created_at', 'updated_at', 'rated_at']
    
    inlines = [DailySlotInline]
    
    fieldsets = (
        (_('Booking Information'), {
            'fields': ('booking_id', 'virtual_booking_id', 'user', 'user_type_snapshot', 'equipment', 'charge_profile', 'status')
        }),
        (_('Time & Charge'), {
            'fields': ('total_time_minutes', 'total_charge')
        }),
        (_('Input Values'), {
            'fields': ('input_values', 'selected_parameters'),
            'description': _('Dynamic input values and selected parameters')
        }),
        (_('Charge Breakdown'), {
            'fields': ('charge_breakdown',),
            'description': _('Line-by-line charge breakdown for audit')
        }),
        (_('Notes'), {
            'fields': ('notes',)
        }),
        (_('User Rating'), {
            'fields': ('rating', 'rating_feedback', 'rated_at'),
            'description': _('Rating and feedback submitted by the booking user')
        }),
        (_('Timestamps'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )


# ============================================================================
# Repeat sample request
# ============================================================================

@admin.register(RepeatSampleRequest)
class RepeatSampleRequestAdmin(admin.ModelAdmin):
    list_display = ['id', 'booking', 'user_email', 'equipment', 'status', 'requested_at', 'responded_at', 'new_booking']
    list_filter = ['status', 'requested_at']
    search_fields = ['booking__virtual_booking_id', 'booking__user__email', 'booking__equipment__code']
    readonly_fields = ['booking', 'requested_at', 'responded_at', 'responded_by', 'new_booking']
    date_hierarchy = 'requested_at'

    def user_email(self, obj):
        return obj.booking.user.email if obj.booking and obj.booking.user else '-'
    user_email.short_description = _('User email')

    def equipment(self, obj):
        return obj.booking.equipment.code if obj.booking and obj.booking.equipment else '-'
    equipment.short_description = _('Equipment')

    def has_add_permission(self, request):
        return False


# ============================================================================
# Booking requests log (BookingAttemptLog)
# ============================================================================

@admin.register(BookingAttemptLog)
class BookingAttemptLogAdmin(admin.ModelAdmin):
    """Admin for Booking attempt log – view-only list of equipment_bookingattemptlog with filters."""

    list_display = [
        'id',
        'user',
        'equipment',
        'requested_at',
        'outcome',
        'failure_reason_short',
        'number_of_samples',
        'slots_requested',
        'duration_minutes',
        'booking_id',
    ]
    list_filter = [
        'outcome',
        'equipment',
        'requested_at',
    ]
    search_fields = [
        'user__email',
        'user__name',
        'equipment__code',
        'equipment__name',
        'failure_reason',
    ]
    date_hierarchy = 'requested_at'
    ordering = ['-requested_at']
    list_per_page = 50
    readonly_fields = [
        'user',
        'equipment',
        'requested_at',
        'outcome',
        'failure_reason',
        'number_of_samples',
        'slots_requested',
        'duration_minutes',
        'booking_id',
    ]

    def failure_reason_short(self, obj):
        if not obj.failure_reason:
            return '-'
        return obj.failure_reason[:60] + '…' if len(obj.failure_reason) > 60 else obj.failure_reason

    failure_reason_short.short_description = _('Failure reason')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


# ============================================================================
# Holiday Calendar Admin
# ============================================================================

# @admin.register(HolidayCalendar)
# class HolidayCalendarAdmin(admin.ModelAdmin):
#     """Admin configuration for Holiday Calendar."""
    
#     list_display = ['equipment', 'holiday_name', 'start_date', 'end_date', 'holiday_type']
#     list_filter = ['holiday_type', 'equipment', 'start_date']
#     search_fields = ['holiday_name', 'equipment__code', 'equipment__name']
#     ordering = ['-start_date']
#     date_hierarchy = 'start_date'
    
#     fieldsets = (
#         (_('Equipment'), {
#             'fields': ('equipment',),
#             'description': _('Leave empty for global holidays')
#         }),
#         (_('Holiday Details'), {
#             'fields': ('holiday_name', 'start_date', 'end_date', 'holiday_type')
#         }),
#         (_('Partial Holiday Times'), {
#             'fields': ('open_time', 'close_time'),
#             'description': _('Required for partial holidays only')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']


# ============================================================================
# Quota Admin
# ============================================================================

# @admin.register(UserTypeQuota)
# class UserTypeQuotaAdmin(admin.ModelAdmin):
#     """Admin configuration for User Type Quota."""
    
#     list_display = ['equipment', 'user_type', 'quota_type', 'limit_type', 'limit_value', 'is_enforced']
#     list_filter = ['equipment', 'user_type', 'quota_type', 'limit_type', 'is_enforced']
#     search_fields = ['equipment__code', 'equipment__name', 'user_type']
#     ordering = ['equipment', 'user_type', 'quota_type']
    
#     fieldsets = (
#         (_('Equipment & User Type'), {
#             'fields': ('equipment', 'user_type')
#         }),
#         (_('Quota Configuration'), {
#             'fields': ('quota_type', 'limit_type', 'limit_value', 'is_enforced')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']


@admin.register(Semester)
class SemesterAdmin(admin.ModelAdmin):
    list_display = ["code", "name", "start_date", "end_date", "is_active"]
    list_filter = ["is_active"]
    search_fields = ["code", "name"]
    ordering = ["-start_date"]


@admin.register(StudentEquipmentNomination)
class StudentEquipmentNominationAdmin(admin.ModelAdmin):
    list_display = ["student", "equipment", "semester", "supervisor", "status", "nominated_at", "approved_at"]
    list_filter = ["status", "semester", "equipment"]
    search_fields = ["student__email", "student__name", "supervisor__email", "equipment__code"]
    raw_id_fields = ["student", "supervisor", "equipment", "semester", "approved_by"]
    readonly_fields = ["nominated_at"]
    date_hierarchy = "nominated_at"


@admin.register(EquipmentOperatingTACall)
class EquipmentOperatingTACallAdmin(admin.ModelAdmin):
    list_display = ["equipment", "semester", "number_of_operators_required", "nomination_deadline", "status", "created_by", "email_sent_at", "created_at"]
    list_filter = ["status", "semester", "equipment"]
    search_fields = ["equipment__code", "equipment__name"]
    raw_id_fields = ["equipment", "semester", "created_by"]
    readonly_fields = ["created_at", "updated_at", "email_sent_at"]
    date_hierarchy = "created_at"


# @admin.register(ExternalUserQuota)
# class ExternalUserQuotaAdmin(admin.ModelAdmin):
#     """Admin configuration for External User Quota."""
    
#     list_display = ['equipment', 'quota_type', 'limit_type', 'limit_value', 'is_paid', 'is_enforced']
#     list_filter = ['equipment', 'quota_type', 'limit_type', 'is_paid', 'is_enforced']
#     search_fields = ['equipment__code', 'equipment__name']
#     ordering = ['equipment', 'quota_type']
    
#     fieldsets = (
#         (_('Equipment'), {
#             'fields': ('equipment',)
#         }),
#         (_('Quota Configuration'), {
#             'fields': ('quota_type', 'limit_type', 'limit_value', 'is_paid', 'is_enforced')
#         }),
#         (_('Timestamps'), {
#             'fields': ('created_at', 'updated_at'),
#             'classes': ('collapse',)
#         }),
#     )
    
#     readonly_fields = ['created_at', 'updated_at']
