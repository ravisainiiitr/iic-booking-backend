"""Portal endpoints for the signed-in user's own I-STEM link (email OTP linking of an existing I-STEM account)."""

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from .otp_link import OtpLinkError, find_existing_account, link_status, send_otp, verify_otp


def _refusal(exc: OtpLinkError) -> Response:
    return Response({"error": exc.message, "code": exc.code, **exc.extra}, status=exc.http_status)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def istem_link_status(request):
    """State of the user's I-STEM link; ``otp_required`` with the masked I-STEM email when an OTP is needed."""
    return Response(link_status(request.user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def istem_link_find_account(request):
    """Body: ``email`` or ``mobile`` registered with I-STEM. Finds the existing I-STEM account; then send-otp/verify-otp."""
    try:
        found = find_existing_account(request.user, email=str(request.data.get("email") or ""),
                                      mobile=str(request.data.get("mobile") or ""))
    except OtpLinkError as exc:
        return _refusal(exc)
    return Response({"message": f"I-STEM account found. Ask for an OTP to {found['email_masked']} to link it.", **found})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def istem_link_send_otp(request):
    """Send an OTP to the email registered on I-STEM for the user's existing I-STEM account."""
    try:
        sent = send_otp(request.user)
    except OtpLinkError as exc:
        return _refusal(exc)
    return Response({"message": f"OTP sent to {sent['email_masked']}.", **sent})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def istem_link_verify_otp(request):
    """Body: ``otp``. A correct code links the portal account to the existing I-STEM user."""
    try:
        link = verify_otp(request.user, str(request.data.get("otp") or ""))
    except OtpLinkError as exc:
        return _refusal(exc)
    return Response({"message": "Your I-STEM account is linked.", **link_status(request.user), "state": link.state},
                    status=status.HTTP_200_OK)


# --- registration with an existing I-STEM account (before the portal account exists) -------------------------------

def _client_ip(request) -> str:
    forwarded = (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")[0].strip()
    return forwarded or request.META.get("REMOTE_ADDR", "")


def _registration_refusal(exc) -> Response:
    return Response({"error": exc.message, "code": exc.code}, status=exc.http_status)


@api_view(["GET"])
@permission_classes([AllowAny])
def istem_register_availability(request):
    """Whether the registration form may offer "Already registered on I-STEM?" (I-STEM API and OTP linking on)."""
    from .registration_lookup import available

    return Response({"available": available()})


@api_view(["POST"])
@permission_classes([AllowAny])
def istem_register_lookup(request):
    """Body: ``email`` and ``mobile`` registered on I-STEM. Emails an OTP to that address when one account matches both."""
    from .registration_lookup import RegistrationLookupError, start

    try:
        sent = start(str(request.data.get("email") or ""), str(request.data.get("mobile") or ""),
                     client_ip=_client_ip(request))
    except RegistrationLookupError as exc:
        return _registration_refusal(exc)
    return Response({"message": f"OTP sent to {sent['email_masked']}.", **sent})


@api_view(["POST"])
@permission_classes([AllowAny])
def istem_register_verify(request):
    """Body: ``token`` and ``otp``. Returns the I-STEM profile for the form and ``link_token`` for the registration."""
    from .registration_lookup import RegistrationLookupError, verify

    try:
        result = verify(str(request.data.get("token") or ""), str(request.data.get("otp") or ""))
    except RegistrationLookupError as exc:
        return _registration_refusal(exc)
    return Response({"message": "I-STEM account verified. Check the details and complete your registration.", **result})
