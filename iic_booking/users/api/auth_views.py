import secrets
import base64
import socket
from contextlib import contextmanager
from io import BytesIO
from urllib.parse import urlencode, quote
from datetime import datetime

import requests
import urllib3.util.connection as urllib3_connection
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.mail import send_mail
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.utils import OperationalError, ProgrammingError
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.welcome_email import build_welcome_email, welcome_email_kwargs_from_user
from django.http import HttpResponseRedirect, HttpResponse
from django.urls import reverse
from django.utils.http import urlsafe_base64_encode, urlsafe_base64_decode
from django.utils.encoding import force_bytes, force_str
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.authtoken.models import Token

from django.contrib.auth import authenticate
from .auth_serializers import (
    OmniportCallbackSerializer,
    OmniportAuthResponseSerializer,
    LoginSerializer,
    LoginResponseSerializer,
    RegisterSerializer,
    RegisterResponseSerializer,
    UserTypeSerializer,
    ResendVerificationEmailSerializer,
)
from .token_auth import set_token_activity
from iic_booking.users.serializers import UserSerializer
from iic_booking.users.models import UserType, Department, UserLoginLock, OrganizationRequest
import logging
logger = logging.getLogger(__name__)
User = get_user_model()


@contextmanager
def _omniport_network_context():
    """
    Prefer IPv4 for Channel i API calls.

    Some production hosts resolve channeli.in to IPv6 first and hang until timeout,
    which breaks the OAuth code exchange after the user clicks Allow.
    """
    allowed = urllib3_connection.allowed_gai_family
    urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    try:
        yield
    finally:
        urllib3_connection.allowed_gai_family = allowed


def _omniport_timeout():
    connect = int(getattr(settings, "OMNIPORT_CONNECT_TIMEOUT", 10))
    read = int(getattr(settings, "OMNIPORT_READ_TIMEOUT", 30))
    return (connect, read)


def _omniport_post(url: str, **kwargs):
    kwargs.setdefault("timeout", _omniport_timeout())
    with _omniport_network_context():
        return requests.post(url, **kwargs)


def _omniport_get(url: str, **kwargs):
    kwargs.setdefault("timeout", _omniport_timeout())
    with _omniport_network_context():
        return requests.get(url, **kwargs)


def _should_send_welcome_email(user) -> bool:
    """
    Send the first-login welcome email to any portal user with a deliverable email.
    """
    return bool((getattr(user, "email", None) or "").strip())


def _mark_login_and_check_first(user) -> bool:
    """
    Atomically mark login time and return True only once per user (first login).

    This avoids duplicate "first login" emails in concurrent login requests.
    """
    now_ts = timezone.now()
    first_login = (
        User.objects.filter(pk=user.pk, last_login__isnull=True).update(last_login=now_ts) == 1
    )
    if not first_login:
        User.objects.filter(pk=user.pk).update(last_login=now_ts)
    user.last_login = now_ts
    return first_login


def _regenerate_auth_token(user):
    """
    Ensure single session only: delete all existing tokens for this user and create one new token.
    Call on every login (email/password, OTP, Omniport). Applies everywhere — same PC, multiple
    browsers, or different devices: only the most recent login remains valid; all others get 401.
    Uses a dedicated DB table (UserLoginLock) with select_for_update() so concurrent logins for
    the same user are serialized across ALL workers. If the table does not exist yet (migration
    not run), falls back to locking the User row so login still works.
    """
    try:
        with transaction.atomic():
            UserLoginLock.objects.select_for_update().get_or_create(user=user, defaults={})
            Token.objects.filter(user=user).delete()
            return Token.objects.create(user=user)
    except (OperationalError, ProgrammingError) as e:
        # Table users_userloginlock may not exist yet (migration not run); fall back to User row lock
        err = str(e).lower()
        if "users_userloginlock" in err or "does not exist" in err or "no such table" in err:
            with transaction.atomic():
                User.objects.select_for_update().get(pk=user.pk)
                Token.objects.filter(user=user).delete()
                return Token.objects.create(user=user)
        raise

def _user_has_active_session(user):
    """
    Return True if the user already has an auth token (i.e. is logged in elsewhere).
    No longer used: we allow re-login and regenerate token (last login wins).
    """
    return Token.objects.filter(user=user).exists()

# Public/free email domains: for Educational Institute and Govt R&D, documents are required when email uses these.
PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com",
    "outlook.com", "hotmail.com", "hotmail.co.in", "live.com", "live.in", "msn.com",
    "rediffmail.com", "rediff.com",
    "icloud.com", "me.com", "mac.com",
    "mail.com", "protonmail.com", "pm.me", "aol.com", "zoho.com",
    "gmx.com", "gmx.net", "inbox.com", "mailinator.com",
})

# External users must not use IIT Roorkee email domain (that is for internal users).
IITR_EMAIL_DOMAIN = "iitr.ac.in"


def omniport_callback_url(request):
    """
    Generate the Omniport callback URL dynamically based on the current request.
    This ensures the redirect_uri matches the actual callback endpoint URL.
    
    Prefers OMNIPORT_REDIRECT_URI from settings if it's a full URL.
    Otherwise, builds from request, but checks for proper host to avoid 127.0.0.1.
    """
    # First, prefer the explicitly configured redirect URI from settings (if it's a full URL)
    if settings.OMNIPORT_REDIRECT_URI and settings.OMNIPORT_REDIRECT_URI.startswith(('http://', 'https://')):
        return settings.OMNIPORT_REDIRECT_URI
    
    # Try to build from request
    try:
        callback_url = request.build_absolute_uri(reverse("api:omniport-callback"))
        # Check if the URL contains 127.0.0.1 (which shouldn't be used in production)
        if '127.0.0.1' in callback_url and not settings.DEBUG:
            # In production, use ALLOWED_HOSTS to build proper URL
            if hasattr(settings, 'ALLOWED_HOSTS') and settings.ALLOWED_HOSTS:
                # Get the first non-wildcard host
                allowed_host = next((h for h in settings.ALLOWED_HOSTS if h != '*'), settings.ALLOWED_HOSTS[0])
                # Remove port if present
                allowed_host = allowed_host.split(':')[0]
                scheme = 'https' if not settings.DEBUG else 'http'
                return f"{scheme}://{allowed_host}/api/auth/omniport/callback/"
            # Fallback to settings value
            if settings.OMNIPORT_REDIRECT_URI:
                return settings.OMNIPORT_REDIRECT_URI
        return callback_url
    except Exception as e:
        logger.warning(f"Failed to build absolute URI from request: {e}")
    
    # Fallback: use settings value if available
    if settings.OMNIPORT_REDIRECT_URI:
        return settings.OMNIPORT_REDIRECT_URI
    
    # Last resort: build from ALLOWED_HOSTS
    if hasattr(settings, 'ALLOWED_HOSTS') and settings.ALLOWED_HOSTS:
        allowed_host = next((h for h in settings.ALLOWED_HOSTS if h != '*'), settings.ALLOWED_HOSTS[0])
        allowed_host = allowed_host.split(':')[0]
        scheme = 'https' if not settings.DEBUG else 'http'
        return f"{scheme}://{allowed_host}/api/auth/omniport/callback/"
    
    # Ultimate fallback: use request (for local development)
    return request.build_absolute_uri(reverse("api:omniport-callback"))


def redirect_to_frontend_with_error(request, error_message, error_code=None):
    """
    Redirect to frontend URL with error information in query parameters.
    Used for GET requests (OAuth redirects) to show errors on the frontend.
    """
    frontend_url = settings.FRONTEND_URL.rstrip('/')
    error_params = {
        "error": error_message,
    }
    if error_code:
        error_params["error_code"] = error_code
    
    redirect_url = f"{frontend_url}/auth/callback?{urlencode(error_params)}"
    logger.warning(f"Redirecting to frontend with error: {error_message}")
    return HttpResponseRedirect(redirect_url)


def determine_user_type_from_omniport(user_info: dict) -> str:
    """
    Determine user type from Omniport user info.

    Channel i returns non-empty ``student`` / ``facultyMember`` objects for those
    roles. Other institute accounts (staff, officers, maintainers, etc.) often
    have both empty and only ``roles`` populated.
    """
    faculty_member = user_info.get("facultyMember", {})
    if faculty_member and isinstance(faculty_member, dict) and len(faculty_member) > 0:
        return UserType.FACULTY

    student = user_info.get("student", {})
    if student and isinstance(student, dict) and len(student) > 0:
        return UserType.STUDENT

    roles = user_info.get("roles", [])
    faculty_indicators = ("faculty", "professor", "instructor")
    # Non-teaching institute accounts that authenticate via Channel i
    staff_indicators = ("staff", "employee", "officer", "maintainer", "admin", "operator")

    role_names: list[str] = []
    if isinstance(roles, list):
        for role in roles:
            if isinstance(role, dict):
                role_names.append(
                    str(role.get("name") or role.get("role") or role.get("codename") or role).lower()
                )
            else:
                role_names.append(str(role).lower())

    for role_name in role_names:
        if any(ind in role_name for ind in faculty_indicators):
            return UserType.FACULTY
    for role_name in role_names:
        if any(ind in role_name for ind in staff_indicators):
            # Staff/officers book as internal faculty-class users until an admin
            # reassigns them (manager/operator/etc.).
            return UserType.FACULTY

    # Last resort for Channel i accounts with no student/faculty payload
    logger.warning(
        "Omniport user has empty student/facultyMember; roles=%s — defaulting to faculty",
        role_names,
    )
    return UserType.FACULTY


def _blank_to_none(value):
    """Normalize empty strings to None so unique nullable fields don't collide."""
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _omniport_dict_get(data: dict, *keys):
    """Return the first non-empty value for any of the candidate keys (Channel i key variants)."""
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _parse_omniport_date(value):
    """Parse Omniport date values (ISO string, date, or datetime) into a date or None."""
    if value is None:
        return None
    if hasattr(value, "year") and hasattr(value, "month") and hasattr(value, "day") and not isinstance(value, str):
        # datetime.date or datetime.datetime
        try:
            return value.date() if hasattr(value, "hour") else value
        except Exception:
            return None
    text = str(value).strip()
    if not text or text.lower() in {"none", "null"}:
        return None
    # ISO datetime → take date part
    if "T" in text:
        text = text.split("T", 1)[0]
    for date_format in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    logger.warning("Failed to parse Omniport date value '%s'", value)
    return None


def _role_start_end_dates(role: dict) -> tuple:
    """Extract joining (start) and end/graduation dates from a student/facultyMember payload."""
    start_raw = _omniport_dict_get(
        role,
        "start_date",
        "startDate",
        "start date",
        "joining_date",
        "joiningDate",
        "joining date",
    )
    end_raw = _omniport_dict_get(
        role,
        "end_date",
        "endDate",
        "end date",
        "graduation_date",
        "graduationDate",
        "graduation date",
    )
    return _parse_omniport_date(start_raw), _parse_omniport_date(end_raw)


def _get_or_create_user_from_omniport(*, email: str, defaults: dict):
    """
    Create or load a Django user from Omniport profile data.

    Handles common live failures for non-student/non-faculty Channel i accounts:
    - empty emp_id stored as '' colliding with UNIQUE(emp_id)
    - emp_id already belonging to a different email
    - ImageField in get_or_create defaults causing storage save errors
    """
    from django.db import IntegrityError

    defaults = dict(defaults)
    profile_picture_file = defaults.pop("profile_picture", None)

    for key in ("emp_id", "phone_number", "secondary_phone_number", "internal_id", "name"):
        if key in defaults:
            defaults[key] = _blank_to_none(defaults.get(key))

    # Prefer existing account by email (case-insensitive)
    existing = User.objects.filter(email__iexact=email).first()
    if existing:
        return existing, False

    emp_id = defaults.get("emp_id")
    if emp_id and User.objects.filter(emp_id=emp_id).exclude(email__iexact=email).exists():
        logger.warning(
            "Omniport emp_id=%s already used by another user; creating %s without emp_id",
            emp_id,
            email,
        )
        defaults["emp_id"] = None

    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email, password=None, **defaults)
    except IntegrityError as exc:
        # Race: another request created the same email, or emp_id unique race
        existing = User.objects.filter(email__iexact=email).first()
        if existing:
            return existing, False
        if defaults.get("emp_id"):
            logger.warning(
                "IntegrityError creating Omniport user %s (%s); retrying without emp_id",
                email,
                exc,
            )
            defaults["emp_id"] = None
            with transaction.atomic():
                user = User.objects.create_user(email=email, password=None, **defaults)
        else:
            raise

    if profile_picture_file:
        # Only reached for newly created users (existing accounts return above).
        try:
            user.profile_picture = profile_picture_file
            user.save(update_fields=["profile_picture"])
        except Exception as pic_exc:
            logger.warning(
                "Omniport user %s created but profile picture save failed: %s",
                email,
                pic_exc,
            )

    return user, True


@api_view(["GET"])
@permission_classes([AllowAny])
def omniport_auth_url(request):
    """
    Generate Omniport OAuth authorization URL for React frontend.
    
    Returns:
        - auth_url: The URL to redirect user to for Omniport login
        - state: Random state token for CSRF protection
    """
    state = secrets.token_urlsafe(32)
    redirect_uri = omniport_callback_url(request)
    
    cache.set(f"omniport_state_{state}", redirect_uri, timeout=600)
    
    params = {
        "client_id": settings.OMNIPORT_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": "read",
        "state": state,
    }
    return Response(
        {
            "auth_url": settings.OMNIPORT_AUTH_URL + "?" + urlencode(params),
            "state": state,
        }
    )


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def omniport_callback(request):
    """
    Handle Omniport OAuth callback and exchange code for token.
    
    Supports both GET (direct OAuth redirect) and POST (from React frontend).
    
    This endpoint:
    1. Validates the authorization code
    2. Exchanges code for access token with Omniport
    3. Fetches user info from Omniport
    4. Creates or updates user in Django
    5. Returns Django auth token for React frontend
    """
    # Support both GET (query params) and POST (body)
    data = request.query_params if request.method == "GET" else request.data
    
    # Log incoming request for debugging
    logger.info(f"Omniport callback received: method={request.method}, params={dict(data)}")
    
    # Check for OAuth error parameters first (OAuth 2.0 spec allows error in callback)
    if "error" in data:
        error_message = data.get("error_description", data.get("error", "OAuth authentication failed"))
        error_code = data.get("error", "oauth_error")
        logger.warning(f"OAuth callback received error: {error_message} (code: {error_code})")
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, error_message, error_code)
        return Response(
            {"error": error_message, "error_code": error_code},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate serializer (requires code parameter)
    serializer = OmniportCallbackSerializer(data=data)
    if not serializer.is_valid():
        error_message = "Invalid request parameters"
        if serializer.errors:
            # Format error message more clearly
            error_parts = []
            for k, v in serializer.errors.items():
                if isinstance(v, list):
                    error_parts.append(f"{k}: {v[0]}")
                else:
                    error_parts.append(f"{k}: {str(v)}")
            error_message = "; ".join(error_parts)
        
        logger.warning(f"Omniport callback validation failed: {error_message}. Received data: {dict(data)}")
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, error_message, "invalid_request")
        return Response(
            {"error": error_message, "details": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    code = serializer.validated_data["code"]
    state = serializer.validated_data.get("state")
    
    # Validate state token and get stored redirect_uri
    # Generate callback URL dynamically to match the actual endpoint
    redirect_uri = omniport_callback_url(request)
    if state:
        cached_redirect_uri = cache.get(f"omniport_state_{state}")
        if cached_redirect_uri:
            # Use the redirect_uri that was used in the authorization request
            redirect_uri = cached_redirect_uri
            # Delete state after use
            cache.delete(f"omniport_state_{state}")
        else:
            # State token not found or expired, but continue with dynamically generated URL
            logger.warning(f"State token {state} not found in cache, using dynamically generated redirect_uri")
    
    token_data_basic = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri
    }
    
    client_credentials = f"{settings.OMNIPORT_CLIENT_ID}:{settings.OMNIPORT_CLIENT_SECRET}"
    encoded_credentials = base64.b64encode(client_credentials.encode()).decode()
    
    token_url = settings.OMNIPORT_TOKEN_URL
    token_response = None
    
    token_response = None
    token_error_code = "token_endpoint_error"
    token_error_message = "Failed to connect to Omniport token service"
    try:
        token_response = _omniport_post(
            token_url,
            data=token_data_basic,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Authorization": f"Basic {encoded_credentials}",
            },
        )
    except requests.ConnectTimeout as e:
        token_error_code = "token_endpoint_timeout"
        token_error_message = (
            "Connection to Omniport timed out. The server may be unable to reach channeli.in."
        )
        logger.error("Omniport token exchange connect timeout at %s: %s", token_url, e)
    except requests.ReadTimeout as e:
        token_error_code = "token_endpoint_timeout"
        token_error_message = "Omniport token service did not respond in time."
        logger.error("Omniport token exchange read timeout at %s: %s", token_url, e)
    except requests.ConnectionError as e:
        token_error_code = "token_endpoint_unreachable"
        token_error_message = (
            "Cannot reach Omniport from the server. Check outbound HTTPS access to channeli.in."
        )
        logger.error("Omniport token exchange connection error at %s: %s", token_url, e)
    except requests.RequestException as e:
        logger.error("Omniport token exchange request failed at %s: %s", token_url, e)

    if not token_response:
        if request.method == "GET":
            return redirect_to_frontend_with_error(
                request, token_error_message, token_error_code
            )
        return Response(
            {"error": token_error_message, "error_code": token_error_code},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        token_response.raise_for_status()
        token_info = token_response.json()
        logger.info(f"Token info received: {token_info}")
        access_token = token_info.get("access_token")
        if not access_token:
            error_message = "Failed to obtain access token from Omniport"
            if request.method == "GET":
                return redirect_to_frontend_with_error(request, error_message, "no_access_token")
            return Response(
                {"error": error_message},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except requests.HTTPError as e:
        error_message = f"Token exchange failed: {token_response.text if token_response else str(e)}"
        logger.error(error_message)
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Failed to exchange authorization code for token", "token_exchange_failed")
        return Response(
            {"error": error_message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except ValueError as e:
        error_message = f"Invalid token response: {str(e)}"
        logger.error(error_message)
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Invalid response from authentication server", "invalid_token_response")
        return Response(
            {"error": error_message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        user_response = _omniport_get(
            settings.OMNIPORT_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        user_response.raise_for_status()
        user_info = user_response.json()
    except requests.RequestException as e:
        error_message = f"Failed to fetch user info from Omniport: {str(e)}"
        logger.error(error_message)
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Failed to fetch user information", "userinfo_fetch_failed")
        return Response(
            {"error": error_message},
            status=status.HTTP_400_BAD_REQUEST,
        )

    logger.info(f"User info received: {user_info}")
    
    # Initialize all variables with default values
    user_type = None  # Will be a user type code string
    internal_id = _blank_to_none(user_info.get("userId", ""))
    emp_id = _blank_to_none(user_info.get("username", ""))
    person = user_info.get("person", {})
    name = person.get("fullName", "") if isinstance(person, dict) else ""
    profile_picture_path = person.get("displayPicture", "") if isinstance(person, dict) else ""
    profile_picture = None  # Initialize to None
    branch_name = ""  # Initialize to empty string
    degree_name = ""  # Initialize to empty string
    department_name = ""  # Initialize to empty string
    designation = ""  # Initialize to empty string
    joining_date = None
    graduation_date = None

    student = user_info.get("student") or user_info.get("student_member") or {}
    if student and isinstance(student, dict) and len(student) > 0:
        # Set student user type code
        user_type = UserType.STUDENT
        branch_name = student.get("branch name", "") or student.get("branch_name", "") or ""
        degree_name = student.get("branch degree name", "") or student.get("branch_degree_name", "") or ""
        department_name = (
            student.get("branch department name", "")
            or student.get("branch_department_name", "")
            or ""
        )
        joining_date, graduation_date = _role_start_end_dates(student)

    faculty = user_info.get("facultyMember") or user_info.get("faculty_member") or {}
    if faculty and isinstance(faculty, dict) and len(faculty) > 0:
        # Set faculty user type code
        user_type = UserType.FACULTY
        department_name = (
            faculty.get("department name", "")
            or faculty.get("department_name", "")
            or ""
        )
        designation = (faculty.get("designation", "") or "").strip()
        fac_joining, _fac_end = _role_start_end_dates(faculty)
        if fac_joining:
            joining_date = fac_joining

    biological_info = user_info.get("biologicalInformation") or user_info.get("biological_information") or {}
    date_of_birth_str = (
        biological_info.get("dateOfBirth")
        or biological_info.get("date_of_birth")
        or ""
    ) if isinstance(biological_info, dict) else ""
    
    contact_info = user_info.get("contactInformation") or user_info.get("contact_information") or {}
    email = None
    phone_number = None
    secondary_phone_number = None
    if isinstance(contact_info, dict):
        email = (
            contact_info.get("instituteWebmailAddress")
            or contact_info.get("institute_webmail_address")
        )
        phone_number = _blank_to_none(
            contact_info.get("primaryPhoneNumber") or contact_info.get("primary_phone_number")
        )
        secondary_phone_number = _blank_to_none(
            contact_info.get("secondaryPhoneNumber") or contact_info.get("secondary_phone_number")
        )
    
    # Validate that email is present (required for user creation)
    if not email:
        error_message = "Email address not found in user information"
        logger.error(error_message)
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Email address is required for authentication", "missing_email")
        return Response(
            {"error": error_message},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Parse date_of_birth if provided
    date_of_birth = _parse_omniport_date(date_of_birth_str) if date_of_birth_str else None
    
    # Get or create department if department_name is provided
    department = None
    if department_name:
        try:
            department, _ = Department.objects.get_or_create(
                name=department_name,
                defaults={"description": f"Department created from Omniport for {department_name}"}
            )
            logger.info(f"Department '{department_name}' retrieved or created: {department.id}")
        except Exception as e:
            logger.error(f"Failed to get or create department '{department_name}': {str(e)}")
            # Continue without department if creation fails
    
    # Download Channel i profile picture only when this account does not already have one.
    # Policy: capture once (first login / empty picture). Never overwrite a picture the user
    # already has (including one they uploaded later in IIC Booking).
    profile_picture_file = None
    existing_for_picture = User.objects.filter(email__iexact=email).first() if email else None
    already_has_profile_picture = bool(
        existing_for_picture
        and existing_for_picture.profile_picture
        and getattr(existing_for_picture.profile_picture, "name", None)
    )
    if profile_picture_path and not already_has_profile_picture:
        actual_path = "https://channeli.in" + profile_picture_path
        try:
            response = _omniport_get(actual_path)
            if response.status_code == 200:
                # Get file extension from path
                file_extension = profile_picture_path.split(".")[-1] if "." in profile_picture_path else "jpg"
                if "/" in file_extension or not file_extension:
                    file_extension = "jpg"  # Fallback to jpg if extension parsing fails
                
                # Generate unique filename for S3 storage
                filename = f"profile_pictures/user_{email.replace('@', '_at_')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{file_extension}"
                
                # Upload to S3 first to ensure it's available before saving the user
                try:
                    file_content = ContentFile(response.content)
                    s3_path = default_storage.save(filename, file_content)
                    logger.info(f"Profile picture uploaded to S3 before user save: {s3_path}")
                    
                    # Create a ContentFile with the original content and S3 path as the name
                    # The ImageField will store the relative path (without 'media/' prefix) in the database
                    # django-storages will handle generating the S3 URL when needed
                    # Extract relative path (remove 'media/' prefix if present)
                    relative_path = s3_path.replace("media/", "") if s3_path.startswith("media/") else s3_path
                    profile_picture_file = ContentFile(response.content, name=relative_path)
                except Exception as e:
                    logger.error(f"Failed to upload profile picture to S3: {str(e)}")
                    profile_picture_file = None
        except requests.RequestException as e:
            logger.error(f"Failed to download profile picture from {actual_path}: {str(e)}")
            profile_picture_file = None
    elif already_has_profile_picture:
        logger.info(
            "Omniport login: skipping Channel i profile picture for %s (already set)",
            email,
        )
    
    # If user_type is still None, use determine_user_type_from_omniport
    if not user_type:
        user_type = determine_user_type_from_omniport(user_info)

    # Prepare defaults for user creation (no email key — passed separately)
    defaults = {
        "internal_id": internal_id,
        "name": name or "",
        "phone_number": phone_number,
        "secondary_phone_number": secondary_phone_number,
        "emp_id": emp_id,
        "date_of_birth": date_of_birth,
        "branch_name": branch_name or "",
        "degree_name": degree_name or "",
        "designation": designation or "",
        "joining_date": joining_date,
        "graduation_date": graduation_date,
        "user_type": user_type,
        "department": department,
        "is_active": True,
        "email_verified": True,
        "admin_approved": True,
    }

    if profile_picture_file:
        defaults["profile_picture"] = profile_picture_file

    try:
        user, created = _get_or_create_user_from_omniport(email=email, defaults=defaults)
        if not created:
            # Existing account: never overwrite user_type after it was set on first login
            # (admin/OIC may have changed it; Channel i must not reset it).
            update_fields = []
            if not user.user_type and user_type:
                user.user_type = user_type
                update_fields.append("user_type")
            if department and user.department != department:
                user.department = department
                update_fields.append("department")
            if emp_id and not user.emp_id:
                if not User.objects.filter(emp_id=emp_id).exclude(pk=user.pk).exists():
                    user.emp_id = emp_id
                    update_fields.append("emp_id")
            # Profile picture: Channel i seeds only when the account still has none.
            # Never overwrite a picture already stored (user change or prior Channel i capture).
            if profile_picture_file and not (
                user.profile_picture and getattr(user.profile_picture, "name", None)
            ):
                user.profile_picture = profile_picture_file
                update_fields.append("profile_picture")
            if name and not user.name:
                user.name = name
                update_fields.append("name")
            # Refresh Omniport academic / employment fields when Channel i sends them
            if designation and user.designation != designation:
                user.designation = designation
                update_fields.append("designation")
            if branch_name and user.branch_name != branch_name:
                user.branch_name = branch_name
                update_fields.append("branch_name")
            if degree_name and user.degree_name != degree_name:
                user.degree_name = degree_name
                update_fields.append("degree_name")
            if joining_date and user.joining_date != joining_date:
                user.joining_date = joining_date
                update_fields.append("joining_date")
            if graduation_date and user.graduation_date != graduation_date:
                user.graduation_date = graduation_date
                update_fields.append("graduation_date")
            if date_of_birth and user.date_of_birth != date_of_birth:
                user.date_of_birth = date_of_birth
                update_fields.append("date_of_birth")
            if update_fields:
                user.save(update_fields=update_fields)
            logger.info(
                "Omniport login existing user email=%s type=%s joining=%s graduation=%s designation=%s roles=%s",
                email,
                user.user_type,
                user.joining_date,
                user.graduation_date,
                user.designation,
                user_info.get("roles"),
            )
        else:
            logger.info(
                "Omniport created user email=%s type=%s joining=%s graduation=%s designation=%s roles=%s",
                email,
                user.user_type,
                joining_date,
                graduation_date,
                designation,
                user_info.get("roles"),
            )
    except Exception as e:
        error_message = f"Failed to create or retrieve user: {str(e)}"
        logger.exception(
            "Omniport user_creation_failed email=%s emp_id=%s roles=%s has_student=%s has_faculty=%s",
            email,
            emp_id,
            user_info.get("roles"),
            bool(student),
            bool(faculty),
        )
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Failed to create user account", "user_creation_failed")
        return Response(
            {"error": error_message},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    
    # Get or create Django auth token (regenerate so this session gets the token; any other session will get 401)
    try:
        token = _regenerate_auth_token(user)
        set_token_activity(token.key)
    except RuntimeError as e:
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Another login is in progress. Please try again shortly.", "login_conflict")
        return Response(
            {"error": "Login in progress", "message": str(e)},
            status=status.HTTP_409_CONFLICT,
        )
    except Exception as e:
        error_message = f"Failed to create authentication token: {str(e)}"
        logger.error(error_message)
        if request.method == "GET":
            return redirect_to_frontend_with_error(request, "Failed to create authentication token", "token_creation_failed")
        return Response(
            {"error": error_message},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    is_first_login = _mark_login_and_check_first(user)

    # First-login welcome email (styled)
    if _should_send_welcome_email(user) and is_first_login:
        try:
            content = build_welcome_email(**welcome_email_kwargs_from_user(user))
            send_mail(
                subject=content.subject,
                message=content.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=content.html_body,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send first-login welcome email to %s", user.email)
    
    # Prepare response data
    response_data = {
        "token": token.key,
        "user": {
            "id": user.id,
            "email": user.email,
            "name": user.name,
            "user_type": user.user_type if user.user_type else None,
            "uses_admin_panel": user.uses_admin_panel(),
        },
    }
    
    # If GET request (direct OAuth redirect), redirect to frontend
    if request.method == "GET":
        frontend_url = settings.FRONTEND_URL.rstrip('/')
        
        # Build redirect URL with token and user info as query parameters
        redirect_params = {
            "token": token.key,
            "user_id": str(user.id),
            "email": user.email,
            "name": user.name or "",
            "user_type": user.user_type if user.user_type else "",
            "uses_admin_panel": "true" if user.uses_admin_panel() else "false",
        }
        
        # Encode parameters for URL
        from urllib.parse import urlencode
        redirect_url = f"{frontend_url}/auth/callback?{urlencode(redirect_params)}"
        
        logger.info(f"Redirecting user {user.email} to frontend: {redirect_url}")
        return HttpResponseRedirect(redirect_url)
    
    # If POST request (from React frontend), return JSON response
    response_serializer = OmniportAuthResponseSerializer(response_data)
    return Response(response_serializer.data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def obtain_auth_token_single_session(request):
    """
    DRF-compatible token endpoint that enforces single session.
    POST body: username (user's email), password.
    Returns: {"token": "<key>"}. Invalidates any previous token for this user.
    """
    from rest_framework.authtoken.serializers import AuthTokenSerializer
    serializer = AuthTokenSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    user = serializer.validated_data["user"]
    if not user.is_active:
        return Response(
            {"non_field_errors": ["Unable to log in with provided credentials."]},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        token = _regenerate_auth_token(user)
    except RuntimeError as e:
        return Response(
            {"error": "Login in progress", "message": str(e)},
            status=status.HTTP_409_CONFLICT,
        )
    set_token_activity(token.key)
    return Response({"token": token.key})


@api_view(["POST"])
@permission_classes([AllowAny])
def login(request):
    """
    Login endpoint for email/password authentication.
    
    Primarily used by External users, but can be used by any user type
    that has email/password authentication enabled.
    
    Request Body:
        - email: User's email address
        - password: User's password
    
    Returns:
        - token: Django REST Framework auth token
        - user: User information including id, email, name, user_type
    """
    serializer = LoginSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    
    email = serializer.validated_data["email"]
    password = serializer.validated_data["password"]
    
    # Check if user exists first to provide better error messages
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return Response(
            {"error": "Invalid email or password"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    
    # Check if email is verified
    if not user.email_verified:
        return Response(
            {
                "error": "Email not verified",
                "message": "Please verify your email address before logging in. Check your email for the verification link or request a new one.",
                "email_verified": False,
                "admin_approved": False,
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Check if admin has approved
    if not user.admin_approved:
        return Response(
            {
                "error": "Account pending approval",
                "message": "Your email has been verified, but your account is pending admin approval. You will be notified once approved.",
                "email_verified": True,
                "admin_approved": False,
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    # For Post Doc / Research Associates: supervisor must approve first
    if getattr(user, "needs_supervisor_approval", lambda: False)() and not getattr(user, "supervisor_approved", False):
        return Response(
            {
                "error": "Pending supervisor approval",
                "message": "Your account is pending approval by your supervisor. You will be notified once approved.",
                "email_verified": True,
                "admin_approved": user.admin_approved,
                "supervisor_approved": False,
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    # Program end date / access-on-hold controls are for internal program-linked users.
    # External users are governed by admin approval + active/force_inactive only.
    from django.utils import timezone
    if UserType.is_internal_user(getattr(user, "user_type", "")):
        if getattr(user, "program_end_date", None) and timezone.localdate() > user.program_end_date:
            return Response(
                {
                    "error": "Access expired",
                    "message": "Your program/course/position end date has passed. Access is disabled. Contact support if you need an extension.",
                    "email_verified": True,
                    "admin_approved": True,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
        if getattr(user, "access_on_hold", False):
            return Response(
                {
                    "error": "Access on hold",
                    "message": "Your access is on hold. Please submit documentary evidence to revalidate (e.g. extension letter). Contact support.",
                    "email_verified": True,
                    "admin_approved": True,
                    "access_on_hold": True,
                },
                status=status.HTTP_403_FORBIDDEN,
            )
    
    # Check if account is active
    if not user.is_active:
        return Response(
            {
                "error": "Account disabled",
                "message": "Your account has been disabled. Please contact support.",
                "email_verified": user.email_verified,
                "admin_approved": user.admin_approved,
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    if not user.is_department_access_enabled():
        return Response(
            {
                "error": "Department access disabled",
                "message": "Your department access has been disabled by the Main Administrator.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Authenticate user (check password)
    authenticated_user = authenticate(request=request, username=email, password=password)
    
    if not authenticated_user:
        return Response(
            {"error": "Invalid email or password"},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    
    # Ensure authenticated user matches the user we found
    user = authenticated_user

    # Create token for this session (regenerate so this session gets the token; any other session will get 401)
    try:
        token = _regenerate_auth_token(user)
    except RuntimeError as e:
        return Response(
            {"error": "Login in progress", "message": str(e)},
            status=status.HTTP_409_CONFLICT,
        )
    set_token_activity(token.key)
    is_first_login = _mark_login_and_check_first(user)

    # First-login welcome email (styled)
    if _should_send_welcome_email(user) and is_first_login:
        try:
            content = build_welcome_email(**welcome_email_kwargs_from_user(user))
            send_mail(
                subject=content.subject,
                message=content.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=content.html_body,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send first-login welcome email to %s", user.email)

    # Serialize full user data using UserSerializer
    from ..serializers import UserSerializer
    user_serializer = UserSerializer(user, context={"request": request})
    
    # Prepare response with full user data
    response_serializer = LoginResponseSerializer(
        {
            "token": token.key,
            "user": user_serializer.data,
        }
    )
    
    return Response(response_serializer.data, status=status.HTTP_200_OK)


def _normalize_phone_india(raw):
    """Normalize Indian phone to 10 digits for lookup (strip +91, leading 0, spaces)."""
    import re
    s = (raw or "").strip().replace(" ", "")
    s = re.sub(r"^\+91", "", s)
    s = re.sub(r"^0+", "", s)
    return s.strip() if s.isdigit() and len(s) == 10 else None


LOGIN_OTP_CACHE_PREFIX = "login_otp:"
FORGOT_OTP_CACHE_PREFIX = "forgot_otp:"
OTP_EXPIRY_SECONDS = 600  # 10 minutes


@api_view(["POST"])
@permission_classes([AllowAny])
def request_login_otp(request):
    """
    Request OTP for login via email. OTP is sent to the user's email.
    Only active, email-verified, admin-approved users can request login OTP.
    """
    email_raw = (request.data.get("email") or "").strip().lower()
    if not email_raw:
        return Response(
            {"error": "Email is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user = User.objects.get(email=email_raw)
    except User.DoesNotExist:
        return Response(
            {"error": "No active account found with this email. Please sign up or use password login."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not user.can_login():
        from django.utils import timezone
        if UserType.is_internal_user(getattr(user, "user_type", "")):
            if getattr(user, "program_end_date", None) and timezone.localdate() > user.program_end_date:
                return Response(
                    {"error": "Access expired. Your program/course end date has passed. Contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if getattr(user, "access_on_hold", False):
                return Response(
                    {"error": "Access on hold. Submit documentary evidence to revalidate. Contact support."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if getattr(user, "needs_supervisor_approval", lambda: False)() and not getattr(user, "supervisor_approved", False):
                return Response(
                    {"error": "Account pending supervisor approval."},
                    status=status.HTTP_403_FORBIDDEN,
                )
        return Response(
            {"error": "No active account found with this email. Please sign up or use password login."},
            status=status.HTTP_404_NOT_FOUND,
        )
    otp = "".join([str(secrets.randbelow(10)) for _ in range(6)])
    cache_key = f"{LOGIN_OTP_CACHE_PREFIX}{email_raw}"
    cache.set(cache_key, {"otp": otp, "user_id": user.id}, timeout=OTP_EXPIRY_SECONDS)
    try:
        subject = "Your IIC Booking login OTP"
        body_plain = f"Your one-time password (OTP) for login is: {otp}\n\nThis OTP expires in 10 minutes. Do not share it with anyone."
        html_body = f"""
        <p>Your one-time password (OTP) for login is:</p>
        <p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{otp}</p>
        <p>This OTP expires in 10 minutes. Do not share it with anyone.</p>
        """
        send_mail(
            subject=subject,
            message=body_plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("Failed to send login OTP email: %s", e)
        return Response(
            {"error": "Failed to send OTP. Please try again later."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {"message": "OTP has been sent to your email address. It expires in 10 minutes."},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_login_otp(request):
    """
    Verify login OTP and return auth token. Body: email, otp.
    """
    email_raw = (request.data.get("email") or "").strip().lower()
    otp = (request.data.get("otp") or "").strip().replace(" ", "")
    if not email_raw or not otp:
        return Response(
            {"error": "Email and OTP are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cache_key = f"{LOGIN_OTP_CACHE_PREFIX}{email_raw}"
    data = cache.get(cache_key)
    if not data or data.get("otp") != otp:
        return Response(
            {"error": "Invalid or expired OTP. Please request a new one."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cache.delete(cache_key)
    try:
        user = User.objects.get(pk=data["user_id"])
    except User.DoesNotExist:
        return Response(
            {"error": "User not found or inactive."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not user.can_login():
        return Response(
            {"error": "User not found or inactive."},
            status=status.HTTP_404_NOT_FOUND,
        )
    try:
        token = _regenerate_auth_token(user)
    except RuntimeError as e:
        return Response(
            {"error": "Login in progress", "message": str(e)},
            status=status.HTTP_409_CONFLICT,
        )
    set_token_activity(token.key)
    is_first_login = _mark_login_and_check_first(user)

    # First-login welcome email (styled)
    if _should_send_welcome_email(user) and is_first_login:
        try:
            content = build_welcome_email(**welcome_email_kwargs_from_user(user))
            send_mail(
                subject=content.subject,
                message=content.text_body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                html_message=content.html_body,
                fail_silently=False,
            )
        except Exception:
            logger.exception("Failed to send first-login welcome email to %s", user.email)

    from ..serializers import UserSerializer
    user_serializer = UserSerializer(user, context={"request": request})
    return Response(
        {"token": token.key, "user": user_serializer.data},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def request_forgot_password_otp(request):
    """
    Request OTP for forgot password. Sends OTP to the user's email.
    """
    email = (request.data.get("email") or "").strip().lower()
    if not email:
        return Response(
            {"error": "Email is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user = User.objects.get(email=email, is_active=True)
    except User.DoesNotExist:
        return Response(
            {"error": "No active account found with this email address."},
            status=status.HTTP_404_NOT_FOUND,
        )
    otp = "".join([str(secrets.randbelow(10)) for _ in range(6)])
    cache_key = f"{FORGOT_OTP_CACHE_PREFIX}{email}"
    cache.set(cache_key, {"otp": otp, "user_id": user.id}, timeout=OTP_EXPIRY_SECONDS)
    try:
        subject = "Your IIC Booking password reset OTP"
        body_plain = f"Your one-time password (OTP) for resetting your password is: {otp}\n\nThis OTP expires in 10 minutes. Do not share it with anyone."
        html_body = f"""
        <p>Your one-time password (OTP) for resetting your password is:</p>
        <p style="font-size:24px;font-weight:bold;letter-spacing:4px;">{otp}</p>
        <p>This OTP expires in 10 minutes. Do not share it with anyone.</p>
        """
        send_mail(
            subject=subject,
            message=body_plain,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[user.email],
            html_message=html_body,
            fail_silently=False,
        )
    except Exception as e:
        logger.exception("Failed to send forgot password OTP email: %s", e)
        return Response(
            {"error": "Failed to send OTP. Please try again later."},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    return Response(
        {"message": "OTP has been sent to your email address. It expires in 10 minutes."},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def verify_forgot_password_otp_and_set_password(request):
    """
    Verify forgot-password OTP and set new password. Body: email, otp, new_password, new_password_confirm.
    """
    email = (request.data.get("email") or "").strip().lower()
    otp = (request.data.get("otp") or "").strip().replace(" ", "")
    new_password = request.data.get("new_password")
    new_password_confirm = request.data.get("new_password_confirm")
    if not email or not otp:
        return Response(
            {"error": "Email and OTP are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not new_password or len(new_password) < 8:
        return Response(
            {"error": "New password must be at least 8 characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if new_password != new_password_confirm:
        return Response(
            {"error": "New password and confirmation do not match."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cache_key = f"{FORGOT_OTP_CACHE_PREFIX}{email}"
    data = cache.get(cache_key)
    if not data or data.get("otp") != otp:
        return Response(
            {"error": "Invalid or expired OTP. Please request a new one."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    cache.delete(cache_key)
    try:
        user = User.objects.get(pk=data["user_id"], email=email, is_active=True)
    except User.DoesNotExist:
        return Response(
            {"error": "User not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    user.set_password(new_password)
    user.save(update_fields=["password"])
    return Response(
        {"message": "Password has been reset successfully. You can now sign in with your new password."},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def register(request):
    """
    Registration endpoint for external users only.
    
    Allows external users to create an account with email/password authentication.
    Only external users (Educational Institute, R&D Center, Institute, Other) can register via this endpoint.
    All other user types (Student, Individual Student, Faculty, Admin, Manager, Operator, Finance) must login via Omniport.
    
    Request Body (multipart/form-data):
        - email: User's email address (required, must be unique)
        - password: User's password (required, min 8 characters)
        - password_confirm: Password confirmation (required, must match password)
        - name: User's full name (required)
        - user_type: One of "external", "RND", "Institutes", or "other" (required, external users only)
        - profile_picture: Profile picture image file (required)
        - emp_id: Employee/Student ID (optional, must be unique if provided)
        - phone_number: Phone number (optional)
        - department: Department ID (optional)
        - documents: List of document files (optional, multiple files allowed)
        - document_types: List of document types corresponding to documents (optional)
        - document_descriptions: List of descriptions corresponding to documents (optional)
    
    Returns:
        - token: Django REST Framework auth token
        - user: User information
        - message: Success message
    """
    # Handle both JSON and multipart/form-data
    # Extract documents from request.FILES first (before serializer validation)
    # Files can be sent as 'documents[]' or 'documents' with multiple files
    documents_list = []
    if request.FILES:
        # Handle multiple files with same key (documents[])
        if 'documents[]' in request.FILES:
            documents_list = request.FILES.getlist('documents[]')
        elif 'documents' in request.FILES:
            # Could be single file or multiple
            files = request.FILES.getlist('documents')
            documents_list = files
        else:
            # Check for any file fields that might be documents
            for key in request.FILES:
                if 'document' in key.lower() and key != 'profile_picture':
                    documents_list.extend(request.FILES.getlist(key))
    
    # Extract document_types and document_descriptions from request.data
    # These come as arrays like document_types[] or document_types
    document_types = []
    if 'document_types[]' in request.data:
        document_types = request.data.getlist('document_types[]')
    elif 'document_types' in request.data:
        types = request.data.getlist('document_types')
        document_types = types if isinstance(types, list) else [types]
    
    document_descriptions = []
    if 'document_descriptions[]' in request.data:
        document_descriptions = request.data.getlist('document_descriptions[]')
    elif 'document_descriptions' in request.data:
        descs = request.data.getlist('document_descriptions')
        document_descriptions = descs if isinstance(descs, list) else [descs]
    
    # Prepare data for serializer (exclude documents from data, handle separately)
    data = request.data.copy() if hasattr(request.data, 'copy') else dict(request.data)
    
    # Remove documents from data since we'll handle them separately
    # The serializer doesn't need documents in data since we'll process them after validation
    if 'documents' in data:
        del data['documents']
    if 'documents[]' in data:
        del data['documents[]']
    
    # Remove document_types and document_descriptions from data since we handle them separately
    if 'document_types' in data:
        del data['document_types']
    if 'document_types[]' in data:
        del data['document_types[]']
    if 'document_descriptions' in data:
        del data['document_descriptions']
    if 'document_descriptions[]' in data:
        del data['document_descriptions[]']
    
    # Pass data to serializer
    # Profile picture will be automatically extracted from request.FILES by DRF
    # DRF automatically merges request.FILES when validating file fields
    serializer = RegisterSerializer(data=data)
    serializer.is_valid(raise_exception=True)
    
    # Validate user_type is allowed for registration
    user_type_code = serializer.validated_data["user_type"]
    user_type_alias = (serializer.validated_data.get("user_type_alias") or "").strip() or None

    # External users always allowed; student/individual_student allowed only with alias
    allowed_external = UserType.get_external_user_codes()
    allowed_internal_with_alias = {UserType.STUDENT, UserType.INDIVIDUAL_STUDENT}
    allowed_internal_startup = {UserType.STARTUP_INCUBATED_IITR}
    if user_type_code in allowed_external:
        pass
    elif user_type_code in allowed_internal_with_alias and user_type_alias:
        pass
    elif user_type_code in allowed_internal_startup:
        pass
    else:
        if user_type_code in allowed_internal_with_alias:
            return Response(
                {"error": "For this user type, a display name (alias) is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"error": f"Registration is only allowed for external users or IITR Post Doctoral Fellows / Research Associates / Startups. Got: {user_type_code}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate user_type code is valid
    valid_user_types = {choice[0] for choice in UserType.get_choices()}
    if user_type_code not in valid_user_types:
        return Response(
            {"error": f"Invalid user type: {user_type_code}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # For Educational Institute and Govt R&D Organizations: documents required when using public email domain
    email = serializer.validated_data.get("email", "")
    if isinstance(email, str) and "@" in email:
        email_domain = email.strip().split("@")[-1].lower()
        # External users cannot use IIT Roorkee email domain (reserved for internal users).
        if user_type_code in UserType.get_external_user_codes() and email_domain == IITR_EMAIL_DOMAIN:
            return Response(
                {
                    "error": "External users cannot register with an IIT Roorkee (iitr.ac.in) email address. Please use your institution or organization email.",
                    "fieldErrors": {"email": ["Use your institution/organization email, not @iitr.ac.in."]},
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user_type_code in (UserType.EXTERNAL, UserType.RND) and email_domain in PUBLIC_EMAIL_DOMAINS:
            if not documents_list:
                return Response(
                    {
                        "error": "A signed and filled KYC form (scan) is required when using a public email (e.g. Gmail, Yahoo). Download the form, fill it, sign it, scan it, and upload it below—or use your institution/organization email.",
                        "fieldErrors": {"documents": ["Upload the signed KYC form (scan) or use your institution email."]},
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Document type selection is optional on the frontend; treat any uploaded document as acceptable KYC scan.

    # Validate department if provided; for RND, allow organization_request instead
    department_id = serializer.validated_data.get("department")
    organization_request_id = serializer.validated_data.get("organization_request")
    department = None
    org_request = None
    if department_id:
        try:
            from iic_booking.users.models import Department
            from iic_booking.users.models.department import DepartmentType
            department = Department.objects.get(id=department_id)
        except Department.DoesNotExist:
            return Response(
                {"error": "Invalid department ID"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # IITR Post Doc / Research Associates / Startups / incubated startups must have an internal department
        if user_type_code in allowed_internal_with_alias and user_type_alias:
            if department.department_type != DepartmentType.INTERNAL:
                return Response(
                    {"error": "Internal department is required for this user type."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif user_type_code in allowed_internal_startup:
            if department.department_type != DepartmentType.INTERNAL:
                return Response(
                    {"error": "Internal startup department is required for this user type."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
    elif user_type_code == UserType.RND and organization_request_id:
        try:
            org_request = OrganizationRequest.objects.get(pk=organization_request_id)
        except OrganizationRequest.DoesNotExist:
            return Response(
                {"error": "Invalid organization request ID."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if org_request.status != OrganizationRequest.Status.PENDING:
            return Response(
                {"error": "This organization request is no longer pending. Please select an organization from the list or submit a new request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Get profile picture from validated data
    profile_picture = serializer.validated_data.get("profile_picture")
    
    # Create user with email_verified=False and admin_approved=False
    try:
        user = User.objects.create_user(
            email=serializer.validated_data["email"],
            password=serializer.validated_data["password"],
            name=serializer.validated_data["name"],
            gender=serializer.validated_data.get("gender"),
            user_type=user_type_code,
            emp_id=serializer.validated_data.get("emp_id") or None,
            phone_number=serializer.validated_data.get("phone_number") or None,
            department=department,
            organization_request=org_request,
            profile_picture=profile_picture,
            is_active=False,  # User must verify email and get admin approval
            email_verified=False,  # Email verification required
            admin_approved=False,  # Admin approval required
        )
        if user_type_alias:
            user.user_type_alias = user_type_alias[:100]
            user.save(update_fields=["user_type_alias"])
        supervisor = serializer.validated_data.get("supervisor")
        if supervisor:
            user.supervisor = supervisor
            user.save(update_fields=["supervisor"])
        program_end_date = serializer.validated_data.get("program_end_date")
        program_start_date = serializer.validated_data.get("program_start_date")
        if program_end_date:
            user.program_end_date = program_end_date
            if program_start_date:
                user.program_start_date = program_start_date
            # If duration > 1 year, put access on hold until revalidation with documents
            start = program_start_date or user.date_joined.date() if user.date_joined else program_end_date
            if hasattr(start, "year"):
                delta = (program_end_date - start).days if program_end_date else 0
                if delta > 365:
                    user.access_on_hold = True
            user.save(update_fields=["program_end_date", "program_start_date", "access_on_hold"])
    except Exception as e:
        return Response(
            {"error": f"Failed to create user: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Handle document uploads if provided
    # Use documents_list extracted from request.FILES earlier
    # Use document_types and document_descriptions extracted from request.data earlier
    
    if documents_list:
        try:
            from iic_booking.users.models import UserDocument
            
            # Validate document_types and document_descriptions match document count
            if document_types and len(document_types) != len(documents_list):
                logger.warning(f"Document types count ({len(document_types)}) doesn't match documents count ({len(documents_list)})")
            if document_descriptions and len(document_descriptions) != len(documents_list):
                logger.warning(f"Document descriptions count ({len(document_descriptions)}) doesn't match documents count ({len(documents_list)})")
            
            for idx, document_file in enumerate(documents_list):
                document_type = document_types[idx] if idx < len(document_types) else ""
                if not document_type and idx == 0:
                    # Default first uploaded document to KYC form when type is not provided by frontend.
                    document_type = "kyc_form"
                description = document_descriptions[idx] if idx < len(document_descriptions) else ""
                
                UserDocument.objects.create(
                    user=user,
                    file=document_file,
                    document_type=document_type,
                    description=description,
                )
        except Exception as e:
            logger.error(f"Failed to save documents for user {user.email}: {str(e)}")
            # Don't fail registration if document upload fails, but log it
            # User can upload documents later if needed
    
    # Send verification email: user can Accept or Reject via the link. Accept completes registration
    # (immediate login for external non-public email) or submits for admin approval (others).
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    verification_url = f"{frontend_url}/auth/self-verify?uidb64={quote(uid, safe='')}&token={quote(token, safe='')}"
    user.verification_email_sent_at = timezone.now()
    user.save(update_fields=["verification_email_sent_at"])
    try:
        CommunicationService.send_email(
            recipient=user,
            template="registration_self_verification_email",
            template_context={
                "name": user.name or user.email,
                "verification_url": verification_url,
            },
        )
    except Exception as e:
        logger.exception("Failed to send verification email via template: %s", e)
        # Fallback: send a plain email so verification still works even if the template isn't created/migrated.
        try:
            subject = "Verify your IIC Booking registration"
            body_plain = (
                f"Hello {user.name or user.email},\n\n"
                "Thank you for registering with IIC Booking.\n\n"
                "To complete your registration, please verify your email by clicking the link below:\n\n"
                f"{verification_url}\n\n"
                "This verification link is valid for 10 minutes. If no action is taken, the registration will be cancelled and your entry will be deleted.\n\n"
                "If you did not request this registration, you can safely ignore this email.\n\n"
                "— IIC Booking"
            )
            send_mail(
                subject=subject,
                message=body_plain,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception as e2:
            logger.exception("Fallback verification email send failed: %s", e2)

    # Best-effort cleanup: if user doesn't act within 10 minutes, delete the entry.
    try:
        from django.db import transaction
        from iic_booking.users.tasks import delete_unverified_user_after_verification_expiry

        sent_iso = (user.verification_email_sent_at.isoformat() if user.verification_email_sent_at else None)

        def _schedule():
            delete_unverified_user_after_verification_expiry.apply_async(
                args=[user.id, sent_iso],
                countdown=600,
            )

        transaction.on_commit(_schedule)
    except Exception as e:
        logger.exception("Failed to schedule verification expiry cleanup for user %s: %s", user.id, e)
    reg_message = "Registration successful. Please check your email to verify your registration. Click the link and choose Accept to continue, or Reject to cancel. If you accept, you may be able to log in immediately or your request will be sent for admin approval depending on your email domain."
    response_serializer = RegisterResponseSerializer(
        {
            "token": None,  # No token until email is verified (or self-verified)
            "user": {
                "id": user.id,
                "email": user.email,
                "name": user.name,
                "user_type": user.get_user_type_code() if user.user_type else None,
                "is_active": user.is_active,
            },
            "message": reg_message,
        }
    )
    
    return Response(response_serializer.data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def current_user(request):
    """
    Get current authenticated user information.

    Returns the user information for the currently authenticated user
    based on the provided authentication token. Also includes auth_inactivity_timeout_seconds
    so the frontend can run the inactivity timer in sync with the backend.
    """
    from iic_booking.users.api.token_auth import get_inactivity_timeout_seconds

    serializer = UserSerializer(request.user, context={"request": request})
    data = dict(serializer.data)
    data["auth_inactivity_timeout_seconds"] = get_inactivity_timeout_seconds(user=request.user)
    return Response(data, status=status.HTTP_200_OK)


def _is_admin_user(user):
    """True if user is Admin type or staff (can edit auth settings)."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "is_staff", False):
        return True
    return getattr(user, "user_type", None) == UserType.ADMIN


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def auth_settings(request):
    """
    GET: Return auth settings. Admin only.
    Response: { "global_inactivity_timeout_seconds": 1800, "by_user_type": { "student": 1800, ... } }
    PATCH: Update auth settings. Admin only. Body: { "global_inactivity_timeout_seconds": 1800, "by_user_type": { "student": 1800, ... } }
    """
    if not _is_admin_user(request.user):
        return Response(
            {"error": "Only Admin users can view or edit auth settings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.users.models import AuthSettings, UserTypeInactivityTimeout
    from iic_booking.users.api.token_auth import (
        CACHE_KEY_GLOBAL_TIMEOUT,
        CACHE_KEY_TYPE_TIMEOUT_PREFIX,
    )

    if request.method == "GET":
        obj = AuthSettings.get_singleton()
        by_user_type = {}
        for row in UserTypeInactivityTimeout.objects.all():
            by_user_type[row.user_type] = row.inactivity_timeout_seconds
        return Response(
            {
                "global_inactivity_timeout_seconds": obj.inactivity_timeout_seconds,
                "by_user_type": by_user_type,
                "user_type_choices": [{"code": c[0], "name": str(c[1])} for c in UserType.get_choices()],
            },
            status=status.HTTP_200_OK,
        )

    # PATCH
    data = request.data or {}
    if "global_inactivity_timeout_seconds" in data:
        try:
            seconds = int(data["global_inactivity_timeout_seconds"])
            if seconds < 60 or seconds > 86400 * 7:
                return Response(
                    {"error": "global_inactivity_timeout_seconds must be between 60 and 604800 (7 days)."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            obj = AuthSettings.get_singleton()
            obj.inactivity_timeout_seconds = seconds
            obj.save(update_fields=["inactivity_timeout_seconds"])
            cache.delete(CACHE_KEY_GLOBAL_TIMEOUT)
        except (TypeError, ValueError):
            return Response(
                {"error": "global_inactivity_timeout_seconds must be a number."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    by_user_type = data.get("by_user_type")
    if isinstance(by_user_type, dict):
        valid_types = {c[0] for c in UserType.get_choices()}
        for user_type, seconds in list(by_user_type.items()):
            if user_type not in valid_types:
                continue
            if seconds is None or (isinstance(seconds, (int, float)) and int(seconds) <= 0):
                UserTypeInactivityTimeout.objects.filter(user_type=user_type).delete()
                cache.delete(f"{CACHE_KEY_TYPE_TIMEOUT_PREFIX}{user_type}")
            else:
                try:
                    sec = int(seconds)
                    if sec < 60 or sec > 86400 * 7:
                        continue
                    UserTypeInactivityTimeout.objects.update_or_create(
                        user_type=user_type,
                        defaults={"inactivity_timeout_seconds": sec},
                    )
                    cache.delete(f"{CACHE_KEY_TYPE_TIMEOUT_PREFIX}{user_type}")
                except (TypeError, ValueError):
                    pass

    obj = AuthSettings.get_singleton()
    by_user_type_resp = {}
    for row in UserTypeInactivityTimeout.objects.all():
        by_user_type_resp[row.user_type] = row.inactivity_timeout_seconds
    return Response(
        {
            "global_inactivity_timeout_seconds": obj.inactivity_timeout_seconds,
            "by_user_type": by_user_type_resp,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def profile_me(request):
    """
    Get current user's profile information.
    
    Returns the profile information for the currently authenticated user.
    This endpoint is specifically for profile-related operations.
    
    Returns:
        - user: User profile information including id, email, name, user_type,
                uses_admin_panel, uses_email_auth
    """
    serializer = UserSerializer(request.user, context={"request": request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def profile_me_avatar(request):
    """
    GET: Stream the current user's profile picture (stable URL; does not expire).
    POST: Upload or update the current user's profile picture.
          Expects multipart form data with key 'avatar' (image file).
          Returns stable proxy profile_picture / avatar_url.
    """
    if request.method == "GET":
        from iic_booking.users.media_utils import stream_profile_picture_response

        streamed = stream_profile_picture_response(request.user)
        if streamed is None:
            return Response(
                {"error": "No profile picture set."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return streamed

    # POST: upload
    avatar_file = request.FILES.get("avatar")
    if not avatar_file:
        return Response(
            {"error": "No file provided. Send multipart form with key 'avatar'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    user = request.user
    user.profile_picture = avatar_file
    user.save(update_fields=["profile_picture"])
    url = user.get_profile_picture_url_or_none(request=request)
    return Response({
        "profile_picture": url,
        "avatar_url": url,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes([])  # Ignore stale Authorization for anonymous <img> loads
@permission_classes([AllowAny])
def user_profile_picture_proxy(request, user_id):
    """
    Stream the given user's profile picture from storage through the API.

    Stable URL for <img src> — does not expire like signed S3 URLs. Public read so
    avatars work without credentials.
    """
    from iic_booking.users.media_utils import stream_profile_picture_response

    User = get_user_model()
    try:
        user = User.objects.get(pk=user_id)
    except User.DoesNotExist:
        return Response(
            {"error": "User not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    streamed = stream_profile_picture_response(user)
    if streamed is None:
        return Response(
            {"error": "No profile picture set."},
            status=status.HTTP_404_NOT_FOUND,
        )
    return streamed


@api_view(["GET"])
@permission_classes([AllowAny])
def verify_email(request, uidb64, token):
    """
    Verify user email address and redirect to frontend.
    
    This endpoint is called when user clicks the verification link in their email.
    After verification, redirects to frontend with status and token (if approved).
    
    URL Parameters:
        - uidb64: Base64 encoded user ID
        - token: Email verification token
    
    Query Parameters (optional):
        - redirect_url: Frontend URL to redirect to (defaults to settings.FRONTEND_URL)
    
    Returns:
        - Redirects to frontend with query parameters:
          - status: "success" or "failed"
          - message: Status message
          - token: Auth token (only if admin approved)
          - email_verified: Boolean
          - admin_approved: Boolean
    """
    # Get frontend redirect URL from query params or settings
    redirect_url = request.GET.get("redirect_url") or settings.FRONTEND_URL
    
    try:
        # Decode user ID
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        # Redirect to frontend with error
        error_params = urlencode({
            "status": "failed",
            "message": "Invalid verification link",
            "error": "invalid_link",
        })
        return HttpResponseRedirect(f"{redirect_url}/auth/verify-email?{error_params}")
    
    # Check if token is valid
    if not default_token_generator.check_token(user, token):
        # Redirect to frontend with error
        error_params = urlencode({
            "status": "failed",
            "message": "Invalid or expired verification token",
            "error": "invalid_token",
        })
        return HttpResponseRedirect(f"{redirect_url}/auth/verify-email?{error_params}")
    
    # Check if already verified
    was_already_verified = user.email_verified
    
    # Mark email as verified (if not already)
    if not user.email_verified:
        user.email_verified = True
        user.save(update_fields=["email_verified"])
        # Refresh from database to get updated is_active status
        user.refresh_from_db()
    
    # Check if admin has approved (is_active is automatically managed by save())
    if user.admin_approved and user.is_active:
        # User is fully approved - redirect with success
        success_params = urlencode({
            "status": "success",
            "message": "Email verified and account approved. You can now log in.",
            "email_verified": "true",
            "admin_approved": "true",
        })
        return HttpResponseRedirect(f"{redirect_url}/auth/verify-email?{success_params}")
    elif user.admin_approved and not user.is_active:
        # Admin approved but account is inactive
        pending_params = urlencode({
            "status": "pending",
            "message": "Email verified, but account is inactive. Please contact support.",
            "email_verified": "true",
            "admin_approved": "true",
            "is_active": "false",
        })
        return HttpResponseRedirect(f"{redirect_url}/auth/verify-email?{pending_params}")
    else:
        # Email verified but waiting for admin approval
        pending_params = urlencode({
            "status": "pending",
            "message": "Email verified successfully. Your account is pending admin approval. You will be notified once approved.",
            "email_verified": "true",
            "admin_approved": "false",
        })
        return HttpResponseRedirect(f"{redirect_url}/auth/verify-email?{pending_params}")


def _get_user_from_self_verify_token(uidb64, token):
    """Validate uidb64 and token; return (user, None) or (None, error_response)."""
    try:
        uid = force_str(urlsafe_base64_decode(uidb64))
        user = User.objects.get(pk=uid)
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None, Response(
            {"error": "Invalid or expired link.", "code": "invalid_link"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not default_token_generator.check_token(user, token):
        return None, Response(
            {"error": "Invalid or expired verification token.", "code": "invalid_token"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return user, None


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def self_verify(request, uidb64, token):
    """
    Self-verification for external users with non-public email (no admin approval needed).
    GET: Return user summary for the confirmation page.
    POST: Body { "action": "accept" | "reject" }. Accept: set email_verified and admin_approved so user can log in. Reject: delete user.
    """
    user, err = _get_user_from_self_verify_token(uidb64, token)
    if err is not None:
        return err

    # Expiry enforcement: 10 minutes from last verification email sent
    sent_at = getattr(user, "verification_email_sent_at", None) or getattr(user, "date_joined", None)
    if sent_at and timezone.now() - sent_at > timezone.timedelta(minutes=10) and not user.email_verified:
        user.delete()
        return Response(
            {"error": "Verification link expired. Registration cancelled and entry deleted.", "code": "expired"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.method == "POST":
        action = (request.data.get("action") or "").strip().lower()
        if action not in ("accept", "reject"):
            return Response(
                {"error": "Invalid action. Use 'accept' or 'reject'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if action == "reject":
            user.delete()
            return Response(
                {"message": "Registration cancelled. Your details have been removed."},
                status=status.HTTP_200_OK,
            )
        user.email_verified = True
        # External with non-public email: no admin approval needed. Others: request goes to admin.
        email_domain = user.email.strip().split("@")[-1].lower() if user.email and "@" in user.email else ""
        if user.user_type in UserType.get_external_user_codes() and email_domain not in PUBLIC_EMAIL_DOMAINS:
            user.admin_approved = True
            user.save(update_fields=["email_verified", "admin_approved"])
            return Response(
                {"message": "Account verified successfully. You can now log in.", "email_verified": True, "admin_approved": True},
                status=status.HTTP_200_OK,
            )
        user.admin_approved = False
        user.save(update_fields=["email_verified", "admin_approved"])
        return Response(
            {"message": "Registration verified. Your request has been sent for admin approval. You will be notified once approved.", "email_verified": True, "admin_approved": False},
            status=status.HTTP_200_OK,
        )
    # GET: return user summary
    dept_name = user.department.name if user.department else None
    if getattr(user, "organization_request_id", None) and user.organization_request_id:
        dept_name = user.organization_request.name if user.organization_request else "Pending organization"
    return Response({
        "name": user.name,
        "email": user.email,
        "department_name": dept_name,
        "user_type_display": user.get_user_type_display() if hasattr(user, "get_user_type_display") else (user.user_type or ""),
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([AllowAny])
def resend_verification_email(request):
    """
    Resend email verification link.
    
    Request Body:
        - email: User's email address
    
    Returns:
        - message: Success message
    """
    serializer = ResendVerificationEmailSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    email = serializer.validated_data["email"]
    
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        # Don't reveal if email exists for security
        return Response(
            {"message": "If an account with this email exists and is unverified, a verification email has been sent."},
            status=status.HTTP_200_OK,
        )
    
    # Only send if user is not already verified
    if user.email_verified:
        return Response(
            {"message": "Email already verified. Waiting for admin approval."},
            status=status.HTTP_200_OK,
        )
    
    # Resend same verification link (Accept/Reject) as sent on registration
    token = default_token_generator.make_token(user)
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    frontend_url = (getattr(settings, "FRONTEND_URL", "") or "").strip().rstrip("/")
    verification_url = f"{frontend_url}/auth/self-verify?uidb64={quote(uid, safe='')}&token={quote(token, safe='')}"
    user.verification_email_sent_at = timezone.now()
    user.save(update_fields=["verification_email_sent_at"])
    try:
        CommunicationService.send_email(
            recipient=user,
            template="registration_self_verification_email",
            template_context={
                "name": user.name or user.email,
                "verification_url": verification_url,
            },
        )
        return Response(
            {"message": "Verification email sent. Please check your email and use the link to Accept or Reject."},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.exception("Failed to resend verification email via template: %s", e)
        # Fallback: send plain email
        try:
            subject = "Verify your IIC Booking registration"
            body_plain = (
                f"Hello {user.name or user.email},\n\n"
                "To complete your registration, please verify your email by clicking the link below:\n\n"
                f"{verification_url}\n\n"
                "This verification link is valid for 10 minutes. If no action is taken, the registration will be cancelled and your entry will be deleted.\n\n"
                "If you did not request this registration, you can safely ignore this email.\n\n"
                "— IIC Booking"
            )
            send_mail(
                subject=subject,
                message=body_plain,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[user.email],
                fail_silently=False,
            )
            return Response(
                {"message": "Verification email sent. Please check your email and use the link to Accept or Reject."},
                status=status.HTTP_200_OK,
            )
        except Exception as e2:
            logger.exception("Fallback resend verification email failed: %s", e2)
            return Response(
                {"error": "Failed to send verification email. Please try again later."},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def logout(request):
    """
    Logout endpoint to invalidate the current authentication token.
    
    Deletes the authentication token for the currently authenticated user,
    effectively logging them out. The token will need to be recreated on next login.
    
    If the user logged in via Omniport, also returns the Omniport logout URL
    so the frontend can redirect the user to log out from Omniport as well.
    
    Returns:
        - message: Success message confirming logout
        - omniport_logout_url: (optional) URL to redirect to for Omniport logout
    """
    try:
        user = request.user
        
        # Check if user logged in via Omniport
        omniport_logout_url = None
        if user.uses_omniport_auth():
            # Build Omniport logout URL with redirect to frontend
            frontend_url = settings.FRONTEND_URL.rstrip('/')
            logout_params = {
                "next": frontend_url,  # Redirect back to frontend after logout
            }
            omniport_logout_url = f"{settings.OMNIPORT_LOGOUT_URL}?{urlencode(logout_params)}"
            logger.info(f"User {user.email} logged in via Omniport, providing logout URL: {omniport_logout_url}")
        
        # Delete the token for the current user
        Token.objects.filter(user=user).delete()
        
        response_data = {
            "message": "Successfully logged out",
        }
        
        # Include Omniport logout URL if applicable
        if omniport_logout_url:
            response_data["omniport_logout_url"] = omniport_logout_url
        
        return Response(
            response_data,
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.error(f"Error during logout: {str(e)}")
        return Response(
            {"error": "An error occurred during logout"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_indian_states(request):
    """
    Get list of Indian states and union territories for signup (e.g. State/UT dropdown).
    Returns list of { value, label, type } with type "state" or "union_territory" for appropriate labeling.
    """
    from iic_booking.users.models.department import IndianState

    try:
        choices_with_type = IndianState.get_choices_with_type()
        result = [
            {"value": value, "label": str(label), "type": choice_type}
            for value, label, choice_type in choices_with_type
        ]
        return Response({"states": result}, status=status.HTTP_200_OK)
    except Exception as e:
        logger.error(f"Error fetching Indian states: {str(e)}")
        return Response(
            {"error": "An error occurred while fetching states"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def get_register_user_types(request):
    """
    Get user types available for registration.
    
    Returns a list of user types that are eligible for registration.
    Includes external users and internal alias types (IITR Post Doctoral Fellows,
    IITR Research Associates in Projects, IITR Startups) which map to student/individual_student.
    
    Returns:
        - user_types: List of user type objects with code, name, description, and optional alias
    """
    try:
        # External users can register via portal (exclude "Other")
        register_codes = (UserType.get_external_user_codes() - {UserType.OTHER}) | {
            UserType.STARTUP_INCUBATED_IITR,
        }
        all_choices = UserType.get_choices()
        # Coerce to str so lazy translation objects are JSON-serializable (avoids 500)
        register_choices = [(str(code), str(name)) for code, name in all_choices if code in register_codes]

        # Build list with plain strings only (no serializer to avoid any lazy/serialization issues)
        user_types_list = [
            {"code": code, "name": name, "description": ""}
            for code, name in register_choices
        ]

        # Alias types: same functionality as IITR Student or Individual Student, display alias in UI
        alias_types = [
            (UserType.STUDENT, "IITR Post Doctoral Fellows", "Same as IITR Student. Use faculty wallet for bookings."),
            (UserType.STUDENT, "IITR Research Associates in Projects", "Same as IITR Student. Use faculty wallet for bookings."),
            (UserType.INDIVIDUAL_STUDENT, "IITR Startups", "Same as Individual Student. Own wallet for bookings."),
        ]
        for code, name, description in alias_types:
            user_types_list.append({
                "code": str(code),
                "name": name,
                "description": description,
                "alias": name,
            })

        return Response(
            {"user_types": user_types_list},
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.exception("Error fetching user types")
        return Response(
            {"error": "An error occurred while fetching user types"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def search_faculty_for_signup(request):
    """
    Search Internal IITR Faculty (faculty with internal department) for supervisor selection
    during signup (Post Doc / Research Associates). Returns name, email, department for verification.
    """
    from django.db.models import Q

    from iic_booking.users.models.department import DepartmentType

    query = request.query_params.get("q", "").strip()
    limit = min(int(request.query_params.get("limit", 20)), 50)

    if not query:
        return Response({"results": []}, status=status.HTTP_200_OK)

    faculty_queryset = (
        User.objects.filter(user_type=UserType.FACULTY, is_active=True)
        .filter(department__department_type=DepartmentType.INTERNAL)
        .filter(Q(name__icontains=query) | Q(email__icontains=query))
        .select_related("department")[:limit]
    )

    results = [
        {
            "id": f.id,
            "name": f.name or f.email,
            "email": f.email,
            "department": f.department.name if f.department else None,
        }
        for f in faculty_queryset
    ]

    return Response({"results": results}, status=status.HTTP_200_OK)
