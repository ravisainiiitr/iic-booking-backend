"""Rate limits for payment routes. Rates live in REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"] and can be set by environment."""

from rest_framework.throttling import AnonRateThrottle
from rest_framework.throttling import UserRateThrottle


class PaymentUserThrottle(UserRateThrottle):
    """Starting and verifying payments, per signed-in user."""

    scope = "payments_user"


class PaymentWebhookThrottle(AnonRateThrottle):
    """Webhook deliveries, per source address. Generous: Razorpay retries anything it cannot deliver."""

    scope = "payments_webhook"
