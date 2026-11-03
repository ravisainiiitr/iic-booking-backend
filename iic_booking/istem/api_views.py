"""Portal endpoints for the signed-in user's own I-STEM link (email OTP linking of an existing I-STEM account)."""

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
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


def _test_key(request) -> str:
    """The I-STEM registration test key, from the ``istem_test`` query parameter or body field."""
    return str(request.query_params.get("istem_test") or request.data.get("istem_test") or "")


def _registration_refusal(exc) -> Response:
    return Response({"error": exc.message, "code": exc.code}, status=exc.http_status)


@api_view(["GET"])
@permission_classes([AllowAny])
def istem_register_availability(request):
    """Whether the registration form may offer "Already registered on I-STEM?" (I-STEM API and OTP linking on)."""
    from .registration_lookup import available

    return Response({"available": available(_test_key(request))})


@api_view(["POST"])
@permission_classes([AllowAny])
def istem_register_lookup(request):
    """Body: ``email`` or ``mobile`` registered on I-STEM (both must match when both are given). Emails an OTP to the
    email registered on I-STEM when exactly one account matches."""
    from .registration_lookup import RegistrationLookupError, start

    try:
        sent = start(str(request.data.get("email") or ""), str(request.data.get("mobile") or ""),
                     client_ip=_client_ip(request), test_key=_test_key(request))
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


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def istem_register_complete(request):
    """Body: ``link_token`` (from verify) and ``user_type``. Creates the approved portal account from the I-STEM profile
    at once (no password, no form), links it and signs the user in: returns ``token``, ``user`` and ``onboarding``
    (what the dashboard still has to ask)."""
    from django.conf import settings
    from django.core.mail import send_mail

    from iic_booking.communication.welcome_email import build_welcome_email, welcome_email_kwargs_from_user
    from iic_booking.users import onboarding, registration_approvals
    from iic_booking.users.api.auth_views import (
        _mark_login_and_check_first,
        _regenerate_auth_token,
        _registration_attempt_allowed,
        _should_send_welcome_email,
    )
    from iic_booking.users.api.token_auth import set_token_activity
    from iic_booking.users.serializers import UserSerializer

    from .direct_signup import DirectSignupError, create_account
    from .registration_lookup import available

    if not available(_test_key(request)):
        return Response({"error": "Registering with an I-STEM account is not available.", "code": "unavailable"},
                        status=status.HTTP_403_FORBIDDEN)
    if not _registration_attempt_allowed(request):
        return Response({"error": "Too many sign-up attempts from your network. Please wait an hour and try again.",
                         "code": "rate_limited"}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    try:
        user = create_account(str(request.data.get("link_token") or ""), str(request.data.get("user_type") or ""))
    except DirectSignupError as exc:
        return Response({"error": exc.message, "code": exc.code}, status=exc.http_status)
    registration_approvals.on_registration_verified(user, request=request)
    token = _regenerate_auth_token(user)
    set_token_activity(token.key)
    if _mark_login_and_check_first(user) and _should_send_welcome_email(user):
        try:
            content = build_welcome_email(**welcome_email_kwargs_from_user(user))
            send_mail(subject=content.subject, message=content.text_body, from_email=settings.DEFAULT_FROM_EMAIL,
                      recipient_list=[user.email], html_message=content.html_body, fail_silently=True)
        except Exception:  # noqa: BLE001 - the account exists; a welcome email is not worth failing for
            pass
    return Response({"token": token.key, "user": UserSerializer(user, context={"request": request}).data,
                     "onboarding": onboarding.status(user)}, status=status.HTTP_201_CREATED)


# --- I-STEM equipment mapping page (Main Admin: every instrument; department administrators: their own) --------------

def _mapping_refusal(exc) -> Response:
    return Response({"error": exc.message}, status=exc.http_status)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def istem_equipment_mappings(request):
    """The instruments the user manages, each with its I-STEM equipment mapping."""
    from .equipment_mapping import MappingRefused, list_rows

    try:
        return Response(list_rows(request.user))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def istem_equipment_search(request):
    """``?q=`` part of an I-STEM equipment name: active I-STEM equipment of the portal's institute (read-only)."""
    from .equipment_mapping import MappingRefused, search_istem_equipment

    try:
        source: dict = {}
        results = search_istem_equipment(request.user, request.query_params.get("q", ""), source=source)
        return Response({"results": results, "istem_source": source})
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["PUT"])
@permission_classes([IsAuthenticated])
def istem_equipment_mapping_save(request, equipment_id: int):
    """Body: ``istem_equipment_id``, ``sample_count_input_key``, ``is_active``, ``notes``."""
    from .equipment_mapping import MappingRefused, save_mapping

    try:
        return Response(save_mapping(request.user, equipment_id, request.data))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def istem_equipment_listing(request):
    """I-STEM's equipment listing for the institute, with the portal instruments mapped to each (``?refresh=1``)."""
    from .equipment_mapping import MappingRefused, istem_listing

    try:
        return Response(istem_listing(request.user, refresh=request.query_params.get("refresh") == "1"))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def istem_equipment_suggestions(request):
    """``?department_id=``: that department's unmapped instruments with likely I-STEM equipment by name."""
    from .equipment_mapping import MappingRefused, suggestions

    department_id = request.query_params.get("department_id") or None
    equipment_id = request.query_params.get("equipment_id") or None
    for value in (department_id, equipment_id):
        if value is not None and not str(value).isdigit():
            return Response({"error": "department_id and equipment_id are numbers."}, status=400)
    try:
        return Response(suggestions(request.user, department_id=department_id, equipment_id=equipment_id,
                                    refresh=request.query_params.get("refresh") == "1"))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def istem_department_mappings(request):
    """Main Admin. GET: I-STEM departments (from its equipment list) and their portal departments. PUT body:
    ``mappings``: [{istem_department_id, department_id}] (department_id null removes it)."""
    from .equipment_mapping import MappingRefused, istem_departments, save_department_mappings

    try:
        if request.method == "PUT":
            return Response(save_department_mappings(request.user, request.data.get("mappings")))
        return Response(istem_departments(request.user, refresh=request.query_params.get("refresh") == "1"))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def istem_equipment_mappings_bulk(request):
    """Body: ``mappings``: [{equipment_id, istem_equipment_id, sample_count_input_key}], each saved on its own."""
    from .equipment_mapping import MappingRefused, save_many

    try:
        return Response(save_many(request.user, request.data.get("mappings")))
    except MappingRefused as exc:
        return _mapping_refusal(exc)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def istem_equipment_mappings_verify(request):
    """Check every mapping the user manages against I-STEM's Equipment List."""
    from .equipment_mapping import MappingRefused, verify

    try:
        return Response(verify(request.user))
    except MappingRefused as exc:
        return _mapping_refusal(exc)
