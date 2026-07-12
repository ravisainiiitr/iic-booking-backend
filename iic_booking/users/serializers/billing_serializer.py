from rest_framework import serializers

from ..models import ExternalBillingProfile


class ExternalBillingProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalBillingProfile
        fields = [
            "billing_name",
            "gstin",
            "billing_address_line1",
            "billing_address_line2",
            "billing_city",
            "billing_state",
            "billing_pincode",
            "billing_country",
            "shipping_same_as_billing",
            "shipping_name",
            "shipping_phone",
            "shipping_address_line1",
            "shipping_address_line2",
            "shipping_city",
            "shipping_state",
            "shipping_pincode",
            "shipping_country",
        ]

