from rest_framework import serializers

from iic_booking.users.models import User, UserType, Gender
from iic_booking.users.models.department import DepartmentType
from django.utils.dateparse import parse_date


class OmniportCallbackSerializer(serializers.Serializer):
    """Serializer for Omniport OAuth callback."""

    code = serializers.CharField(required=True)
    state = serializers.CharField(required=False, allow_blank=True, max_length=255)


class OmniportAuthResponseSerializer(serializers.Serializer):
    """Serializer for Omniport authentication response."""

    token = serializers.CharField()
    user = serializers.DictField()
    auth_url = serializers.URLField(required=False)


class LoginSerializer(serializers.Serializer):
    """Serializer for email/password login."""

    email = serializers.EmailField(required=True)
    password = serializers.CharField(required=True, write_only=True, style={"input_type": "password"})


class LoginResponseSerializer(serializers.Serializer):
    """Serializer for login response."""

    token = serializers.CharField()
    user = serializers.DictField()


class RegisterSerializer(serializers.Serializer):
    """Serializer for user registration."""

    email = serializers.EmailField(required=True)
    password = serializers.CharField(
        required=True,
        write_only=True,
        style={"input_type": "password"},
        min_length=8,
        help_text="Password must be at least 8 characters long",
    )
    password_confirm = serializers.CharField(
        required=True,
        write_only=True,
        style={"input_type": "password"},
        help_text="Confirm your password",
    )
    name = serializers.CharField(required=True, max_length=255)
    gender = serializers.ChoiceField(
        choices=[],  # Set in __init__
        required=True,
        help_text="Gender (required)",
    )
    user_type = serializers.ChoiceField(
        required=True,
        choices=[],  # Will be set dynamically in __init__
        help_text="User type: external users, or student/individual_student with alias (IITR Post Doctoral Fellows, etc.).",
    )
    
    def __init__(self, *args, **kwargs):
        """Initialize serializer with dynamic choices from UserType and Gender."""
        super().__init__(*args, **kwargs)
        # External users + student and individual_student (for alias types)
        register_codes = UserType.get_external_user_codes() | {
            UserType.STUDENT,
            UserType.INDIVIDUAL_STUDENT,
            UserType.STARTUP_INCUBATED_IITR,
        }
        choices_dict = dict(UserType.get_choices())
        self.fields['user_type'].choices = [(code, choices_dict.get(code, code)) for code in register_codes]
        self.fields['gender'].choices = list(Gender.get_choices())

    user_type_alias = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=100,
        help_text="Display alias when user_type is student or individual_student (e.g. IITR Post Doctoral Fellows).",
    )
    emp_id = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=50,
        help_text="Employee/Student ID (optional)",
    )
    phone_number = serializers.CharField(
        required=True,
        allow_blank=False,
        max_length=20,
        help_text="Indian mobile number (10 digits, starting with 6, 7, 8, or 9)",
    )
    department = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="Department ID (optional; for Govt R&D can be omitted if organization_request is provided).",
    )
    organization_request = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="OrganizationRequest ID (Govt R&D only): use when user requested a new organization and signup proceeds with that request.",
    )
    supervisor = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.filter(
            user_type=UserType.FACULTY,
            is_active=True,
            department__department_type=DepartmentType.INTERNAL,
        ),
        required=False,
        allow_null=True,
        help_text="IITR Internal Faculty supervisor ID (required for Post Doctoral Fellows and Research Associates in Projects)",
    )
    program_start_date = serializers.DateField(
        required=False,
        allow_null=True,
        help_text="Start date of program/course/position (optional). Used with end date; if duration > 1 year, access is put on hold until revalidation.",
    )
    program_end_date = serializers.DateField(
        required=True,
        help_text="Current program/employment validity (date). Access is disabled after this date.",
    )
    profile_picture = serializers.ImageField(
        required=True,
        help_text="Profile picture (required, image file)",
    )
    # Note: documents, document_types, and document_descriptions are handled separately
    # in the view to avoid issues with multipart/form-data array notation
    # They are not validated by the serializer but processed directly from request.FILES

    def validate_email(self, value):
        """Validate that email is unique."""
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate_emp_id(self, value):
        """Validate that emp_id is unique if provided."""
        if value:
            if User.objects.filter(emp_id=value).exists():
                raise serializers.ValidationError("A user with this employee ID already exists.")
        return value

    def validate_phone_number(self, value):
        """Validate Indian mobile number: 10 digits, starting with 6, 7, 8, or 9. Optional +91 or 0 prefix."""
        import re
        raw = (value or "").strip()
        if not raw:
            raise serializers.ValidationError("Phone number is required.")
        # Strip optional +91 or leading 0
        digits = re.sub(r"^\+91|^0+", "", raw)
        digits = re.sub(r"\s+", "", digits)
        if not re.match(r"^[6-9]\d{9}$", digits):
            raise serializers.ValidationError(
                "Enter a valid 10-digit Indian mobile number (e.g. 9876543210). It must start with 6, 7, 8, or 9."
            )
        return raw
    
    def validate(self, attrs):
        """Validate that passwords match; require user_type_alias when user_type is student or individual_student."""
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        ut = attrs.get("user_type")
        if ut in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
            alias = (attrs.get("user_type_alias") or "").strip()
            if not alias:
                raise serializers.ValidationError({
                    "user_type_alias": "A display name (e.g. IITR Post Doctoral Fellows) is required for this user type."
                })
            attrs["user_type_alias"] = alias[:100]
            # Supervisor required for IITR Post Doctoral Fellows and IITR Research Associates in Projects
            if alias in ("IITR Post Doctoral Fellows", "IITR Research Associates in Projects"):
                if not attrs.get("supervisor"):
                    raise serializers.ValidationError({
                        "supervisor": "Please select your IITR Faculty supervisor."
                    })
        else:
            attrs["user_type_alias"] = None
            attrs["supervisor"] = None
        # Govt R&D: require either department or organization_request (requested new organization)
        if ut == UserType.RND:
            org_req_id = attrs.get("organization_request")
            dept_id = attrs.get("department")
            if not dept_id and not org_req_id:
                raise serializers.ValidationError({
                    "department": "Select an organization from the list or request a new organization name and use that for signup."
                })
            if dept_id and org_req_id:
                attrs["organization_request"] = None  # prefer department when both sent
        else:
            attrs["organization_request"] = None
        # Validate program dates: end_date required; if start_date given, start <= end
        end_date = attrs.get("program_end_date")
        start_date = attrs.get("program_start_date")
        if end_date:
            if isinstance(end_date, str):
                end_date = parse_date(end_date)
            if start_date:
                if isinstance(start_date, str):
                    start_date = parse_date(start_date)
                if start_date and end_date and start_date > end_date:
                    raise serializers.ValidationError({
                        "program_start_date": "Start date must be on or before end date."
                    })
        return attrs


class RegisterResponseSerializer(serializers.Serializer):
    """Serializer for registration response."""

    token = serializers.CharField(required=False, allow_null=True)
    user = serializers.DictField()
    message = serializers.CharField()


class ResendVerificationEmailSerializer(serializers.Serializer):
    """Serializer for resend verification email request."""

    email = serializers.EmailField(required=True)


class UserTypeSerializer(serializers.Serializer):
    """Serializer for user type information."""

    code = serializers.CharField()
    name = serializers.CharField()
    description = serializers.CharField(required=False, allow_blank=True)
    alias = serializers.CharField(required=False, allow_blank=True, allow_null=True)

