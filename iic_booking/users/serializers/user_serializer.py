"""Serializers for User model."""

from decimal import Decimal

from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from ..models import User
from ..models import UserType


class AdminUserSetPasswordSerializer(serializers.Serializer):
    """Serializer for admin set-password (mirrors Django admin/users/user/<id>/password/)."""

    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})
    password_confirm = serializers.CharField(write_only=True, style={"input_type": "password"})

    def validate(self, attrs):
        if attrs["password"] != attrs["password_confirm"]:
            raise serializers.ValidationError({"password_confirm": "Passwords do not match."})
        validate_password(attrs["password"], self.context.get("user"))
        return attrs


class AdminUserCreateSerializer(serializers.ModelSerializer[User]):
    """Serializer for admin create user (mirrors Django admin/users/user/add/)."""

    password = serializers.CharField(write_only=True, min_length=8, style={"input_type": "password"})

    class Meta:
        model = User
        fields = [
            "email",
            "name",
            "password",
            "user_type",
            "user_type_alias",
            "emp_id",
            "phone_number",
            "department",
        ]
        extra_kwargs = {
            "name": {"required": False, "allow_blank": True},
            "emp_id": {"required": False, "allow_blank": True},
            "phone_number": {"required": False, "allow_blank": True},
            "department": {"required": False, "allow_null": True},
            "user_type": {"required": False, "allow_blank": True},
            "user_type_alias": {"required": False, "allow_blank": True},
        }

    def validate(self, attrs):
        ut = attrs.get("user_type")
        if ut != UserType.STUDENT and ut != UserType.INDIVIDUAL_STUDENT and attrs.get("user_type_alias"):
            attrs["user_type_alias"] = None
        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        user_type_alias = validated_data.pop("user_type_alias", None)
        if validated_data.get("user_type") not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
            user_type_alias = None
        user = User.objects.create_user(
            email=validated_data.get("email"),
            password=password,
            name=validated_data.get("name") or "",
            user_type=validated_data.get("user_type"),
            emp_id=validated_data.get("emp_id") or None,
            phone_number=validated_data.get("phone_number") or None,
            department=validated_data.get("department"),
        )
        if user_type_alias and str(user_type_alias).strip():
            user.user_type_alias = str(user_type_alias).strip()[:100]
            user.save(update_fields=["user_type_alias"])
        return user


class AdminUserUpdateSerializer(serializers.ModelSerializer[User]):
    """Serializer for admin update user (mirrors Django admin/users/user/<id>/change/)."""

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "name",
            "user_type",
            "user_type_alias",
            "emp_id",
            "phone_number",
            "department",
            "email_verified",
            "admin_approved",
            "force_inactive",
            "use_discounted_charge_profile",
            "oic_enable_ta_nomination",
            "oic_enable_ta_duty_assignments",
            "oic_enable_leave_management",
            "oic_enable_reward_config",
        ]
        read_only_fields = ["id", "email"]

    def validate(self, attrs):
        # user_type_alias only valid when user_type is STUDENT or INDIVIDUAL_STUDENT
        ut = attrs.get("user_type")
        if ut is None and self.instance:
            ut = self.instance.user_type
        if ut not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
            attrs["user_type_alias"] = None
        # Business rule: once Admin Approved, treat user as verified/active (no further action).
        # Ensure email_verified is set when admin_approved is enabled via admin edit.
        if attrs.get("admin_approved") is True:
            attrs["email_verified"] = True
        return attrs

    def to_representation(self, instance):
        """Include department_code, department_name, user_type_display for response."""
        data = super().to_representation(instance)
        data["department_code"] = instance.department.code if instance.department and instance.department.code else None
        data["department_name"] = instance.department.name if instance.department else None
        data["user_type_display"] = instance.get_user_type_display_label()
        data["is_active"] = instance.is_active
        data["force_inactive"] = instance.force_inactive
        return data


class UserSerializer(serializers.ModelSerializer[User]):
    """Serializer for User model."""

    department_code = serializers.CharField(
        source="department.code", read_only=True
    )
    department_name = serializers.CharField(
        source="department.name", read_only=True
    )
    department_type = serializers.CharField(
        source="department.department_type", read_only=True, allow_null=True
    )
    user_type_display = serializers.SerializerMethodField()

    can_have_wallet = serializers.SerializerMethodField()

    is_faculty = serializers.SerializerMethodField()

    profile_picture = serializers.ImageField(read_only=True)

    def get_user_type_display(self, obj):
        return obj.get_user_type_display_label()

    def get_can_have_wallet(self, obj):
        return obj.can_have_wallet()

    def get_is_faculty(self, obj):
        return obj.is_faculty()

    def validate(self, attrs):
        enabled = attrs.get("wallet_low_balance_alert_enabled")
        if enabled is True:
            threshold = attrs.get("wallet_low_balance_alert_threshold")
            if threshold is None and self.instance:
                threshold = getattr(self.instance, "wallet_low_balance_alert_threshold", None)
            if threshold is None or (isinstance(threshold, (int, float, Decimal)) and Decimal(str(threshold)) <= 0):
                raise serializers.ValidationError(
                    {"wallet_low_balance_alert_threshold": "A positive amount is required when low balance alert is enabled."}
                )
        elif enabled is False:
            attrs["wallet_low_balance_alert_threshold"] = None
        if attrs.get("istem_portal_acknowledged") is True:
            inst = self.instance
            ut = (inst.user_type if inst else None) or ""
            if inst and not UserType.is_external_user(ut or ""):
                raise serializers.ValidationError(
                    {
                        "istem_portal_acknowledged": (
                            "Only external category users (Educational Institute, Govt R&D, Industry, Other) "
                            "can confirm I-STEM portal registration."
                        )
                    }
                )
        return attrs

    class Meta:
        model = User
        fields = [
            "id",
            "name",
            "email",
            "gender",
            "user_type",
            "user_type_alias",
            "user_type_display",
            "is_faculty",
            "emp_id",
            "phone_number",
            "secondary_phone_number",
            "profile_picture",
            "department",
            "department_code",
            "department_name",
            "department_type",
            "can_have_wallet",
            "date_of_birth",
            "branch_name",
            "degree_name",
            "designation",
            "email_verified",
            "admin_approved",
            "is_active",
            "force_inactive",
            "date_joined",
            "last_login",
            "auto_slot_selection",
            "wallet_low_balance_alert_enabled",
            "wallet_low_balance_alert_threshold",
            "use_discounted_charge_profile",
            "istem_portal_acknowledged",
            "oic_enable_ta_nomination",
            "oic_enable_ta_duty_assignments",
            "oic_enable_leave_management",
            "oic_enable_reward_config",
        ]
        read_only_fields = [
            "id",
            "email",
            "department_code",
            "department_name",
            "department_type",
            "user_type_display",
            "is_faculty",
            "user_type_alias",
            "oic_enable_ta_nomination",
            "oic_enable_ta_duty_assignments",
            "oic_enable_leave_management",
            "oic_enable_reward_config",
        ]
    
    def to_representation(self, instance):
        """Override to handle invalid or missing profile picture paths gracefully."""
        data = super().to_representation(instance)
        url = instance.get_profile_picture_url_or_none()
        data["profile_picture"] = url
        return data

