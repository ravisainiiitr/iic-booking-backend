"""Department model."""

from django.conf import settings
from django.db.models import SET_NULL
from django.db.models import CharField
from django.db.models import DateTimeField
from django.db.models import ForeignKey
from django.db.models import Model
from django.db.models import TextField
from django.utils.translation import gettext_lazy as _


class DepartmentType:
    """Department type constants (for user departments only; equipment uses EquipmentCategory)."""

    INTERNAL = "internal"
    EXTERNAL = "external"

    @classmethod
    def get_choices(cls):
        """Get department type choices."""
        return [
            (cls.INTERNAL, _("Internal")),
            (cls.EXTERNAL, _("External")),
        ]


class InternalDepartmentSubcategory:
    """Subcategory for Internal departments only."""

    IIT_ROORKEE_DEPT_CENTRES = "iit_roorkee_dept_centres"
    STARTUPS = "startups"

    @classmethod
    def get_choices(cls):
        return [
            (cls.IIT_ROORKEE_DEPT_CENTRES, _("IIT Roorkee Department/Centres")),
            (cls.STARTUPS, _("Startups")),
        ]


class ExternalDepartmentSubcategory:
    """Subcategory for External departments only.

    Separate lists are maintained for each subcategory per State/UT:
    - Educational Institute (educational_institute)
    - Govt R&D Organizations (govt_rnd)
    - Industry (industries)
    """

    EDUCATIONAL_INSTITUTE = "educational_institute"
    GOVT_RND = "govt_rnd"
    INDUSTRIES = "industries"
    EXTERNAL_STARTUP_MSME = "external_startup_msme"

    @classmethod
    def get_choices(cls):
        return [
            (cls.EDUCATIONAL_INSTITUTE, _("Educational Institute")),
            (cls.GOVT_RND, _("Govt R&D Organizations")),
            (cls.INDUSTRIES, _("Industries")),
            (cls.EXTERNAL_STARTUP_MSME, _("External Startup/MSME")),
        ]


class IndianState:
    """Indian states and union territories for External department location."""

    ANDHRA_PRADESH = "andhra_pradesh"
    ARUNACHAL_PRADESH = "arunachal_pradesh"
    ASSAM = "assam"
    BIHAR = "bihar"
    CHHATTISGARH = "chhattisgarh"
    GOA = "goa"
    GUJARAT = "gujarat"
    HARYANA = "haryana"
    HIMACHAL_PRADESH = "himachal_pradesh"
    JHARKHAND = "jharkhand"
    KARNATAKA = "karnataka"
    KERALA = "kerala"
    MADHYA_PRADESH = "madhya_pradesh"
    MAHARASHTRA = "maharashtra"
    MANIPUR = "manipur"
    MEGHALAYA = "meghalaya"
    MIZORAM = "mizoram"
    NAGALAND = "nagaland"
    ODISHA = "odisha"
    PUNJAB = "punjab"
    RAJASTHAN = "rajasthan"
    SIKKIM = "sikkim"
    TAMIL_NADU = "tamil_nadu"
    TELANGANA = "telangana"
    TRIPURA = "tripura"
    UTTAR_PRADESH = "uttar_pradesh"
    UTTARAKHAND = "uttarakhand"
    WEST_BENGAL = "west_bengal"
    ANDAMAN_NICOBAR = "andaman_nicobar"
    CHANDIGARH = "chandigarh"
    DADRA_NAGAR_HAVELI_DAMAN_DIU = "dadra_nagar_haveli_daman_diu"
    DELHI = "delhi"
    JAMMU_KASHMIR = "jammu_kashmir"
    LADAKH = "ladakh"
    LAKSHADWEEP = "lakshadweep"
    PUDUCHERRY = "puducherry"

    # Union territory values (rest are states) - for API to return type in signup dropdown.
    # Use string literals here because cls is not defined at class definition time.
    UNION_TERRITORIES = {
        "andaman_nicobar",
        "chandigarh",
        "dadra_nagar_haveli_daman_diu",
        "delhi",
        "jammu_kashmir",
        "ladakh",
        "lakshadweep",
        "puducherry",
    }

    @classmethod
    def get_choices(cls):
        return [
            (cls.ANDHRA_PRADESH, _("Andhra Pradesh")),
            (cls.ARUNACHAL_PRADESH, _("Arunachal Pradesh")),
            (cls.ASSAM, _("Assam")),
            (cls.BIHAR, _("Bihar")),
            (cls.CHHATTISGARH, _("Chhattisgarh")),
            (cls.GOA, _("Goa")),
            (cls.GUJARAT, _("Gujarat")),
            (cls.HARYANA, _("Haryana")),
            (cls.HIMACHAL_PRADESH, _("Himachal Pradesh")),
            (cls.JHARKHAND, _("Jharkhand")),
            (cls.KARNATAKA, _("Karnataka")),
            (cls.KERALA, _("Kerala")),
            (cls.MADHYA_PRADESH, _("Madhya Pradesh")),
            (cls.MAHARASHTRA, _("Maharashtra")),
            (cls.MANIPUR, _("Manipur")),
            (cls.MEGHALAYA, _("Meghalaya")),
            (cls.MIZORAM, _("Mizoram")),
            (cls.NAGALAND, _("Nagaland")),
            (cls.ODISHA, _("Odisha")),
            (cls.PUNJAB, _("Punjab")),
            (cls.RAJASTHAN, _("Rajasthan")),
            (cls.SIKKIM, _("Sikkim")),
            (cls.TAMIL_NADU, _("Tamil Nadu")),
            (cls.TELANGANA, _("Telangana")),
            (cls.TRIPURA, _("Tripura")),
            (cls.UTTAR_PRADESH, _("Uttar Pradesh")),
            (cls.UTTARAKHAND, _("Uttarakhand")),
            (cls.WEST_BENGAL, _("West Bengal")),
            (cls.ANDAMAN_NICOBAR, _("Andaman and Nicobar Islands")),
            (cls.CHANDIGARH, _("Chandigarh")),
            (cls.DADRA_NAGAR_HAVELI_DAMAN_DIU, _("Dadra and Nagar Haveli and Daman and Diu")),
            (cls.DELHI, _("Delhi")),
            (cls.JAMMU_KASHMIR, _("Jammu and Kashmir")),
            (cls.LADAKH, _("Ladakh")),
            (cls.LAKSHADWEEP, _("Lakshadweep")),
            (cls.PUDUCHERRY, _("Puducherry")),
        ]

    @classmethod
    def get_choices_with_type(cls):
        """Return list of (value, label, type) with type 'state' or 'union_territory' for signup dropdown."""
        return [
            (value, str(label), "union_territory" if value in cls.UNION_TERRITORIES else "state")
            for value, label in cls.get_choices()
        ]


class Department(Model):
    """Department model for organizing users."""

    name = CharField(
        _("Department Name"),
        max_length=255,
        unique=True,
        help_text=_("Name of the department"),
    )
    code = CharField(
        _("Department Code"),
        max_length=50,
        unique=True,
        blank=True,
        null=True,
        help_text=_("Short code for the department"),
    )
    department_type = CharField(
        _("Department Type"),
        max_length=50,
        choices=DepartmentType.get_choices(),
        default=DepartmentType.INTERNAL,
        help_text=_("Type of department: Internal or External"),
    )
    internal_subcategory = CharField(
        _("Internal subcategory"),
        max_length=50,
        choices=InternalDepartmentSubcategory.get_choices(),
        blank=True,
        null=True,
        help_text=_("For Internal departments only: IIT Roorkee Department/Centres or Startups"),
    )
    external_subcategory = CharField(
        _("External subcategory"),
        max_length=50,
        choices=ExternalDepartmentSubcategory.get_choices(),
        blank=True,
        null=True,
        help_text=_("For External departments only: Educational Institute, Govt R&D Organizations, Industries, or External Startup/MSME"),
    )
    state = CharField(
        _("State / Union Territory"),
        max_length=50,
        choices=IndianState.get_choices(),
        blank=True,
        null=True,
        help_text=_("For External departments only: Indian state or union territory"),
    )
    description = TextField(
        _("Description"),
        blank=True,
        help_text=_("Optional description of the department"),
    )
    head = ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="headed_departments",
        verbose_name=_("Department head"),
        help_text=_(
            "Optional. If set, this user receives informational emails (e.g. OIC self-service leave) for members of this department."
        ),
    )
    internal_grant_code = CharField(
        _("Internal grant code"),
        max_length=80,
        blank=True,
        help_text=_(
            "SRIC / accounts grant code for this internal department (instrument cost centre). "
            "Used in SRIC transfer API and recharge workflows."
        ),
    )
    created_at = DateTimeField(_("Created at"), auto_now_add=True)
    updated_at = DateTimeField(_("Updated at"), auto_now=True)

    class Meta:
        verbose_name = _("Department")
        verbose_name_plural = _("Departments")
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name

